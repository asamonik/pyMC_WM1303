"""Deliver radio-thread notifications on the TCP listener's owning loop."""

import asyncio
import logging
import threading
from collections import deque

from openhop_core.companion.constants import MAX_PAYLOAD_SIZE

logger = logging.getLogger(__name__)


class ThreadFramesMixin:
    def _init_thread_frames(self):
        self._frame_loop = None
        self._thread_frame_lock = threading.Lock()
        self._thread_frames = deque()
        self._thread_frame_scheduled = False

    def _enqueue_frame(self, data):
        # This entry point is also called by the WM1303 UDP/RX thread. asyncio
        # queues may only be touched on their owning loop, including put_nowait.
        loop = self._frame_loop
        writer, queue = self._client_writer, self._write_queue
        if (self._closing or loop is None or writer is None or queue is None
                or writer.is_closing() or len(data) > MAX_PAYLOAD_SIZE):
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            return super()._enqueue_frame(data)
        with self._thread_frame_lock:
            if self._closing:
                return
            if len(self._thread_frames) >= self._WRITE_QUEUE_MAXSIZE:
                logger.warning("Companion thread notification queue is full")
                return
            self._thread_frames.append((writer, queue, bytes(data)))
            if self._thread_frame_scheduled:
                return
            self._thread_frame_scheduled = True
            try:
                loop.call_soon_threadsafe(self._drain_thread_frames)
            except RuntimeError:
                self._thread_frames.clear()
                self._thread_frame_scheduled = False

    def _drain_thread_frames(self):
        with self._thread_frame_lock:
            pending = tuple(self._thread_frames)
            self._thread_frames.clear()
            self._thread_frame_scheduled = False
        for writer, queue, data in pending:
            # A callback queued before disconnect must never reach its successor.
            if (not self._closing and writer is self._client_writer
                    and queue is self._write_queue and not writer.is_closing()):
                super()._enqueue_frame(data)

    def _discard_thread_frames(self):
        with self._thread_frame_lock:
            self._thread_frames.clear()
