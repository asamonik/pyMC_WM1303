import asyncio
import copy
import logging
import time

from openhop_core.node.handlers.ack import AckHandler
from openhop_core.node.handlers.advert import AdvertHandler
from openhop_core.node.handlers.control import ControlHandler
from openhop_core.node.handlers.group_text import GroupTextHandler
from openhop_core.node.handlers.login_response import LoginResponseHandler
from openhop_core.node.handlers.login_server import LoginServerHandler
from openhop_core.node.handlers.multipart import MultipartAckHandler
from openhop_core.node.handlers.path import PathHandler
from openhop_core.node.handlers.protocol_request import ProtocolRequestHandler
from openhop_core.node.handlers.protocol_response import ProtocolResponseHandler
from openhop_core.node.handlers.text import TextMessageHandler
from openhop_core.node.handlers.trace import TraceHandler
from openhop_core.protocol.constants import (
    PAYLOAD_TYPE_GRP_DATA,
    PAYLOAD_TYPE_MULTIPART,
    PAYLOAD_TYPE_RAW_CUSTOM,
    PH_ROUTE_MASK,
    PH_TYPE_MASK,
    PH_TYPE_SHIFT,
    ROUTE_TYPE_DIRECT,
    ROUTE_TYPE_TRANSPORT_DIRECT,
)

logger = logging.getLogger("PacketRouter")

# Deliver PATH and protocol-response (PATH) to companion at most once per logical packet
# so the client is not spammed with duplicate telemetry when the mesh delivers multiple copies.
_COMPANION_DEDUPE_TTL_SEC = 60.0
_COMPANION_DEDUPE_MAX_ENTRIES = 1024


def _companion_dedup_key(packet) -> str | None:
    """Return a stable key for companion delivery deduplication, or None if not available."""
    try:
        return packet.calculate_packet_hash().hex().upper()
    except Exception:
        return None


def _is_direct_final_hop(packet) -> bool:
    """True if packet is DIRECT (or TRANSPORT_DIRECT) with empty path — we're the final destination."""
    route = getattr(packet, "header", 0) & PH_ROUTE_MASK
    if route != ROUTE_TYPE_DIRECT and route != ROUTE_TYPE_TRANSPORT_DIRECT:
        return False
    path = getattr(packet, "path", None)
    return not path or len(path) == 0


class PacketRouter:

    def __init__(self, daemon_instance):
        self.daemon = daemon_instance
        self.queue = asyncio.Queue(maxsize=500)
        self.running = False
        self.router_task = None
        # Serialize injects so one local TX completes before the next is processed
        self._inject_lock = asyncio.Lock()
        # (Packet hash, bridge identity) -> expiry/result. A duplicate retains
        # authenticated ownership; a failed recipient must remain retryable.
        self._companion_delivered = {}
        self._companion_delivery_results = {}
        # Entries are [lock, users], including waiters. Remove only after the
        # final user leaves, so concurrent copies cannot acquire different locks.
        self._companion_delivery_locks = {}
        # Safety valve: cap the number of _route_packet tasks sleeping concurrently.
        # LoRa's airtime budget naturally limits throughput, but burst arrivals
        # (multi-hop amplification, collision retries) can stack many sleeping
        # delay tasks before the duty-cycle gate fires.  30 is very generous for
        # any realistic LoRa network but protects against pathological scenarios
        # (e.g. a busy bridge node during a mesh-wide flood) exhausting memory or
        # starving the event loop.
        self._in_flight: int = 0
        self._max_in_flight: int = 30
        # Live set of in-flight tasks — kept in sync with _in_flight via the
        # done-callback.  Used exclusively for shutdown drain; the integer
        # counter is used for the cap check (faster, single source of truth).
        self._route_tasks: set = set()
        # Total packets dropped because the cap was reached.  Exposed in logs
        # at shutdown so operators know whether the cap is actually firing.
        self._cap_drop_count: int = 0

    async def start(self):
        self.running = True
        self.router_task = asyncio.create_task(self._process_queue())
        logger.info("Packet router started")
    
    async def stop(self):
        self.running = False
        if self.router_task:
            self.router_task.cancel()
            try:
                await self.router_task
            except asyncio.CancelledError:
                pass

        # Drain in-flight tasks gracefully, then cancel any that outlast the
        # timeout.  This mirrors what the old _route_tasks set enabled and gives
        # in-progress packets a fair chance to finish (e.g. their TX delay sleep
        # + send) before the process exits.
        if self._route_tasks:
            pending_snapshot = set(self._route_tasks)
            logger.info(
                "Draining %d in-flight route task(s) (5 s timeout)...",
                len(pending_snapshot),
            )
            _, still_pending = await asyncio.wait(pending_snapshot, timeout=5.0)
            if still_pending:
                logger.warning(
                    "Cancelling %d route task(s) that did not finish within the shutdown timeout",
                    len(still_pending),
                )
                for task in still_pending:
                    task.cancel()
                await asyncio.gather(*still_pending, return_exceptions=True)

        if self._cap_drop_count:
            logger.warning(
                "In-flight cap dropped %d packet(s) during this session — "
                "consider raising _max_in_flight if this is frequent",
                self._cap_drop_count,
            )
        logger.info("Packet router stopped")

    def _on_route_done(self, task: asyncio.Task) -> None:
        """Done-callback for _route_packet tasks: decrement counter and surface errors."""
        self._in_flight -= 1
        self._route_tasks.discard(task)
        if not task.cancelled():
            exc = task.exception()
            if exc is not None:
                logger.error("_route_packet raised: %s", exc, exc_info=exc)
    
    def _should_deliver_path_to_companions(self, packet, *, recipient=None) -> bool:
        """Return True if this PATH/protocol-response should be delivered to companions (first of duplicates)."""
        packet_key = _companion_dedup_key(packet)
        if not packet_key:
            return True
        key = (packet_key, id(recipient)) if recipient is not None else packet_key
        now = time.monotonic()
        # Keep small caches cheap, but never prune a recipient whose first
        # delivery or duplicate waiters still own its lock.
        if len(self._companion_delivered) > 200:
            expired = [
                k for k, expiry in self._companion_delivered.items()
                if expiry <= now and k not in self._companion_delivery_locks
            ]
            for expired_key in expired:
                self._companion_delivered.pop(expired_key, None)
                self._companion_delivery_results.pop(expired_key, None)
        if self._companion_delivered.get(key, 0.0) > now:
            # WM1303: Record dedup event for visualization
            try:
                from repeater.bridge_engine import _active_bridge
                if _active_bridge:
                    _active_bridge._record_dedup_event('companion_dedup', 'companion',
                                                       packet_key[:12], 0, '')
            except Exception:
                pass
            return False
        self._companion_delivered.pop(key, None)
        self._companion_delivery_results.pop(key, None)
        # When full, still deliver and authenticate; only skip caching this
        # additional result rather than evicting an unexpired owned packet.
        if len(self._companion_delivered) < _COMPANION_DEDUPE_MAX_ENTRIES:
            self._companion_delivered[key] = now + _COMPANION_DEDUPE_TTL_SEC
        return True

    def _record_for_ui(self, packet, metadata: dict) -> None:
        """Record an injection-only packet for the web UI (storage + recent_packets)."""
        handler = getattr(self.daemon, "repeater_handler", None)
        if handler and getattr(handler, "storage", None):
            try:
                handler.record_packet_only(packet, metadata)
            except Exception as e:
                logger.debug("Record for UI failed: %s", e)

    async def _consume_via_local_candidates(self, packet, helper, method_name):
        """Try colliding local hashes and consume only authenticated messages."""
        dest_hash = packet.payload[0] if packet.payload else None
        bridges = getattr(self.daemon, "companion_bridges", {})
        has_companion = dest_hash is not None and dest_hash in bridges
        consumed = False
        if has_companion:
            try:
                result = await bridges[dest_hash].process_received_packet(packet)
                consumed = bool(getattr(result, "authenticated", result))
            except Exception as e:
                logger.debug("Companion candidate delivery failed: %s", e)
        handlers = getattr(helper, "handlers", {}) if helper else {}
        if helper and (not has_companion or dest_hash in handlers):
            try:
                result = await getattr(helper, method_name)(packet)
                consumed = bool(getattr(result, "authenticated", result)) or consumed
            except Exception as e:
                logger.debug("Local candidate delivery failed: %s", e)
        return consumed

    async def _deliver_path_to_bridge(self, packet, bridge):
        """Share a recipient's completed ownership result with duplicate RX."""
        packet_key = _companion_dedup_key(packet)
        if packet_key is None:
            result = await bridge.process_received_packet(packet)
            return getattr(result, "authenticated", False) is True
        key = (packet_key, id(bridge))
        entry = self._companion_delivery_locks.setdefault(key, [asyncio.Lock(), 0])
        entry[1] += 1
        try:
            async with entry[0]:
                # Wait for the first copy before deciding whether this one
                # belongs locally. An in-flight reservation alone cannot safely
                # answer the caller's forwarding decision.
                if not self._should_deliver_path_to_companions(packet, recipient=bridge):
                    return self._companion_delivery_results[key][1]
                cancellation = None
                try:
                    delivery = asyncio.create_task(bridge.process_received_packet(packet))
                    while True:
                        try:
                            result = await asyncio.shield(delivery)
                            break
                        except asyncio.CancelledError as exc:
                            if delivery.cancelled():
                                raise
                            # The bridge shields its owned RX worker. Retain
                            # this awaiter and recipient lock until its actual
                            # result settles, even if this route is cancelled;
                            # otherwise a waiting duplicate could run beside it.
                            cancellation = exc
                    authenticated = getattr(result, "authenticated", False) is True
                except BaseException:
                    self._companion_delivered.pop(key, None)
                    self._companion_delivery_results.pop(key, None)
                    raise
                if authenticated and key in self._companion_delivered:
                    # Retain the actual bridge as well as its id: a replaced
                    # recipient must not inherit another object's cached result.
                    self._companion_delivery_results[key] = (bridge, authenticated)
                    self._companion_delivered[key] = time.monotonic() + _COMPANION_DEDUPE_TTL_SEC
                else:
                    # Not-for-us also covers pre-auth handler failures or a
                    # contact not yet known locally. Do not suppress its retry.
                    self._companion_delivered.pop(key, None)
                    self._companion_delivery_results.pop(key, None)
                if cancellation is not None:
                    raise cancellation
                return authenticated
        finally:
            entry[1] -= 1
            if not entry[1]:
                self._companion_delivery_locks.pop(key, None)

    async def _deliver_to_bridges(self, packet, bridges, *, dedupe=False):
        """Deliver once per recipient and preserve authenticated ownership."""
        if not bridges:
            return False
        if packet.get_payload_type() == AckHandler.payload_type() and len(packet.payload) > 4:
            # Firmware ACKs may carry routing metadata after the CRC. The core
            # CompanionBridge accepts only the four-byte ACK; preserve the
            # original frame for the forwarding engine.
            packet = copy.copy(packet)
            packet.payload = bytearray(packet.payload[:4])
            packet.payload_len = 4
        authenticated = False
        for bridge in tuple(bridges.values()):
            try:
                if dedupe:
                    owned = await self._deliver_path_to_bridge(packet, bridge)
                else:
                    result = await bridge.process_received_packet(packet)
                    owned = getattr(result, "authenticated", False) is True
                authenticated = owned or authenticated
            except Exception as e:
                logger.debug("Companion bridge delivery failed: %s", e)
        return authenticated

    async def _register_packet_ack(self, packet):
        dispatcher = getattr(self.daemon, "dispatcher", None)
        register_ack = getattr(dispatcher, "_register_ack_received", None)
        if not packet.payload:
            return
        if packet.get_payload_type() == PAYLOAD_TYPE_MULTIPART:
            crc = MultipartAckHandler(log_fn=logger.debug).extract_ack_crc(packet)
        else:
            crc = int.from_bytes(packet.payload[:4], "little") if len(packet.payload) >= 4 else None
        if register_ack and crc is not None:
            try:
                await register_ack(crc)
            except Exception as e:
                logger.debug("Dispatcher ACK registration failed: %s", e)
        return crc

    async def wait_for_packet_ack(self, packet, expected_crc=None, ack_timeout_s=None):
        """Wait on the MeshCore acknowledgement CRC supplied by the sender."""
        if packet.get_payload_type() in (AckHandler.payload_type(), AdvertHandler.payload_type()):
            return True
        dispatcher = getattr(self.daemon, "dispatcher", None)
        if dispatcher is None or not hasattr(dispatcher, "wait_for_ack"):
            return False
        crc = expected_crc if expected_crc is not None else packet.get_crc()
        timeout = float(ack_timeout_s) if isinstance(ack_timeout_s, (int, float)) and ack_timeout_s > 0 else 5.0
        return await dispatcher.wait_for_ack(crc, timeout=timeout)

    async def enqueue(self, packet):
        """Add packet to router queue."""
        if self.queue.full():
            logger.warning("Packet router queue full (%d), dropping oldest", self.queue.maxsize)
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        await self.queue.put(packet)

    async def inject_packet(self, packet, wait_for_ack: bool = False, *, expected_crc=None, ack_timeout_s=None):
        try:
            metadata = {
                "rssi": getattr(packet, "rssi", 0),
                "snr": getattr(packet, "snr", 0.0),
                "timestamp": getattr(packet, "timestamp", 0),
            }

            # Serialize injects so one local TX completes before the next runs
            # (avoids duty-cycle or dispatcher races where a later packet goes out first)
            async with self._inject_lock:
                # Use local_transmission=True to bypass forwarding logic
                sent = await self.daemon.repeater_handler(
                    packet, metadata, local_transmission=True
                )
            if sent is False:
                return False

            # Mark so when this packet is dequeued we don't pass to engine again (avoid double-send / double-count)
            packet._injected_for_tx = True

            # Enqueue so router can deliver to companion(s): TXT_MSG -> dest bridge, ACK -> all bridges (sender sees ACK)
            await self.enqueue(packet)

            if wait_for_ack and not await self.wait_for_packet_ack(packet, expected_crc, ack_timeout_s):
                return False

            packet_len = len(packet.payload) if packet.payload else 0
            logger.debug(
                f"Injected packet processed by engine as local transmission ({packet_len} bytes)"
            )
            # Log protocol REQ (e.g. status/telemetry) so we can confirm target node
            ptype = getattr(packet, "get_payload_type", lambda: None)()
            if ptype == ProtocolRequestHandler.payload_type() and packet.payload and packet_len >= 1:
                logger.info(
                    "Injected protocol REQ: dest=0x%02x, payload=%d bytes",
                    packet.payload[0],
                    packet_len,
                )
            return True

        except Exception as e:
            logger.error(f"Error injecting packet through engine: {e}")
            return False
    
    async def _process_queue(self):
        while self.running:
            try:
                packet = await asyncio.wait_for(self.queue.get(), timeout=0.1)
                # Drop early if the in-flight cap is reached.  This is a last-resort
                # safety valve — under normal operation LoRa airtime and the duty-cycle
                # gate keep _in_flight well below _max_in_flight.
                if self._in_flight >= self._max_in_flight:
                    self._cap_drop_count += 1
                    logger.warning(
                        "In-flight task cap reached (%d/%d), dropping packet "
                        "(session total dropped: %d)",
                        self._in_flight, self._max_in_flight, self._cap_drop_count,
                    )
                    continue
                self._in_flight += 1
                task = asyncio.create_task(self._route_packet(packet))
                self._route_tasks.add(task)
                task.add_done_callback(self._on_route_done)
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.error(f"Router error: {e}", exc_info=True)

    async def _route_packet(self, packet, *, origin_channel=None):

        payload_type = packet.get_payload_type()
        processed_by_injection = False
        metadata = {
            "rssi": getattr(packet, "rssi", 0),
            "snr": getattr(packet, "snr", 0.0),
            "timestamp": getattr(packet, "timestamp", 0),
        }

        route = getattr(packet, "header", 0) & PH_ROUTE_MASK
        transit_direct = route in (ROUTE_TYPE_DIRECT, ROUTE_TYPE_TRANSPORT_DIRECT) and bool(packet.path)
        multipart_crc = None
        if payload_type == AckHandler.payload_type():
            # MeshCore permits early ACK notification even at an intermediate hop.
            await self._register_packet_ack(packet)
            if transit_direct:
                await self._deliver_to_bridges(packet, getattr(self.daemon, "companion_bridges", {}))
        elif payload_type == PAYLOAD_TYPE_MULTIPART and not transit_direct:
            # Firmware only receives multipart ACKs at the endpoint. The
            # intermediate-hop branch regenerates them in the repeater engine.
            multipart_crc = await self._register_packet_ack(packet)
        if transit_direct and payload_type != TraceHandler.payload_type():
            # Destination hashes belong to the final recipient. Intermediate
            # repeaters must not consume packets through a colliding local hash.
            if (payload_type == ControlHandler.payload_type() and packet.payload
                    and packet.payload[0] & 0x80):
                return True  # this CONTROL subset is zero-hop only
            if self.daemon.repeater_handler and not getattr(packet, "_injected_for_tx", False):
                await self.daemon.repeater_handler(packet, metadata)
            return False

        # Route to specific handlers for parsing only
        if payload_type == TraceHandler.payload_type():
            # Locally-injected outgoing TRACE requests (path empty, _injected_for_tx
            # set) re-enter the router for companion delivery only — skip TraceHelper
            # to avoid false ping-tag matches against zeroed local metadata of our
            # own TX.
            #
            # TRACE_RESPs received from other nodes via the bridge also arrive with
            # _injected_for_tx=True (set by _bridge_repeater_handler / Bug 1 fix)
            # but carry >=1 path byte (the SNR appended by the responding node).
            # These MUST reach TraceHelper so the ping result is delivered to
            # companion clients.
            # Distinguish by packet.path: empty -> outgoing TX, non-empty -> RF
            # response.
            _pkt_path = getattr(packet, "path", None)
            _is_outgoing_tx = (
                getattr(packet, "_injected_for_tx", False)
                and not _pkt_path  # empty list or None -> locally-originated TX
            )
            if _is_outgoing_tx:
                processed_by_injection = True
            elif self.daemon.trace_helper:
                await self.daemon.trace_helper.process_trace_packet(packet)
                # Skip engine processing for trace packets - they're handled by trace helper
                processed_by_injection = True
                # Do not call _record_for_ui: TraceHelper.log_trace_record already persists the
                # trace path from the payload. record_packet_only would treat packet.path (SNR bytes)
                # as routing hashes and log bogus duplicate rows.
            else:
                # TRACE path bytes hold SNR values, so generic path forwarding
                # would interpret them as hashes and corrupt the trace.
                processed_by_injection = True

        elif payload_type == ControlHandler.payload_type():
            # MeshCore delivers the high-bit CONTROL subset only on a direct
            # zero-hop route. The parser can reject a flood packet, but its
            # return value is independent of the companion broadcast below.
            # Apply the wire gate here too, before either consumer sees it.
            if (not packet.payload or (packet.payload[0] & 0x80) == 0
                    or not _is_direct_final_hop(packet)):
                return True
            # Process control/discovery packet
            if self.daemon.discovery_helper:
                await self.daemon.discovery_helper.control_handler(packet)
            packet.mark_do_not_retransmit()
            # Deliver to companions via daemon (frame servers push PUSH_CODE_CONTROL_DATA 0x8E)
            deliver = getattr(self.daemon, "deliver_control_data", None)
            if deliver:
                snr = getattr(packet, "_snr", None)
                if snr is None:
                    snr = getattr(packet, "snr", 0.0)
                rssi = getattr(packet, "_rssi", None)
                if rssi is None:
                    rssi = getattr(packet, "rssi", 0)
                path_len = getattr(packet, "path_len", 0) or 0
                # The encoded length can still be 64/128 for a valid zero-hop
                # route; those mode bits are not a number of path bytes.
                path_bytes = b""
                payload_bytes = bytes(packet.payload) if packet.payload else b""
                await deliver(snr, rssi, path_len, path_bytes, payload_bytes)

        elif payload_type == AdvertHandler.payload_type():
            # Process advertisement packet for neighbor tracking
            if self.daemon.advert_helper:
                rssi = getattr(packet, "rssi", 0)
                snr = getattr(packet, "snr", 0.0)
                # v2.5.7: forward origin channel so neighbours table tracks per-channel info
                channel = origin_channel or getattr(packet, "_origin_channel", "") or getattr(packet, "origin_channel", "") or ""
                await self.daemon.advert_helper.process_advert_packet(packet, rssi, snr, channel)
            # Also feed adverts to companion bridges (for contact/path updates)
            for bridge in getattr(self.daemon, "companion_bridges", {}).values():
                try:
                    await bridge.process_received_packet(packet)
                except Exception as e:
                    logger.debug(f"Companion bridge advert error: {e}")

        elif payload_type == LoginServerHandler.payload_type():
            # Route to companion if dest is a companion; else to login_helper (for logging into this repeater).
            # When dest is remote (not handled), pass to engine so DIRECT/FLOOD ANON_REQ can be forwarded.
            # Our own injected ANON_REQ is suppressed by the engine's duplicate (mark_seen) check.
            processed_by_injection = await self._consume_via_local_candidates(
                packet, self.daemon.login_helper, "process_login_packet"
            )
            if processed_by_injection:
                self._record_for_ui(packet, metadata)

        elif payload_type in (AckHandler.payload_type(), PAYLOAD_TYPE_MULTIPART):
            # ACK has no dest in payload (4-byte CRC only); deliver to all bridges so sender sees send_confirmed.
            # Do not set processed_by_injection so packet also reaches engine for DIRECT forwarding when we're a middle hop.
            companion_bridges = getattr(self.daemon, "companion_bridges", {})
            ack_packet = packet
            if payload_type == PAYLOAD_TYPE_MULTIPART:
                if multipart_crc is None:
                    return True
                # CompanionBridge has an ACK handler, not a multipart handler.
                # Deliver the embedded ACK without mutating the received frame.
                ack_packet = copy.copy(packet)
                ack_packet.header = ((packet.header & ~(PH_TYPE_MASK << PH_TYPE_SHIFT))
                                     | (AckHandler.payload_type() << PH_TYPE_SHIFT))
                ack_packet.payload = bytearray(packet.payload[1:5])
                ack_packet.payload_len = len(ack_packet.payload)
            await self._deliver_to_bridges(ack_packet, companion_bridges)
            if payload_type == PAYLOAD_TYPE_MULTIPART:
                processed_by_injection = True  # endpoint multipart ACKs are never flooded

        elif payload_type == PAYLOAD_TYPE_RAW_CUSTOM and _is_direct_final_hop(packet):
            await self._deliver_to_bridges(packet, getattr(self.daemon, "companion_bridges", {}))
            processed_by_injection = True

        elif payload_type == TextMessageHandler.payload_type():
            processed_by_injection = await self._consume_via_local_candidates(
                packet, self.daemon.text_helper, "process_text_packet"
            )
            if processed_by_injection:
                self._record_for_ui(packet, metadata)

        elif payload_type in (PathHandler.payload_type(), ProtocolResponseHandler.payload_type()):
            dest_hash = packet.payload[0] if packet.payload else None
            companion_bridges = getattr(self.daemon, "companion_bridges", {})
            targets = {dest_hash: companion_bridges[dest_hash]} if dest_hash in companion_bridges else companion_bridges
            processed_by_injection = await self._deliver_to_bridges(packet, targets, dedupe=True)
            helper = self.daemon.path_helper
            if helper and (not targets or dest_hash in getattr(helper, "acl_dict", {})):
                handled = await helper.process_path_packet(packet)
                processed_by_injection = bool(handled) or processed_by_injection
            if processed_by_injection:
                self._record_for_ui(packet, metadata)

        elif payload_type == LoginResponseHandler.payload_type():
            # PAYLOAD_TYPE_RESPONSE (0x01): payload is dest_hash(1)+src_hash(1)+encrypted.
            # Deliver to the bridge that is the destination, or to all bridges when the
            # response is addressed to this repeater (path-based reply: firmware sends
            # to first hop instead of original requester).
            # Do not set processed_by_injection so packet also reaches engine for DIRECT forwarding when we're a middle hop.
            dest_hash = packet.payload[0] if packet.payload and len(packet.payload) >= 1 else None
            companion_bridges = getattr(self.daemon, "companion_bridges", {})
            targets = {dest_hash: companion_bridges[dest_hash]} if dest_hash in companion_bridges else companion_bridges
            processed_by_injection = await self._deliver_to_bridges(packet, targets, dedupe=True)
            if processed_by_injection:
                self._record_for_ui(packet, metadata)

        elif payload_type == ProtocolRequestHandler.payload_type():
            dest_hash = packet.payload[0] if packet.payload else None
            companion_bridges = getattr(self.daemon, "companion_bridges", {})
            processed_by_injection = await self._consume_via_local_candidates(
                packet, self.daemon.protocol_request_helper, "process_request_packet"
            )
            if processed_by_injection:
                self._record_for_ui(packet, metadata)
            elif (dest_hash not in companion_bridges and not self.daemon.protocol_request_helper
                  and companion_bridges and _is_direct_final_hop(packet)):
                # DIRECT with empty path: we're the final hop; deliver to all bridges for anon matching
                for bridge in companion_bridges.values():
                    try:
                        await bridge.process_received_packet(packet)
                    except Exception as e:
                        logger.debug(f"Companion bridge REQ (final hop) error: {e}")
                processed_by_injection = True
                self._record_for_ui(packet, metadata)

        elif payload_type in (GroupTextHandler.payload_type(), PAYLOAD_TYPE_GRP_DATA):
            # Group text/data reach all companions, which filter by channel.
            companion_bridges = getattr(self.daemon, "companion_bridges", {})
            for bridge in companion_bridges.values():
                try:
                    await bridge.process_received_packet(packet)
                except Exception as e:
                    logger.debug(f"Companion bridge GRP_TXT error: {e}")

        # Only pass to repeater engine if not already processed by injection
        # Skip engine for packets we injected for TX (already sent; avoid double-send/double-count)
        handled_locally = processed_by_injection
        if getattr(packet, "_injected_for_tx", False):
            processed_by_injection = True
        if self.daemon.repeater_handler and not processed_by_injection:
            metadata = {
                "rssi": getattr(packet, "rssi", 0),
                "snr": getattr(packet, "snr", 0.0),
                "timestamp": getattr(packet, "timestamp", 0),
            }
            sent = await self.daemon.repeater_handler(packet, metadata)
            if sent is False:
                reason = metadata.get("_repeater_drop_reason")
                if reason:
                    logger.debug("Repeater did not forward packet: %s", reason)
                else:
                    logger.warning("Repeater did not forward packet and supplied no drop reason")
        return handled_locally
