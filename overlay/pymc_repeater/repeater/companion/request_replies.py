"""Connection-owned binary and PATH replies, including responses before SENT."""

import asyncio
import logging
import math
import time
from dataclasses import dataclass

from openhop_core.companion.constants import (
    BINARY_REQ_TIMEOUT_HINT_MS,
    MAX_PAYLOAD_SIZE,
    PUSH_CODE_BINARY_RESPONSE,
    PUSH_CODE_PATH_DISCOVERY_RESPONSE,
)
from openhop_core.protocol.packet_utils import PathUtils

logger = logging.getLogger(__name__)

MAX_PENDING_REQUEST_REPLIES = 128


@dataclass
class _RequestReply:
    kind: str
    tag: int
    writer: object
    ready: asyncio.Event
    timeout_seconds: float
    deadline: float | None = None
    active: bool = True
    sent: bool = False
    admitted: bool = False
    received: bool = False


class RequestRepliesMixin:
    def _discard_request_reply(self, owner):
        owner.active = False
        owner.writer = None
        key = (owner.kind, owner.tag)
        if self._request_replies.get(key) is owner:
            del self._request_replies[key]

    def _prune_request_replies(self):
        now = time.monotonic()
        for owner in tuple(self._request_replies.values()):
            # Once received on time, a reply may wait for SENT/output space.
            # Keep that delivery in the bound until it finishes or disconnects.
            expired = (not owner.received and owner.deadline is not None
                       and now >= owner.deadline)
            if expired or not self._command_reply_writer_is_current(owner.writer):
                self._discard_request_reply(owner)

    def _register_request_reply(self, kind, tag, timeout_seconds):
        try:
            task = asyncio.current_task()
        except RuntimeError:
            task = None
        capture = self._command_reply_captures.get(task)
        if capture is None:
            # Web/direct bridge requests retain their ordinary broadcast path.
            return None
        self._prune_request_replies()
        if (capture["failed"] or not self._command_reply_writer_is_current(capture["writer"])
                or len(self._request_replies) >= MAX_PENDING_REQUEST_REPLIES):
            raise RuntimeError("Companion request reply capacity unavailable")
        timeout_seconds = float(timeout_seconds)
        if (kind not in ("binary", "path") or type(tag) is not int
                or not 0 <= tag <= 0xFFFFFFFF or not math.isfinite(timeout_seconds)
                or timeout_seconds <= 0 or (kind, tag) in self._request_replies):
            raise ValueError("Invalid companion request registration")
        owner = _RequestReply(kind, tag, capture["writer"], capture["ready"], timeout_seconds)
        self._request_replies[(kind, tag)] = owner
        capture.setdefault("request_replies", []).append(owner)
        return owner

    def _write_sent_result(self, result, *, default_timeout_ms=BINARY_REQ_TIMEOUT_HINT_MS,
                           own_binary_tag=False):
        try:
            task = asyncio.current_task()
        except RuntimeError:
            task = None
        capture = self._command_reply_captures.get(task)
        if capture is not None:
            for owner in capture.get("request_replies", ()):
                if owner.active and owner.tag == result.expected_ack and result.success:
                    timeout_ms = (result.timeout_ms if result.timeout_ms is not None
                                  else default_timeout_ms)
                    owner.sent = True
                    owner.deadline = time.monotonic() + max(
                        owner.timeout_seconds, timeout_ms / 1000.0,
                    )
        # Registration already owns these tags before TX. Never populate the
        # core's unbounded, server-lifetime set after the awaited send.
        return super()._write_sent_result(
            result, default_timeout_ms=default_timeout_ms, own_binary_tag=False,
        )

    def _command_reply_capture_finished(self, capture):
        super()._command_reply_capture_finished(capture)
        for owner in capture.get("request_replies", ()):
            owner.admitted = owner.active and owner.sent and capture["flushed"]
            if not owner.admitted:
                self._discard_request_reply(owner)

    def _queue_owned_request_reply(self, owner, frame):
        self._prune_request_replies()
        if (not isinstance(owner, _RequestReply) or not owner.active or owner.received
                or self._request_replies.get((owner.kind, owner.tag)) is not owner):
            return
        if not isinstance(frame, bytes) or len(frame) > MAX_PAYLOAD_SIZE:
            logger.warning("Companion request response has invalid frame size")
            self._discard_request_reply(owner)
            return
        owner.received = True
        work = self._deliver_owned_request_reply(owner, frame)
        try:
            task = self.bridge._spawn_background_task(work, "companion owned request reply")
        except BaseException:
            work.close()
            self._discard_request_reply(owner)
            raise
        # Cancellation before the first step never enters the coroutine's
        # finally block. This callback releases that ticket as well.
        task.add_done_callback(lambda _task: self._discard_request_reply(owner))

    async def _deliver_owned_request_reply(self, owner, frame):
        try:
            # Do not wait here from inside RX: an inline response can occur on
            # the initiating TX chain. Only this separate, bridge-owned task
            # waits for SENT admission, and it never owns/cancels the RF work.
            await owner.ready.wait()
            if (owner.active and owner.admitted
                    and self._command_reply_writer_is_current(owner.writer)):
                await self._enqueue_response_frame(frame)
        finally:
            self._discard_request_reply(owner)

    def _on_binary_response(self, tag_bytes, response_data, parsed=None, request_type=None):
        owner = self.bridge._get_request_response_owner()
        if owner is None:
            # In particular, retain the intentional unowned web-region push.
            return super()._on_binary_response(tag_bytes, response_data, parsed, request_type)
        # Opaque origin survives disconnect/expiry in bridge metadata. A stale
        # TCP-owned regions response must not turn into an unowned broadcast.
        if (not isinstance(owner, _RequestReply) or owner.kind != "binary"
                or not isinstance(tag_bytes, bytes) or len(tag_bytes) != 4
                or int.from_bytes(tag_bytes, "little") != owner.tag
                or not isinstance(response_data, bytes)):
            return
        self._queue_owned_request_reply(
            owner, bytes([PUSH_CODE_BINARY_RESPONSE, 0]) + tag_bytes + response_data,
        )

    def _on_path_discovery_response(
        self, tag_bytes, contact_pubkey, out_len_byte, out_path, in_len_byte, in_path,
    ):
        owner = self.bridge._get_request_response_owner()
        if owner is None:
            return super()._on_path_discovery_response(
                tag_bytes, contact_pubkey, out_len_byte, out_path, in_len_byte, in_path,
            )
        if (not isinstance(owner, _RequestReply) or owner.kind != "path"
                or not isinstance(tag_bytes, bytes) or len(tag_bytes) != 4
                or int.from_bytes(tag_bytes, "little") != owner.tag
                or not isinstance(contact_pubkey, bytes) or len(contact_pubkey) != 32
                or not isinstance(out_path, bytes) or not isinstance(in_path, bytes)):
            return
        for encoded, path in ((out_len_byte, out_path), (in_len_byte, in_path)):
            if (type(encoded) is not int or not 0 <= encoded <= 255
                    or not PathUtils.is_valid_path_len(encoded)
                    or len(path) != PathUtils.get_path_byte_len(encoded)):
                return
        frame = (bytes([PUSH_CODE_PATH_DISCOVERY_RESPONSE, 0]) + contact_pubkey[:6]
                 + bytes([out_len_byte]) + out_path + bytes([in_len_byte]) + in_path)
        self._queue_owned_request_reply(owner, frame)

    async def _cleanup_client(self, writer, write_queue, writer_task, disconnect_reason):
        for owner in tuple(self._request_replies.values()):
            if owner.writer is writer:
                self._discard_request_reply(owner)
        return await super()._cleanup_client(writer, write_queue, writer_task, disconnect_reason)
