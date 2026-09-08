"""
WebSocket handler for real-time packet updates - simple ws4py implementation
"""

import json
import logging
import socket
import threading
from urllib.parse import parse_qs

import cherrypy
from ws4py.manager import WebSocketManager
from ws4py.server.cherrypyserver import WebSocketPlugin, WebSocketTool
from ws4py.websocket import WebSocket

logger = logging.getLogger("WebSocket")

# Suppress noisy ws4py error logs for normal disconnections (ConnectionResetError, etc.)
logging.getLogger("ws4py").setLevel(logging.CRITICAL)

# Global set of connected clients
_connected_clients = set()

# Heartbeat configuration
PING_INTERVAL = 30  # seconds
_heartbeat_thread = None
_heartbeat_running = False
_heartbeat_stop = None
_websocket_plugin = None
_lifecycle_lock = threading.RLock()


class _LifecycleWebSocketManager(WebSocketManager):
    """Own admission and retirement while retaining ws4py's frame parser."""

    def __init__(self, finish_handler):
        self.started_running = threading.Event()
        self.retiring = {}
        self._finish_handler = finish_handler
        super().__init__()

    def retire_unregistered(self, handler, connection):
        with self.lock:
            self.retiring[handler] = connection
        self._complete_retirement(handler, connection)

    def _complete_retirement(self, handler, connection):
        self._finish_handler(handler, connection)
        with self.lock:
            self.retiring.pop(handler, None)

    def add(self, websocket):
        connection = websocket.sock
        fd = connection.fileno()
        with self.lock:
            if self.websockets.get(fd) is websocket:
                return
        registration_attempted = False
        try:
            websocket.opened()
            with self.lock:
                # Publish only after registration succeeds. A failed opened()
                # or register() must never expose this handler to once().
                registration_attempted = True
                self.poller.register(fd)
                self.websockets[fd] = websocket
        except BaseException:
            with self.lock:
                if registration_attempted:
                    try:
                        self.poller.unregister(fd)
                    except (OSError, ValueError):
                        pass  # Registration failed before adding the fd.
                if self.websockets.get(fd) is websocket:
                    self.websockets.pop(fd, None)
                self.retiring[websocket] = connection
            try:
                self._complete_retirement(websocket, connection)
            except BaseException as cleanup_error:
                logger.error("Failed WebSocket admission cleanup also failed: %s",
                             type(cleanup_error).__name__)
            raise

    def run(self):
        self.running = True
        self.started_running.set()
        try:
            while self.running:
                with self.lock:
                    # EPollPoller.poll is a generator; exhaust it while the
                    # lock still protects registration, not after releasing it.
                    polled = list(self.poller.poll())
                for fd in polled:
                    if not self.running:
                        break
                    with self.lock:
                        handler = self.websockets.get(fd)
                    if handler is None:
                        continue
                    connection = handler.sock
                    keep_open = False
                    if not handler.terminated:
                        try:
                            keep_open = handler.once()
                        except Exception as exc:
                            logger.debug("WebSocket receive failed: %s", type(exc).__name__)
                    if keep_open:
                        continue
                    with self.lock:
                        # Keep failed/in-flight retirement visible to shutdown
                        # even after this fd leaves the active polling pool.
                        self.retiring[handler] = connection
                        if self.websockets.get(fd) is handler:
                            self.websockets.pop(fd, None)
                        try:
                            self.poller.unregister(fd)
                        except (OSError, ValueError):
                            pass  # The peer/socket may already be gone.
                    try:
                        # A close reply can set both terminated flags without
                        # calling closed(). Always complete retirement here.
                        self._complete_retirement(handler, connection)
                    except Exception as exc:
                        logger.error("WebSocket retirement failed: %s", type(exc).__name__)
        finally:
            self.running = False


class _LifecycleWebSocketPlugin(WebSocketPlugin):
    """Own admission, sockets and workers through partial/repeated shutdown."""

    def __init__(self, bus):
        super().__init__(bus)
        self.manager.stop()  # Release the unused, never-started default poller.
        self.manager = _LifecycleWebSocketManager(self._retire_handler)
        self._cleanup_lock = threading.Lock()
        self._cleanup_started = False
        self._cleanup_done = False
        self._cleanup_handlers = None
        self._manager_released = False
        self._heartbeat_thread = None
        self._heartbeat_stop = None

    def start(self):
        with self._cleanup_lock:
            if self._cleanup_started:
                raise RuntimeError("Cannot restart a stopped WebSocket plugin")
            super().start()
            # The upstream loop performs no I/O before setting running=True.
            # This is a thread handshake, not a readiness poll or a timed join.
            self.manager.started_running.wait()
            if not self.manager.is_alive():
                raise RuntimeError("WebSocket manager exited during startup")

    @staticmethod
    def _shutdown_handler(handler):
        try:
            shutdown = getattr(handler, "shutdown_proxy", None)
            if callable(shutdown):
                shutdown()
        finally:
            connection = getattr(handler, "sock", None)
            if connection is not None:
                try:
                    # Keep the fd and stream until the manager has finished any
                    # current once()/poller.unregister call; only unblock I/O.
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass  # Already disconnected/closed.

    @staticmethod
    def _finish_handler(handler, connection=None):
        wait_closed = getattr(handler, "wait_closed", None)
        if callable(wait_closed):
            wait_closed()
        # The manager may have called terminate while responding to shutdown.
        # It clears stream in finally, so never deliver closed() a second time.
        if connection is None:
            connection = getattr(handler, "sock", None)
        try:
            if getattr(handler, "stream", None) is not None:
                handler.terminate()
        finally:
            # ws4py close_connection puts shutdown and close in one try block;
            # an already-disconnected socket can skip close when shutdown fails.
            if connection is not None:
                connection.close()

    def _retire_handler(self, handler, connection):
        self._shutdown_handler(handler)
        self._finish_handler(handler, connection)

    def handle(self, ws_handler, peer_addr):
        # manager.add calls opened() before it takes manager.lock. Serialize the
        # whole admission so cleanup cannot miss an authenticating/connecting WS.
        with self._cleanup_lock:
            if self._cleanup_started:
                connection = ws_handler.sock
                try:
                    self.manager.retire_unregistered(ws_handler, connection)
                except BaseException:
                    retained = dict(self._cleanup_handlers or ())
                    retained[ws_handler] = connection
                    self._cleanup_handlers = tuple(retained.items())
                    self._cleanup_done = False
                    raise
                return
            super().handle(ws_handler, peer_addr)

    def cleanup(self):
        if threading.current_thread() in (self.manager, self._heartbeat_thread):
            raise RuntimeError("WebSocket cleanup cannot join its own worker")
        with self._cleanup_lock:
            if self._cleanup_done:
                return
            self._cleanup_started = True
            if self._heartbeat_stop is not None:
                self._heartbeat_stop.set()
            if self._cleanup_handlers is None:
                with self.manager.lock:
                    self.manager.running = False
                    retained = dict(self.manager.retiring)
                    retained.update((handler, handler.sock)
                                    for handler in self.manager.websockets.values())
                    self._cleanup_handlers = tuple(retained.items())

            errors = []
            for handler, _connection in self._cleanup_handlers:
                try:
                    self._shutdown_handler(handler)
                except Exception as exc:
                    errors.append(exc)
            # No manager lock across joins: its in-flight once() may still need
            # that lock to unregister a socket before it exits. Keep the poller
            # usable until then; upstream stop() releases it immediately.
            if self.manager.ident is not None:
                self.manager.join()
            if self._heartbeat_thread is not None and self._heartbeat_thread.ident is not None:
                self._heartbeat_thread.join()
            if not self._manager_released:
                self.manager.stop()
                self._manager_released = True
            for handler, connection in self._cleanup_handlers:
                try:
                    self._finish_handler(handler, connection)
                except Exception as exc:
                    errors.append(exc)
            if errors:
                # Keep the snapshot after manager.stop clears its pool so a
                # retry still owns sockets/readers whose cleanup did not finish.
                raise RuntimeError("WebSocket connection cleanup failed") from errors[0]
            with self.manager.lock:
                self.manager.retiring.clear()
            self._cleanup_handlers = ()
            self._cleanup_done = True


class PacketWebSocket(WebSocket):
    def opened(self):
        """Called when a WebSocket connection is established"""
        jwt_handler = cherrypy.config.get("jwt_handler")
        token_manager = cherrypy.config.get("token_manager")

        qs = ""
        if hasattr(self, "environ"):
            qs = self.environ.get("QUERY_STRING", "")

        params = parse_qs(qs)
        token = params.get("token", [None])[0]
        client_id = params.get("client_id", [None])[0]

        api_key = self.environ.get("HTTP_X_API_KEY", "") if hasattr(self, "environ") else ""

        if not jwt_handler:
            logger.warning("WebSocket connection rejected: no JWT handler configured")
            self.close(code=1011, reason="server configuration error")
            return

        if not token and not api_key:
            logger.warning("WebSocket connection rejected: missing token")
            self.close(code=1008, reason="unauthorized")
            return

        if token:
            try:
                payload = jwt_handler.verify_jwt(token)
                if payload:
                    if (
                        client_id
                        and payload.get("client_id")
                        and payload.get("client_id") != client_id
                    ):
                        logger.warning("WebSocket connection rejected: client_id mismatch")
                        self.close(code=1008, reason="unauthorized")
                        return
                    self.user = payload.get("sub")
                    _connected_clients.add(self)
                    logger.info(
                        f"WebSocket connected ({self.user or 'unknown user'}). Total clients: {len(_connected_clients)}"
                    )
                    return
            except Exception as e:
                logger.warning(f"WebSocket JWT auth error: {e}")

        api_token = api_key or token
        if api_token and token_manager:
            try:
                token_info = token_manager.verify_token(api_token)
                if token_info:
                    self.user = f"api_token:{token_info.get('name', 'unknown')}"
                    _connected_clients.add(self)
                    logger.info(
                        f"WebSocket connected (API token: {token_info.get('name', 'unknown')}). Total clients: {len(_connected_clients)}"
                    )
                    return
            except Exception as e:
                logger.warning(f"WebSocket API key auth error: {e}")

        logger.warning("WebSocket connection rejected: no valid authentication")
        self.close(code=1008, reason="unauthorized")

    def closed(self, code, reason=None):
        """Called when a WebSocket connection is closed"""
        _connected_clients.discard(self)
        user = getattr(self, "user", "unknown")
        logger.info(
            f"WebSocket disconnected (user: {user}, code: {code}, reason: {reason}). Total clients: {len(_connected_clients)}"
        )

    def received_message(self, message):
        """Handle messages from client"""
        try:
            data = json.loads(str(message))
            if data.get("type") == "ping":
                self.send(json.dumps({"type": "pong"}))
            elif data.get("type") == "pong":
                # Client responded to our ping
                pass
        except Exception as exc:
            logger.debug(f"Ignoring malformed WebSocket message: {exc}")


def broadcast_packet(packet_data: dict):

    if not _connected_clients:
        return

    message = json.dumps({"type": "packet", "data": packet_data})

    for client in list(_connected_clients):
        try:
            client.send(message)
        except Exception as e:
            logger.error(f"WebSocket send error: {e}")
            _connected_clients.discard(client)


def broadcast_stats(stats_data: dict):

    if not _connected_clients:
        return

    message = json.dumps({"type": "stats", "data": stats_data})

    for client in list(_connected_clients):
        try:
            client.send(message)
        except Exception as e:
            logger.error(f"WebSocket send error: {e}")
            _connected_clients.discard(client)


def has_connected_clients() -> bool:
    """Return True when at least one authenticated websocket client is connected."""
    return bool(_connected_clients)


def _heartbeat_loop(stop_event):
    """Send periodic pings until this specific worker generation is stopped."""
    while not stop_event.wait(PING_INTERVAL):
        if not _connected_clients:
            continue

        ping_message = json.dumps({"type": "ping"})

        for client in list(_connected_clients):
            if stop_event.is_set():
                break
            try:
                client.send(ping_message)
            except Exception as e:
                logger.debug(f"Heartbeat ping failed: {e}")
                _connected_clients.discard(client)


def init_websocket():
    """Initialize one plugin and heartbeat generation after draining the old one."""
    global _heartbeat_thread, _heartbeat_running, _heartbeat_stop, _websocket_plugin

    with _lifecycle_lock:
        shutdown_websocket()
        _websocket_plugin = _LifecycleWebSocketPlugin(cherrypy.engine)
        try:
            _websocket_plugin.subscribe()
            cherrypy.tools.websocket = WebSocketTool()
            _heartbeat_stop = threading.Event()
            _heartbeat_thread = threading.Thread(
                target=_heartbeat_loop, args=(_heartbeat_stop,),
                daemon=True, name="packet-websocket-heartbeat",
            )
            _websocket_plugin._heartbeat_stop = _heartbeat_stop
            _websocket_plugin._heartbeat_thread = _heartbeat_thread
            _heartbeat_running = True
            _heartbeat_thread.start()
        except BaseException:
            try:
                shutdown_websocket()
            except BaseException as cleanup_error:
                logger.error(
                    "WebSocket cleanup after failed initialization also failed: %s",
                    type(cleanup_error).__name__,
                )
            raise
        logger.info(f"WebSocket initialized with {PING_INTERVAL}s heartbeat")


def shutdown_websocket():
    """Drain heartbeat and plugin ownership; call outside their worker threads."""
    global _heartbeat_running, _heartbeat_thread, _heartbeat_stop, _websocket_plugin

    with _lifecycle_lock:
        _heartbeat_running = False
        if _heartbeat_stop is not None:
            _heartbeat_stop.set()
        thread = _heartbeat_thread
        if thread is threading.current_thread():
            raise RuntimeError("Cannot join the WebSocket heartbeat from its own thread")
        if _websocket_plugin is not None:
            # Unblock socket I/O before joining the heartbeat; engine.exit can
            # also perform this same idempotent drain before reaching here.
            _websocket_plugin.stop()
            _websocket_plugin.cleanup()
            _websocket_plugin.unsubscribe()
            _websocket_plugin = None
        if thread is not None and thread.ident is not None:
            # No timeout: do not lose an in-flight send or start a second worker
            # while the previous one remains alive. It never awaits the main loop.
            thread.join()
        _heartbeat_thread = None
        _heartbeat_stop = None
        _connected_clients.clear()
