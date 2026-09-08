import json
import logging
import os
import re
import secrets
import threading
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import cherrypy
import cherrypy_cors
from openhop_core.protocol.utils import PAYLOAD_TYPES, ROUTE_TYPES

from repeater import __version__
from repeater.data_acquisition import SQLiteHandler

from .api_endpoints import APIEndpoints
from .auth.cherrypy_tool import register_require_auth_tool
from .auth.api_tokens import APITokenManager
from .auth.jwt_handler import JWTHandler
from .auth_endpoints import AuthEndpoints

# WebSocket support
try:
    from repeater.data_acquisition.websocket_handler import (
        PacketWebSocket,
        broadcast_packet,
        init_websocket,
        shutdown_websocket,
    )
    from .companion_ws_proxy import CompanionFrameWebSocket, set_daemon as _set_companion_daemon

    WEBSOCKET_AVAILABLE = True
except ImportError:
    WEBSOCKET_AVAILABLE = False
    logger = logging.getLogger("HTTPServer")
    logger.warning("ws4py not available - WebSocket support disabled")

logger = logging.getLogger("HTTPServer")


class _ShutdownResponse:
    """Close an owned WSGI response when the server stops, including SSE."""

    def __init__(self, body, stopping):
        self.body = body
        self.iterator = iter(body)
        self.stopping = stopping
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        if self.closed or self.stopping.is_set():
            self.close()
            raise StopIteration
        chunk = next(self.iterator)
        if self.stopping.is_set():
            self.close()
            raise StopIteration
        return chunk

    def close(self):
        if not self.closed:
            self.closed = True
            close = getattr(self.body, "close", None)
            if callable(close):
                close()


class _ShutdownMiddleware:
    """Reject new requests and end active streams at their next heartbeat."""

    def __init__(self, nextapp, stopping):
        self.nextapp = nextapp
        self.stopping = stopping

    def __call__(self, environ, start_response):
        if self.stopping.is_set():
            body = b"Server shutting down\n"
            start_response("503 Service Unavailable", [
                ("Content-Type", "text/plain; charset=utf-8"),
                ("Content-Length", str(len(body))),
            ])
            return [body]
        return _ShutdownResponse(self.nextapp(environ, start_response), self.stopping)


# In-memory log buffer
class LogBuffer(logging.Handler):

    def __init__(self, max_lines=100):
        super().__init__()
        self.logs = deque(maxlen=max_lines)
        self.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))

    def emit(self, record):

        try:
            msg = self.format(record)
            self.logs.append(
                {
                    "message": msg,
                    "timestamp": datetime.fromtimestamp(record.created).isoformat(),
                    "level": record.levelname,
                }
            )
        except Exception:
            self.handleError(record)


# Global log buffer instance
_log_buffer = LogBuffer(max_lines=100)


class DocEndpoint:
    """Simple wrapper to serve API docs at /doc"""

    def __init__(self, api_endpoints):
        self.api_endpoints = api_endpoints

    @cherrypy.expose
    def index(self, **kwargs):
        """Serve Swagger UI at /doc"""
        return self.api_endpoints.docs()

    @cherrypy.expose
    def docs(self):
        """Serve Swagger UI at /doc/docs"""
        return self.api_endpoints.docs()

    @cherrypy.expose
    def openapi_json(self):
        """Serve OpenAPI spec in JSON format at /doc/openapi.json"""
        import json
        import os

        import yaml

        spec_path = os.path.join(os.path.dirname(__file__), "openapi.yaml")
        try:
            with open(spec_path, "r") as f:
                spec_content = yaml.safe_load(f)

            cherrypy.response.headers["Content-Type"] = "application/json"
            return json.dumps(spec_content).encode("utf-8")
        except FileNotFoundError:
            cherrypy.response.status = 404
            return json.dumps({"error": "OpenAPI spec not found"}).encode("utf-8")
        except Exception as e:
            cherrypy.response.status = 500
            return json.dumps({"error": f"Error loading OpenAPI spec: {e}"}).encode("utf-8")


class StatsApp:

    def __init__(
        self,
        stats_getter: Optional[Callable] = None,
        node_name: str = "Repeater",
        pub_key: str = "",
        send_advert_func: Optional[Callable] = None,
        config: Optional[dict] = None,
        event_loop=None,
        daemon_instance=None,
        config_path=None,
    ):

        self.stats_getter = stats_getter
        self.node_name = node_name
        self.pub_key = pub_key
        self.dashboard_template = None
        self.config = config or {}

        # Path to the compiled Vue.js application
        # Use web_path from config if provided, otherwise use default
        default_html_dir = os.path.join(os.path.dirname(__file__), "html")
        web_path = self.config.get("web", {}).get("web_path")
        self.html_dir = web_path if web_path is not None else default_html_dir

        # Create nested API object for routing
        self.api = APIEndpoints(
            stats_getter, send_advert_func, self.config, event_loop, daemon_instance, config_path
        )

        # Create doc endpoint for API documentation
        self.doc = DocEndpoint(self.api)

    @cherrypy.expose
    def index(self, **kwargs):
        """Serve the Vue.js application index.html."""
        index_path = os.path.join(self.html_dir, "index.html")
        try:
            with open(index_path, "r", encoding="utf-8") as f:
                html = f.read()
            # Load before the bundled Console module without modifying its
            # versioned/minified assets. WM upgrades outlive a daemon restart.
            return re.sub(r"(?i)(<head\b[^>]*>)",
                          r'\1<script src="/wm1303-updater.js"></script>', html, count=1)
        except FileNotFoundError:
            raise cherrypy.HTTPError(404, "Application not found. Please build the frontend first.")
        except Exception as e:
            logger.error(f"Error serving index.html: {e}")
            raise cherrypy.HTTPError(500, "Internal server error")

    @cherrypy.expose
    def default(self, *args, **kwargs):
        """Handle client-side routing - serve index.html for all non-API routes."""
        # Handle OPTIONS requests for any path
        if cherrypy.request.method == "OPTIONS":
            return ""

        # Let API routes pass through
        if args and args[0] == "api":
            raise cherrypy.NotFound()

        # Handle WebSocket routes
        if args and len(args) >= 2 and args[0] == "ws" and args[1] in ("packets", "companion_frame"):
            # WebSocket tool will intercept this
            return ""

        # For all other routes, serve the Vue.js app (client-side routing)
        return self.index()


class HTTPStatsServer:
    # CherryPy's process-wide bus must never be stopped by another instance
    # whose construction or startup failed.
    _engine_owner = None
    _engine_lock = threading.Lock()

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8000,
        stats_getter: Optional[Callable] = None,
        node_name: str = "Repeater",
        pub_key: str = "",
        send_advert_func: Optional[Callable] = None,
        config: Optional[dict] = None,
        event_loop=None,
        daemon_instance=None,
        config_path=None,
    ):

        self.host = host
        self.port = port
        self.config = config or {}
        self.config_path = config_path
        self.daemon_instance = daemon_instance
        self.sqlite_handler = None
        self.wm1303_api = None
        self._started = False
        self._stopped = False
        self._engine_started = False
        self._api_started = False
        self._websocket_started = False
        self._mounted_apps = {}
        # Startup rollback calls stop() on this same thread. Serialize full
        # start/stop operations, including callers waiting for an ongoing drain.
        self._stop_lock = threading.RLock()
        self._stopping = threading.Event()

        try:
            # Authentication owns a SQLite checkpoint worker even before the
            # listener is started; clean it if any later constructor step fails.
            self._init_auth_handlers()

            self.app = StatsApp(
                stats_getter,
                node_name,
                pub_key,
                send_advert_func,
                config,
                event_loop,
                daemon_instance,
                config_path,
            )

            # Create auth endpoints (APIEndpoints has the config_manager)
            self.auth_app = AuthEndpoints(
                self.config, self.jwt_handler, self.token_manager, self.app.api.config_manager
            )

            # Create documentation endpoints as separate app
            self.doc_app = DocEndpoint(self.app.api)

            # Set up CORS at the server level if enabled
            self._cors_enabled = self.config.get("web", {}).get("cors_enabled", False)
            logger.info(f"CORS enabled: {self._cors_enabled}")
        except BaseException:
            try:
                self.stop()
            except Exception as cleanup_error:
                logger.warning("HTTP constructor cleanup also failed: %s", cleanup_error)
            raise

    def _init_auth_handlers(self):
        """Initialize JWT handler and API token manager."""
        # Get or generate JWT secret from repeater.security
        repeater_config = self.config.get("repeater", {})
        security_config = repeater_config.get("security", {})
        jwt_secret = security_config.get("jwt_secret", "")

        if not jwt_secret:
            # Auto-generate JWT secret
            jwt_secret = secrets.token_hex(32)
            logger.info("Generated an authentication signing secret")

            # Try to save to config if config_path is available
            if self.config_path:
                try:
                    import yaml
                    from repeater.config import CONFIG_WRITE_LOCK, save_config

                    with CONFIG_WRITE_LOCK:
                        with open(self.config_path, "r") as f:
                            config_data = yaml.safe_load(f)
                        if not isinstance(config_data, dict):
                            raise ValueError("Configuration must contain a YAML mapping")
                        if config_data.get("repeater") is None:
                            config_data["repeater"] = {}
                        if config_data["repeater"].get("security") is None:
                            config_data["repeater"]["security"] = {}
                        saved_security = config_data["repeater"]["security"]
                        # A second constructor may already have persisted a
                        # signing secret while this instance waited for the lock.
                        if saved_security.get("jwt_secret"):
                            jwt_secret = saved_security["jwt_secret"]
                        else:
                            saved_security["jwt_secret"] = jwt_secret
                            if not save_config(config_data, self.config_path):
                                raise OSError("Could not persist authentication signing secret")

                    logger.info(f"Saved auto-generated JWT secret to {self.config_path}")
                except Exception as e:
                    # Do not start with an ephemeral identity when durable
                    # config was requested: later restarts would revoke tokens.
                    raise RuntimeError("Failed to save authentication signing secret") from e
            else:
                logger.warning("No config path; authentication signing secret lasts only for this process")
            self.config.setdefault("repeater", {}).setdefault("security", {})["jwt_secret"] = jwt_secret

        # Initialize JWT handler with configurable expiry (default 1 hour)
        jwt_expiry_minutes = security_config.get("jwt_expiry_minutes", 60)
        self.jwt_handler = JWTHandler(jwt_secret, expiry_minutes=jwt_expiry_minutes)
        logger.info(f"JWT handler initialized (token expiry: {jwt_expiry_minutes} minutes)")

        # Initialize API token manager
        storage_dir = self.config.get("storage", {}).get("storage_dir", ".")

        # Ensure storage directory exists
        os.makedirs(storage_dir, exist_ok=True)

        # Initialize SQLiteHandler and APITokenManager
        self.sqlite_handler = SQLiteHandler(Path(storage_dir))
        self.token_manager = APITokenManager(self.sqlite_handler, jwt_secret)
        logger.info(f"API token manager initialized with database at {storage_dir}/repeater.db")

    def _setup_server_cors(self):
        """Set up CORS using cherrypy_cors.install()"""
        # Configure CORS to allow Authorization header
        # cherrypy-cors will handle preflight requests automatically
        cherrypy_cors.install()

        logger.info("CORS support enabled with Authorization header")

    def _json_error_handler(self, status, message, traceback, version):
        """Return JSON error responses instead of HTML for API endpoints"""
        cherrypy.response.headers["Content-Type"] = "application/json"
        return json.dumps({"success": False, "error": message})

    def _mount(self, app, path, config):
        # CherryPy stores the root mount at "", not "/".
        path = path.rstrip('/')
        # Cover upstream streams too, without duplicating their endpoints. WSGI
        # close() releases request state and the inner stream's subscriptions.
        config.setdefault("/", {}).setdefault("wsgi.pipeline", []).append((
            "shutdown", lambda nextapp: _ShutdownMiddleware(nextapp, self._stopping)
        ))
        previous = cherrypy.tree.apps.get(path)
        mounted = cherrypy.tree.mount(app, path, config)
        self._mounted_apps[path] = (mounted, previous)

    def start(self):
        with self._stop_lock:
            self._start()

    def _start(self):
        try:
            if self._stopping.is_set():
                raise RuntimeError("HTTP server is stopped; create a new instance to restart")
            if self._started:
                return
            with HTTPStatsServer._engine_lock:
                if (HTTPStatsServer._engine_owner is not None or
                        cherrypy.engine.state not in (cherrypy.engine.states.STOPPED,
                                                      cherrypy.engine.states.EXITING)):
                    raise RuntimeError("CherryPy HTTP engine is already in use")
                HTTPStatsServer._engine_owner = self

            # WM1303 hotfix v2.6.1: explicitly register the CherryPy require_auth tool.
            # Under upstream openhop_repeater@dev the module-level import side-effect
            # is no longer sufficient; without this call every endpoint that sets
            # tools.require_auth.on returns HTTP 500 (AttributeError on Toolbox).
            register_require_auth_tool()

            if self._cors_enabled:
                self._setup_server_cors()

            default_html_dir = os.path.join(os.path.dirname(__file__), "html")
            web_path = self.config.get("web", {}).get("web_path")
            html_dir = web_path if web_path is not None else default_html_dir

            assets_dir = os.path.join(html_dir, "assets")
            next_dir = os.path.join(html_dir, "_next")

            # Build config with conditional CORS settings
            config = {
                "/": {
                    "tools.sessions.on": False,
                    # "tools.gzip.on": True,
                    # "tools.gzip.mime_types": ["application/json", "text/html", "text/plain"],
                    # Ensure proper content types for static files
                    "tools.staticfile.content_types": {
                        "js": "application/javascript",
                        "css": "text/css",
                        "html": "text/html; charset=utf-8",
                        "svg": "image/svg+xml",
                        "txt": "text/plain",
                    },
                },
                # Require authentication for all /api endpoints
                "/api": {
                    "tools.require_auth.on": True,
                },
                # Enable gzip for bulk packet downloads
                "/api/bulk_packets": {
                    "tools.gzip.on": True,
                    "tools.gzip.mime_types": ["application/json"],
                    "tools.gzip.compress_level": 6,
                },
                # Public documentation endpoints (no auth required)
                "/api/openapi": {
                    "tools.require_auth.on": False,
                },
                "/api/docs": {
                    "tools.require_auth.on": False,
                },
                # Public setup wizard endpoints (no auth required)
                "/api/needs_setup": {
                    "tools.require_auth.on": False,
                },
                "/api/hardware_options": {
                    "tools.require_auth.on": False,
                },
                "/api/radio_presets": {
                    "tools.require_auth.on": False,
                },
                "/api/setup_wizard": {
                    "tools.require_auth.on": False,
                },
                "/favicon.ico": {
                    "tools.staticfile.on": True,
                    "tools.staticfile.filename": os.path.join(html_dir, "favicon.ico"),
                },
            }

            # Add WebSocket configuration to main config if available
            if WEBSOCKET_AVAILABLE:
                try:
                    self._websocket_started = True
                    init_websocket()
                    config["/ws/packets"] = {
                        "tools.websocket.on": True,
                        "tools.websocket.handler_cls": PacketWebSocket,
                        "tools.trailing_slash.on": False,
                        "tools.require_auth.on": False,
                        "tools.gzip.on": False,
                    }
                    logger.info("WebSocket endpoint configured at /ws/packets")

                    # Companion frame proxy (binary WS ↔ TCP byte pipe)
                    if self.daemon_instance:
                        _set_companion_daemon(self.daemon_instance)
                        config["/ws/companion_frame"] = {
                            "tools.websocket.on": True,
                            "tools.websocket.handler_cls": CompanionFrameWebSocket,
                            "tools.trailing_slash.on": False,
                            "tools.require_auth.on": False,
                            "tools.gzip.on": False,
                        }
                        logger.info("WebSocket endpoint configured at /ws/companion_frame")
                except Exception as e:
                    logger.error(f"Failed to initialize WebSocket: {e}")
                    import traceback

                    logger.error(traceback.format_exc())

            # Add CORS configuration if enabled
            if self._cors_enabled:
                cors_config = {
                    "cors.expose.on": True,
                    "tools.response_headers.on": True,
                    "tools.response_headers.headers": [
                        ("Access-Control-Allow-Origin", "*"),
                        ("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS"),
                        ("Access-Control-Allow-Headers", "Authorization, Content-Type, X-API-Key"),
                        ("Access-Control-Allow-Credentials", "true"),
                    ],
                    # Disable automatic trailing slash redirects to prevent CORS issues
                    "tools.trailing_slash.on": False,
                }

                # Apply CORS to paths
                config["/"].update(cors_config)
                config["/api"].update(cors_config)

            # Add Vue.js assets support only if assets directory exists
            if os.path.isdir(assets_dir):
                config["/assets"] = {
                    "tools.staticdir.on": True,
                    "tools.staticdir.dir": assets_dir,
                    # Set proper content types for assets
                    "tools.staticdir.content_types": {
                        "js": "application/javascript",
                        "css": "text/css",
                        "map": "application/json",
                    },
                }

            # Add Next.js support only if _next directory exists
            if os.path.isdir(next_dir):
                config["/_next"] = {
                    "tools.staticdir.on": True,
                    "tools.staticdir.dir": next_dir,
                    # Set proper content types for Next.js assets
                    "tools.staticdir.content_types": {
                        "js": "application/javascript",
                        "css": "text/css",
                        "map": "application/json",
                    },
                }

            # Only add CORS to static assets if CORS is enabled
            if self._cors_enabled:
                if "/assets" in config:
                    config["/assets"]["cors.expose.on"] = True
                if "/_next" in config:
                    config["/_next"]["cors.expose.on"] = True
                config["/favicon.ico"]["cors.expose.on"] = True

            http_cfg = self.config.get("http", {}) if isinstance(self.config, dict) else {}
            thread_pool = max(2, int(http_cfg.get("thread_pool", 8)))
            thread_pool_max = max(thread_pool, int(http_cfg.get("thread_pool_max", 16)))
            socket_timeout = max(15, int(http_cfg.get("socket_timeout", 65)))
            socket_queue_size = max(10, int(http_cfg.get("socket_queue_size", 100)))

            cherrypy.config.update(
                {
                    "server.socket_host": self.host,
                    "server.socket_port": self.port,
                    "server.socket_queue_size": socket_queue_size,
                    "engine.autoreload.on": False,
                    "log.screen": False,
                    "log.access_file": "",  # Disable access log file
                    "log.error_file": "",  # Disable error log file
                    # Disable automatic trailing slash redirects globally
                    "tools.trailing_slash.on": False,
                    # Custom error handler to return JSON for API endpoints
                    "error_page.401": self._json_error_handler,
                    # Add auth handlers to config so they're accessible in endpoints
                    "jwt_handler": self.jwt_handler,
                    "token_manager": self.token_manager,
                    # Bound the thread pool to prevent unbounded growth.
                    # SSE streams each hold one thread; allow headroom for concurrent
                    # SSE clients plus normal API polling without growing unboundedly.
                    "server.thread_pool": thread_pool,
                    "server.thread_pool_max": thread_pool_max,
                    # Close idle/stale connections so their threads return to the pool.
                    "server.socket_timeout": socket_timeout,
                }
            )
            logger.info(
                "HTTP worker config: thread_pool=%s, thread_pool_max=%s, socket_timeout=%ss, socket_queue_size=%s",
                thread_pool,
                thread_pool_max,
                socket_timeout,
                socket_queue_size,
            )

            # Serve wm1303.html as static file (WM1303 Manager dashboard)
            wm1303_html_path = os.path.join(html_dir, "wm1303.html")
            config["/wm1303-updater.js"] = {
                "tools.staticfile.on": True,
                "tools.staticfile.filename": os.path.join(os.path.dirname(__file__), "html", "wm1303-updater.js"),
                "tools.require_auth.on": False,
            }
            if os.path.isfile(wm1303_html_path):
                config["/wm1303.html"] = {
                    "tools.staticfile.on": True,
                    "tools.staticfile.filename": wm1303_html_path,
                    "tools.require_auth.on": False,
                }
                logger.info(f"WM1303 dashboard available at /wm1303.html")

            # Mount main app
            self._mount(self.app, "/", config)

            # Mount auth endpoints
            auth_config = {
                "/": {
                    "tools.response_headers.on": True,
                    "tools.response_headers.headers": [
                        ("Content-Type", "application/json"),
                    ],
                    # Disable automatic trailing slash redirects
                    "tools.trailing_slash.on": False,
                }
            }
            if self._cors_enabled:
                auth_config["/"]["cors.expose.on"] = True
                # Add CORS headers for OPTIONS requests
                auth_config["/"]["tools.response_headers.headers"].extend(
                    [
                        ("Access-Control-Allow-Origin", "*"),
                        ("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS"),
                        ("Access-Control-Allow-Headers", "Authorization, Content-Type, X-API-Key"),
                        ("Access-Control-Allow-Credentials", "true"),
                    ]
                )

            self._mount(self.auth_app, "/auth", auth_config)

            # Mount documentation endpoints as separate app (no auth required for docs)
            doc_config = {
                "/": {
                    "tools.require_auth.on": False,  # Docs are publicly accessible
                    "tools.response_headers.on": True,
                    "tools.response_headers.headers": [
                        ("Content-Type", "text/html; charset=utf-8"),
                    ],
                    "tools.trailing_slash.on": False,
                }
            }
            if self._cors_enabled:
                doc_config["/"]["cors.expose.on"] = True
                doc_config["/"]["tools.response_headers.headers"].extend(
                    [
                        ("Access-Control-Allow-Origin", "*"),
                        ("Access-Control-Allow-Methods", "GET, POST, OPTIONS"),
                        ("Access-Control-Allow-Headers", "Authorization, Content-Type, X-API-Key"),
                    ]
                )

            self._mount(self.doc_app, "/doc", doc_config)

            # Mount WM1303 API
            try:
                from .wm1303_api import WM1303API
                self.wm1303_api = WM1303API(self.daemon_instance)
                wm1303_api_config = {
                    "/": {
                        "tools.json_out.on": False,
                        "tools.trailing_slash.on": False,
                        "tools.require_auth.on": True,
                    }
                }
                if self._cors_enabled:
                    wm1303_api_config["/"]["cors.expose.on"] = True
                self._mount(self.wm1303_api, "/api/wm1303", wm1303_api_config)
                logger.info("WM1303 API mounted at /api/wm1303")
            except ImportError:
                logger.info("WM1303 API not available (wm1303_api module not found)")
            except Exception as e:
                logger.warning(f"WM1303 API mount failed: {e}")

            # Store auth handlers in cherrypy config for middleware access
            cherrypy.config.update(
                {
                    "jwt_handler": self.jwt_handler,
                    "token_manager": self.token_manager,
                    "security_config": self.config.get("security", {}),
                }
            )

            # Completely disable access logging
            cherrypy.log.access_log.propagate = False
            cherrypy.log.error_log.setLevel(logging.ERROR)

            self._engine_started = True
            cherrypy.engine.start()
            if getattr(self, 'wm1303_api', None) is not None:
                self._api_started = True
                self.wm1303_api.start()
            self._started = True
            server_url = "http://{}:{}".format(self.host, self.port)
            logger.info(f"HTTP stats server started on {server_url}")

        except BaseException as e:
            logger.error(f"Failed to start HTTP server: {e}")
            try:
                self.stop()
            except Exception as cleanup_error:
                logger.warning("HTTP startup cleanup also failed: %s", cleanup_error)
            raise

    def stop(self):
        # Close admission even when another thread still owns startup/cleanup.
        self._stopping.set()
        with self._stop_lock:
            if self._stopped:
                return
            self._stop()
            # A raised cleanup failure must not turn later calls into no-ops.
            self._stopped = True

    def _stop(self):
        self._started = False
        errors = []
        try:
            calibration = getattr(getattr(getattr(self, "app", None), "api", None), "cad_calibration", None)
            if calibration is not None:
                try:
                    calibration.stop_calibration()
                except Exception as e:
                    errors.append(e)
                    logger.warning("Error stopping CAD calibration: %s", e)
            if self._engine_started:
                try:
                    cherrypy.engine.exit()
                    self._engine_started = False
                    logger.info("HTTP stats server stopped")
                except Exception as e:
                    errors.append(e)
                    logger.warning(f"Error stopping HTTP server: {e}")
            if self._api_started:
                try:
                    self.wm1303_api.stop()
                    self._api_started = False
                except Exception as e:
                    errors.append(e)
                    logger.warning("Error stopping WM1303 API workers: %s", e)
            if self._websocket_started:
                try:
                    shutdown_websocket()
                    self._websocket_started = False
                except Exception as e:
                    errors.append(e)
                    logger.warning("Error stopping WebSocket workers: %s", e)
            if not self._engine_started:
                for path, (mounted, previous) in self._mounted_apps.items():
                    if cherrypy.tree.apps.get(path) is mounted:
                        if previous is None:
                            cherrypy.tree.apps.pop(path, None)
                        else:
                            cherrypy.tree.apps[path] = previous
                self._mounted_apps.clear()
        finally:
            if self.sqlite_handler is not None:
                try:
                    self.sqlite_handler.stop_wal_checkpoint_thread()
                except Exception as e:
                    errors.append(e)
                    logger.warning("Error stopping authentication checkpoint worker: %s", e)
                try:
                    self.sqlite_handler.close_thread_connection()
                except Exception as e:
                    errors.append(e)
                    logger.warning("Error closing authentication database connection: %s", e)
        if errors:
            # Keep ownership while a global worker/plugin may still be active;
            # a replacement server must not reuse it after incomplete cleanup.
            raise RuntimeError("HTTP server shutdown incomplete") from errors[0]
        with HTTPStatsServer._engine_lock:
            if HTTPStatsServer._engine_owner is self:
                HTTPStatsServer._engine_owner = None
