"""Bound complete inbound frames without cancelling admitted command work."""

import asyncio
import logging
import struct

from openhop_core.companion.constants import FRAME_INBOUND_PREFIX, MAX_FRAME_SIZE

logger = logging.getLogger(__name__)


class FrameReadMixin:
    async def _read_one_frame(self, reader):
        prefix = await reader.read(1)
        if not prefix:
            return "empty_read", None
        if prefix[0] != FRAME_INBOUND_PREFIX:
            logger.warning("Invalid companion frame prefix: 0x%02x", prefix[0])
            return "invalid_prefix", None
        length = struct.unpack("<H", await reader.readexactly(2))[0]
        if length > MAX_FRAME_SIZE:
            logger.warning("Companion frame too long: %s", length)
            return "frame_too_long", None
        return None, await reader.readexactly(length)

    async def _read_client_frames(self, reader, writer_task):
        while True:
            try:
                # The same timeout covers prefix, length and body. Applying it
                # only to the prefix leaves an incomplete frame waiting forever.
                reason, payload = await asyncio.wait_for(
                    self._read_one_frame(reader), timeout=self._client_idle_timeout_sec,
                )
            except asyncio.TimeoutError:
                return "idle_timeout"
            if reason == "invalid_prefix":
                continue
            if reason is not None:
                return reason
            # Never include command execution in the read timeout: cancelling
            # an admitted command could orphan a still-running SQLite worker.
            await self._handle_cmd(payload)
            if writer_task.done():
                if not writer_task.cancelled():
                    error = writer_task.exception()
                    if error is not None:
                        logger.warning("Companion writer failed (%s)", type(error).__name__)
                return "writer_failed"
