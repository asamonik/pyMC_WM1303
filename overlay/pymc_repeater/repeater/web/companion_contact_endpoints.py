"""Loop-owned contact mutations over the upstream companion REST endpoints."""

import asyncio
import json
import logging
import queue
import threading
import time
from concurrent.futures import TimeoutError as FutureTimeoutError

import cherrypy

from .auth.middleware import require_auth
from .companion_endpoints import CompanionAPIEndpoints as _UpstreamCompanionAPIEndpoints

logger = logging.getLogger("CompanionAPI")


class CompanionAPIEndpoints(_UpstreamCompanionAPIEndpoints):
    def __init__(self, *args, **kwargs):
        self._callback_registration_lock = threading.Lock()
        super().__init__(*args, **kwargs)

    def _ensure_callbacks(self):
        # CherryPy may start multiple event streams in different worker threads.
        # Serialize upstream's check/register/publish flag without changing its
        # default-bridge selection or retry-on-later-request behavior.
        with self._callback_registration_lock:
            return super()._ensure_callbacks()

    @cherrypy.expose
    def events(self, **kwargs):
        """Stream companion events under the existing tool-level authentication."""
        cherrypy.response.headers["Content-Type"] = "text/event-stream"
        cherrypy.response.headers["Cache-Control"] = "no-cache"
        cherrypy.response.headers["Connection"] = "keep-alive"
        cherrypy.response.headers["X-Accel-Buffering"] = "no"

        def generate():
            client_queue = None
            registered = False
            try:
                self._ensure_callbacks()
                # A response closed before its first iteration must not leave
                # a queue whose generator finally block has never been entered.
                client_queue = queue.Queue(maxsize=self._sse_queue_maxsize)
                with self._sse_lock:
                    self._sse_clients.append(client_queue)
                    registered = True
                payload = {"event": "connected", "timestamp": int(time.time())}
                yield f"data: {json.dumps(payload)}\n\n"

                while True:
                    with self._sse_lock:
                        subscribed = client_queue in self._sse_clients
                    if not subscribed:
                        # Upstream evicts a full queue. Expose the gap and end
                        # this stream so native clients can reconnect and reload
                        # durable state; SSE notifications have no replay log.
                        payload = {"event": "resync_required", "reason": "slow_consumer",
                                   "timestamp": int(time.time())}
                        yield f"data: {json.dumps(payload)}\n\n"
                        return
                    # An open stream can precede the first companion's live
                    # activation. Retry the locked registration without making
                    # the browser reconnect; a completed registration is a no-op.
                    self._ensure_callbacks()
                    try:
                        item = client_queue.get(timeout=float(self._sse_keepalive_sec))
                        yield f"data: {json.dumps(item)}\n\n"
                    except queue.Empty:
                        yield ": keepalive\n\n"
            except GeneratorExit:
                pass
            except Exception as exc:
                logger.debug("Companion SSE stream ended (%s)", type(exc).__name__)
            finally:
                if registered:
                    with self._sse_lock:
                        if client_queue in self._sse_clients:
                            self._sse_clients.remove(client_queue)

        return generate()

    events._cp_config = {"response.stream": True}

    @staticmethod
    def _positive_contact_integer(value, field):
        if isinstance(value, str):
            value = value.strip()
            if not value.isascii() or not value.isdecimal():
                raise cherrypy.HTTPError(400, f"{field} must be a positive integer")
            try:
                value = int(value)
            except ValueError:
                raise cherrypy.HTTPError(400, f"{field} must be a positive integer") from None
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise cherrypy.HTTPError(400, f"{field} must be a positive integer")
        return value

    def _contact_body(self):
        self._require_post()
        body = self._get_json_body()
        if not isinstance(body, dict):
            raise cherrypy.HTTPError(400, "JSON body must be an object")
        return body

    def _run_contact_action(self, body, method, **params):
        loop = self.event_loop
        if loop is None or loop.is_closed() or not loop.is_running():
            raise cherrypy.HTTPError(503, "Companion event loop is unavailable")

        async def dispatch():
            if getattr(self.daemon_instance, "_shutdown_started", False):
                raise RuntimeError("Companion is shutting down")
            # Resolve on the daemon loop too: configuration activation and
            # shutdown can change its registrations while HTTP is running.
            bridge = self._get_bridge(**self._resolve_bridge_params(body))
            for server in getattr(self.daemon_instance, "companion_frame_servers", ()):
                if server.bridge is bridge:
                    return await getattr(server, method)(**params)
            raise RuntimeError("Companion contact persistence is unavailable")

        coro = dispatch()
        try:
            future = asyncio.run_coroutine_threadsafe(coro, loop)
        except RuntimeError:
            coro.close()
            raise cherrypy.HTTPError(503, "Companion event loop is unavailable") from None
        try:
            return future.result(timeout=30.0)
        except FutureTimeoutError:
            # The frame server owns admitted work and drains it at shutdown.
            # A timed-out HTTP request must not cancel a pending SQLite save.
            raise cherrypy.HTTPError(
                504, "Contact operation has not finished; it may still complete. Check contacts before retrying."
            ) from None
        except ValueError as exc:
            raise cherrypy.HTTPError(409, str(exc)) from None
        except RuntimeError as exc:
            raise cherrypy.HTTPError(503, str(exc)) from None

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @require_auth
    def import_repeater_contacts(self, **kwargs):
        """Seed contacts with one validated save, without resetting known peers."""
        body = self._contact_body()
        if not isinstance(body.get("companion_name"), str) or not body["companion_name"]:
            raise cherrypy.HTTPError(400, "companion_name required")
        contact_types = body.get("contact_types")
        if contact_types is not None:
            allowed = {"companion", "repeater", "room_server", "sensor"}
            if (not isinstance(contact_types, list)
                    or any(not isinstance(value, str) or value not in allowed for value in contact_types)):
                raise cherrypy.HTTPError(
                    400, "contact_types must list companion, repeater, room_server or sensor"
                )
        filters = {"contact_types": contact_types}
        for field in ("hours", "limit"):
            value = body.get(field)
            filters[field] = None if value is None else self._positive_contact_integer(value, field)
        result = self._run_contact_action(body, "import_repeater_contacts", **filters)
        return self._success(result)

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @require_auth
    def reset_path(self, **kwargs):
        """Reset routing on the daemon loop, serialized with contact imports."""
        body = self._contact_body()
        pub_key = self._pub_key_from_hex(body.get("pub_key", ""))
        result = self._run_contact_action(body, "reset_contact_path", pubkey=pub_key)
        return self._success({"reset": result})
