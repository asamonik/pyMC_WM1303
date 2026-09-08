"""Bounded ownership for companion PATH discovery requests."""

import asyncio
import logging
import random
import time
from dataclasses import dataclass

from openhop_core.companion.constants import DEFAULT_RESPONSE_TIMEOUT_MS
from openhop_core.companion.models import SentResult
from openhop_core.protocol import PacketBuilder
from openhop_core.protocol.constants import REQ_TYPE_GET_TELEMETRY_DATA, TELEM_PERM_BASE

logger = logging.getLogger(__name__)

MAX_PENDING_PATH_DISCOVERIES = 64


@dataclass
class _DiscoveryReservation:
    # None reserves the tag during TX; the advertised response window starts
    # only when TX succeeds. Object identity also protects cleanup from reuse.
    deadline: float | None = None
    claimed: bool = False
    owner: object = None


class PathDiscoveryRequestsMixin:
    def _prune_path_discovery_requests(self):
        now = time.monotonic()
        for tag, reservation in tuple(self._path_discovery_deadlines.items()):
            expired = (reservation.deadline is not None and now >= reservation.deadline
                       and not reservation.claimed)
            if expired or tag not in self._pending_discovery_tags:
                self._path_discovery_deadlines.pop(tag, None)
                self._pending_discovery_tags.discard(tag)

    def _claim_path_discovery(self, tag):
        self._prune_path_discovery_requests()
        reservation = self._path_discovery_deadlines.get(tag)
        if reservation is None or tag not in self._pending_discovery_tags:
            return False
        if not reservation.claimed:
            # The retained core handler does not yield between the authenticated
            # path predicate, skipped reciprocal, parsing and consuming the tag.
            # Hold one decision across that event-loop turn, even at expiry.
            # If parsing rejects the response, release next turn instead of
            # leaving an unconsumable claimed request permanently reserved.
            loop = asyncio.get_running_loop()
            reservation.claimed = True
            loop.call_soon(self._release_path_discovery_claim, tag, reservation)
        return True

    def _release_path_discovery_claim(self, tag, reservation):
        if self._path_discovery_deadlines.get(tag) is reservation:
            reservation.claimed = False
            self._prune_path_discovery_requests()

    async def send_path_discovery_req(self, pub_key):
        """Register the discovery before an asynchronous send can receive its reply."""
        self._prune_path_discovery_requests()
        if self._stop_task is not None:
            return SentResult(success=False, error="send_failed")
        contact = self.contacts.get_by_key(pub_key)
        proxy = self.contacts.get_proxy_by_key(pub_key)
        if contact is None or proxy is None:
            return SentResult(success=False, error="not_found")
        if len(self._pending_discovery_tags) >= MAX_PENDING_PATH_DISCOVERIES:
            return SentResult(success=False, error="send_failed")

        tag = None
        reservation = None
        success = False
        try:
            inv_perm = 0xFF & ~TELEM_PERM_BASE
            req_data = bytes([inv_perm, 0, 0, 0]) + random.getrandbits(32).to_bytes(4, "little")
            packet, tag = PacketBuilder.create_protocol_request(
                contact=proxy,
                local_identity=self._identity,
                protocol_code=REQ_TYPE_GET_TELEMETRY_DATA,
                data=req_data,
                route_type="flood",
            )
            # The core generator is locked and strictly increasing. Still reject
            # a duplicate rather than overwrite or later clear an earlier owner.
            if (type(tag) is not int or not 0 <= tag <= 0xFFFFFFFF
                    or tag in self._pending_discovery_tags):
                return SentResult(success=False, error="send_failed")
            self._apply_flood_scope(packet)
            self._apply_path_hash_mode(packet)
            reservation = _DiscoveryReservation()
            self._path_discovery_deadlines[tag] = reservation
            self._pending_discovery_tags.add(tag)
            reservation.owner = self._notify_request_registered(
                "path", tag, DEFAULT_RESPONSE_TIMEOUT_MS / 1000.0,
            )
            success = bool(await self._send_packet(packet, wait_for_ack=False))
            if (success and self._path_discovery_deadlines.get(tag) is reservation
                    and tag in self._pending_discovery_tags):
                reservation.deadline = time.monotonic() + DEFAULT_RESPONSE_TIMEOUT_MS / 1000.0
            return SentResult(
                success=success,
                is_flood=True,
                expected_ack=tag,
                timeout_ms=DEFAULT_RESPONSE_TIMEOUT_MS,
                error=None if success else "send_failed",
            )
        except Exception as exc:
            logger.warning("Path discovery send failed (%s)", type(exc).__name__)
            return SentResult(success=False, error="send_failed")
        finally:
            # Also clean up cancellation. A response may already have consumed
            # this entry while TX was awaited; never re-register it on success.
            if (not success and reservation is not None
                    and self._path_discovery_deadlines.get(tag) is reservation):
                self._path_discovery_deadlines.pop(tag, None)
                self._pending_discovery_tags.discard(tag)

    async def _try_handle_path_discovery(self, tag_bytes, path_info):
        tag = int.from_bytes(tag_bytes, "little")
        reservation = self._path_discovery_deadlines.get(tag)
        owner = reservation.owner if reservation is not None else None
        self._prune_path_discovery_requests()
        try:
            with self._request_response_origin(owner):
                return await super()._try_handle_path_discovery(tag_bytes, path_info)
        finally:
            # The core consumes the tag before awaiting the unchanged callback.
            if tag not in self._pending_discovery_tags:
                self._path_discovery_deadlines.pop(tag, None)
