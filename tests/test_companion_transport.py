"""Exercise the actual cross-thread frame handoff without upstream packages."""

import asyncio
import importlib.util
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location(
    "companion_thread_frames",
    Path(__file__).resolve().parents[1]
    / "overlay/pymc_repeater/repeater/companion/thread_frames.py",
)
module = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {
    "openhop_core.companion.constants": SimpleNamespace(MAX_PAYLOAD_SIZE=176),
}):
    spec.loader.exec_module(module)


class Sink:
    def _enqueue_frame(self, data):
        self.received.append((threading.get_ident(), data))


class Server(module.ThreadFramesMixin, Sink):
    _WRITE_QUEUE_MAXSIZE = 3

    def __init__(self):
        self._init_thread_frames()
        self._frame_loop = asyncio.get_running_loop()
        self._closing = False
        self._client_writer = SimpleNamespace(is_closing=lambda: False)
        self._write_queue = object()
        self.received = []


class ThreadFrameTests(unittest.IsolatedAsyncioTestCase):
    def produce_without_yielding_loop(self, server, frames):
        worker = threading.Thread(target=lambda: [server._enqueue_frame(frame) for frame in frames])
        worker.start()
        worker.join(timeout=1)
        self.assertFalse(worker.is_alive())

    async def test_notifications_are_bounded_ordered_and_delivered_on_owner_thread(self):
        server = Server()
        with self.assertLogs("companion_thread_frames", level="WARNING"):
            self.produce_without_yielding_loop(server, [b"one", b"two", b"three", b"overflow"])
        self.assertFalse(server.received)
        self.assertEqual(len(server._thread_frames), 3)
        await asyncio.sleep(0)
        self.assertEqual(server.received, [(threading.get_ident(), data)
                                          for data in (b"one", b"two", b"three")])
        self.assertFalse(server._thread_frames)

    async def test_pending_frames_do_not_cross_connection_replacement(self):
        server = Server()
        self.produce_without_yielding_loop(server, [b"old connection"])
        server._client_writer = SimpleNamespace(is_closing=lambda: False)
        server._write_queue = object()
        await asyncio.sleep(0)
        self.assertFalse(server.received)
        await asyncio.to_thread(server._enqueue_frame, b"new connection")
        await asyncio.sleep(0)
        self.assertEqual(server.received, [(threading.get_ident(), b"new connection")])

    async def test_shutdown_discards_pending_notifications(self):
        server = Server()
        self.produce_without_yielding_loop(server, [b"pending"])
        server._closing = True
        server._discard_thread_frames()
        await asyncio.sleep(0)
        await asyncio.to_thread(server._enqueue_frame, b"late")
        self.assertFalse(server.received)
        self.assertFalse(server._thread_frames)


if __name__ == "__main__":
    unittest.main()
