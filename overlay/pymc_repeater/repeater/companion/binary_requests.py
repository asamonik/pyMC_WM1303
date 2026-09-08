"""Request ownership around the retained binary/ANON builders and parser."""

import asyncio
import math
import time
from contextlib import contextmanager
from dataclasses import dataclass, field

from openhop_core.companion.models import SentResult

MAX_PENDING_BINARY_REQUESTS = 128


@dataclass
class _BinaryRequest:
    metadata: dict
    timeout_seconds: float
    owner: object = None
    deadline: float | None = None


@dataclass
class _BinarySend:
    registrations: list = field(default_factory=list)
    collisions: dict = field(default_factory=dict)


class BinaryRequestsMixin:
    def _init_request_ownership(self):
        self._request_registration_handler = None
        self._binary_request_metadata = {}
        self._binary_send_registrations = {}
        self._request_response_origins = {}

    def set_request_registration_handler(self, handler):
        """Bind a synchronous pre-TX hook; rejection must raise, not return None."""
        self._request_registration_handler = handler

    def _notify_request_registered(self, kind, tag, timeout_seconds):
        handler = self._request_registration_handler
        return handler(kind, tag, timeout_seconds) if handler is not None else None

    @staticmethod
    def _request_task():
        try:
            return asyncio.current_task()
        except RuntimeError:
            return None

    @contextmanager
    def _request_response_origin(self, owner):
        task = self._request_task()
        if task is None:
            yield
            return
        missing = object()
        previous = self._request_response_origins.get(task, missing)
        self._request_response_origins[task] = owner
        try:
            yield
        finally:
            if previous is missing:
                self._request_response_origins.pop(task, None)
            else:
                self._request_response_origins[task] = previous

    def _get_request_response_owner(self):
        return self._request_response_origins.get(self._request_task())

    async def send_binary_req(self, pub_key, data, timeout_seconds=15.0):
        return await self._send_registered_binary(
            super().send_binary_req, pub_key, data, timeout_seconds,
        )

    async def send_anon_req(self, pub_key, data, timeout_seconds=15.0):
        return await self._send_registered_binary(
            super().send_anon_req, pub_key, data, timeout_seconds,
        )

    async def _send_registered_binary(self, send, pub_key, data, timeout_seconds):
        if self._stop_task is not None:
            return SentResult(success=False, error="send_failed")
        task = asyncio.current_task()
        previous = self._binary_send_registrations.get(task)
        state = _BinarySend()
        self._binary_send_registrations[task] = state
        result = None
        try:
            # Stay in the calling task: the TCP registration hook identifies
            # its originating command capture by exact task, not global state.
            result = await send(pub_key, data, timeout_seconds=timeout_seconds)
            return result
        finally:
            # Core's send-error branch unconditionally pops its generated tag.
            # On duplicate rejection restore only the pre-existing entry that
            # branch removed. There is no await from rejection through here.
            for tag, metadata in state.collisions.items():
                if tag not in self._pending_binary_requests:
                    self._pending_binary_requests[tag] = metadata
            success = result is not None and result.success
            for tag, request in state.registrations:
                if self._binary_request_metadata.get(tag) is not request:
                    continue
                if self._pending_binary_requests.get(tag) is not request.metadata:
                    self._binary_request_metadata.pop(tag, None)
                elif success:
                    # A queued TX can take longer than the response window.
                    # Start the original requested lifetime after successful TX.
                    request.deadline = time.monotonic() + request.timeout_seconds
                else:
                    self._pending_binary_requests.pop(tag, None)
                    self._binary_request_metadata.pop(tag, None)
            if self._binary_send_registrations.get(task) is state:
                if previous is None:
                    self._binary_send_registrations.pop(task, None)
                else:
                    self._binary_send_registrations[task] = previous

    def register_binary_request(
        self, tag_hex, request_type, timeout_seconds, pubkey_prefix="", context=None,
    ):
        tag_bytes = bytes.fromhex(tag_hex)
        if len(tag_bytes) != 4:
            raise ValueError("Binary request tag must contain four bytes")
        tag_hex = tag_bytes.hex()
        lifetime = float(timeout_seconds)
        if not math.isfinite(lifetime):
            raise ValueError("Binary request timeout must be finite")
        self.cleanup_expired_binary_requests()
        state = self._binary_send_registrations.get(self._request_task())
        if tag_hex in self._pending_binary_requests:
            if state is not None:
                state.collisions[tag_hex] = self._pending_binary_requests[tag_hex]
            raise ValueError("Binary request tag is already pending")
        if len(self._pending_binary_requests) >= MAX_PENDING_BINARY_REQUESTS:
            raise RuntimeError("Binary request table is full")

        # Preserve request type, prefix, ANON subtype and all core parsing data.
        super().register_binary_request(
            tag_hex, request_type, timeout_seconds, pubkey_prefix, context,
        )
        metadata = self._pending_binary_requests[tag_hex]
        request = _BinaryRequest(metadata, lifetime)
        self._binary_request_metadata[tag_hex] = request
        if state is not None:
            state.registrations.append((tag_hex, request))
        else:
            # Preserve standalone registrations with no asynchronous send phase.
            request.deadline = time.monotonic() + lifetime
        try:
            request.owner = self._notify_request_registered(
                "binary", int.from_bytes(tag_bytes, "little"), lifetime,
            )
        except BaseException:
            if self._pending_binary_requests.get(tag_hex) is metadata:
                self._pending_binary_requests.pop(tag_hex, None)
            if self._binary_request_metadata.get(tag_hex) is request:
                self._binary_request_metadata.pop(tag_hex, None)
            raise

    def cleanup_expired_binary_requests(self):
        now = time.monotonic()
        for tag, request in tuple(self._binary_request_metadata.items()):
            if self._pending_binary_requests.get(tag) is not request.metadata:
                self._binary_request_metadata.pop(tag, None)
            elif request.deadline is not None and now >= request.deadline:
                self._pending_binary_requests.pop(tag, None)
                self._binary_request_metadata.pop(tag, None)
        # Any externally installed, untracked metadata keeps the core wall-clock
        # expiration contract. Tracked in-flight requests must not use it.
        wall_now = time.time()
        for tag, metadata in tuple(self._pending_binary_requests.items()):
            if tag not in self._binary_request_metadata and wall_now > metadata["expires_at"]:
                self._pending_binary_requests.pop(tag, None)

    async def _on_binary_response(self, tag_bytes, response_data, path_info=None):
        tag = tag_bytes.hex()
        request = self._binary_request_metadata.get(tag)
        owner = (request.owner if request is not None
                 and self._pending_binary_requests.get(tag) is request.metadata else None)
        try:
            # Capture before core cleanup/pop. Even an expired TCP-owned reply
            # must not become an unowned web-region broadcast on reconnection.
            with self._request_response_origin(owner):
                return await super()._on_binary_response(tag_bytes, response_data, path_info)
        finally:
            if (request is not None and self._binary_request_metadata.get(tag) is request
                    and self._pending_binary_requests.get(tag) is not request.metadata):
                self._binary_request_metadata.pop(tag, None)

    def _clear_request_ownership(self):
        """Clear after external producers and bridge-owned work have drained."""
        self._request_registration_handler = None
        self._pending_binary_requests.clear()
        self._binary_request_metadata.clear()
        self._binary_send_registrations.clear()
        self._request_response_origins.clear()
