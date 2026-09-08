"""Authenticated raw-byte WebSocket proxy to a live companion TCP listener."""

import logging
import socket
import threading
from urllib.parse import parse_qs

import cherrypy
from ws4py.websocket import WebSocket

logger = logging.getLogger("CompanionWSProxy")

_daemon = None


def set_daemon(instance):
    global _daemon
    _daemon = instance


class CompanionFrameWebSocket(WebSocket):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._proxy_stop = threading.Event()
        self._state_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._tcp = None
        self._reader = None
        self._companion_name = "?"

    def _reject(self, code, reason):
        try:
            self.close(code=code, reason=reason)
        finally:
            # Keep the WebSocket fd open: ws4py registers it after opened().
            self.shutdown_proxy()

    def opened(self):
        """Retain JWT authentication; connect only to an active local listener."""
        jwt_handler = cherrypy.config.get("jwt_handler")
        params = parse_qs((self.environ or {}).get("QUERY_STRING", ""))
        token = params.get("token", [None])[0]
        companion_name = params.get("companion_name", [None])[0]
        if not jwt_handler:
            logger.warning("Connection rejected: no JWT handler configured")
            self._reject(1011, "server configuration error")
            return
        if not token:
            self._reject(1008, "unauthorized")
            return
        try:
            payload = jwt_handler.verify_jwt(token)
        except Exception as exc:
            logger.warning("WebSocket authentication failed (%s)", type(exc).__name__)
            self._reject(1008, "unauthorized")
            return
        if not payload:
            self._reject(1008, "unauthorized")
            return
        if not companion_name:
            self._reject(1008, "missing companion_name")
            return

        endpoint = self._resolve_tcp_endpoint(companion_name)
        if endpoint is None:
            self._reject(1008, "companion not found")
            return
        family, address = endpoint
        self._companion_name = companion_name
        try:
            with self._state_lock:
                if self._proxy_stop.is_set():
                    return
                tcp = socket.socket(family, socket.SOCK_STREAM)
                # Publish before connect so any shutdown also owns this socket.
                self._tcp = tcp
            tcp.settimeout(5.0)
            tcp.connect(address)
            tcp.settimeout(None)
        except Exception as exc:
            logger.warning("Companion TCP connect failed for %r (%s)",
                           companion_name, type(exc).__name__)
            self._reject(1011, "TCP connect failed")
            return

        try:
            with self._state_lock:
                if self._proxy_stop.is_set():
                    return
                self._reader = threading.Thread(
                    target=self._tcp_to_ws, args=(tcp,), daemon=True,
                    name=f"ws-tcp-{companion_name}",
                )
                # A drain cannot see an unstarted reader that will start later.
                self._reader.start()
        except Exception as exc:
            logger.warning("Companion proxy reader failed to start (%s)", type(exc).__name__)
            self._reject(1011, "proxy reader failed")
            return
        logger.info("Companion WS opened: user=%s, companion=%s, tcp=%s",
                    payload.get("sub", "unknown"), companion_name, address)

    def _resolve_tcp_endpoint(self, companion_name):
        """Return (socket family, address) from the actual serving socket.

        Desired configuration may contain staged changes or omit a hot-created
        companion. The live bridge/listener is authoritative, including an
        OS-assigned port and IPv6 scope ID.
        """
        daemon = _daemon
        if daemon is None or getattr(daemon, "_shutdown_started", False):
            return None
        manager = getattr(daemon, "identity_manager", None)
        if manager is None:
            return None
        entry = manager.get_identity_by_name(companion_name)
        if entry is None or entry[2] != "companion":
            return None
        public_key = entry[0].get_public_key()
        bridge = getattr(daemon, "companion_bridges", {}).get(public_key[0])
        if bridge is None or bridge.get_public_key() != public_key:
            return None
        for frame_server in tuple(getattr(daemon, "companion_frame_servers", ())):
            if frame_server.bridge is not bridge or frame_server._closing:
                continue
            server = frame_server._server
            if server is None or not server.is_serving():
                continue
            for listener in server.sockets or ():
                try:
                    address = listener.getsockname()
                except OSError:
                    # A listener may close concurrently with HTTP admission.
                    continue
                if listener.family == socket.AF_INET:
                    host, port = address
                    if host == "0.0.0.0":
                        host = "127.0.0.1"
                    return socket.AF_INET, (host, port)
                if listener.family == socket.AF_INET6:
                    host, port, flowinfo, scope_id = address
                    if host == "::":
                        host = "::1"
                    return socket.AF_INET6, (host, port, flowinfo, scope_id)
        return None

    def _write(self, data):
        # Reader data and manager pong/close frames must not interleave writes.
        # Shutdown never takes this lock, so it can unblock a stalled send.
        with self._write_lock:
            if self._proxy_stop.is_set():
                raise OSError("Companion proxy is stopping")
            return super()._write(data)

    def received_message(self, message):
        with self._state_lock:
            tcp = self._tcp
        if tcp is None or self._proxy_stop.is_set():
            return
        try:
            data = message.data
            if isinstance(data, str):
                data = data.encode("latin-1")
            tcp.sendall(data)
        except Exception as exc:
            if not self._proxy_stop.is_set():
                logger.warning("WS to TCP send failed for %r (%s)",
                               self._companion_name, type(exc).__name__)
            self.shutdown_proxy()

    def _tcp_to_ws(self, tcp):
        try:
            while not self._proxy_stop.is_set():
                data = tcp.recv(4096)
                if not data:
                    break
                self.send(data, binary=True)
        except Exception as exc:
            if not self._proxy_stop.is_set():
                logger.warning("TCP to WS reader failed for %r (%s)",
                               self._companion_name, type(exc).__name__)
        finally:
            self.shutdown_proxy()

    def shutdown_proxy(self):
        """Disarm and unblock both directions, without clearing ws4py state."""
        with self._state_lock:
            self._proxy_stop.set()
            tcp = self._tcp
            try:
                if tcp is not None:
                    try:
                        tcp.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    finally:
                        tcp.close()
                    self._tcp = None
            finally:
                # Keep this fd for the manager's pending poll/unregister
                # operation. Always unblock WS, even if TCP close failed.
                # terminate() closes it after the manager and reader drain.
                sock = self.sock
                if sock is not None:
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass

    def wait_closed(self):
        with self._state_lock:
            reader = self._reader
        if reader is threading.current_thread():
            raise RuntimeError("Cannot join the companion proxy reader from itself")
        if reader is not None and reader.ident is not None:
            reader.join()

    def closed(self, code, reason=None):
        self.shutdown_proxy()
        # Normal manager removal also drains; it may precede plugin snapshotting.
        self.wait_closed()
        logger.info("Companion WS closed: companion=%s, code=%s, reason=%s",
                    self._companion_name, code, reason)

    def close_connection(self):
        # ws4py skips close() when shutdown() raises. Always attempt both.
        sock = self.sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            finally:
                sock.close()
            self.sock = None
