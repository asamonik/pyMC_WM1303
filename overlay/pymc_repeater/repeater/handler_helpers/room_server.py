import asyncio
import logging
import secrets
import time
from copy import deepcopy
from typing import Dict

from openhop_core.protocol import CryptoUtils, PacketBuilder
from openhop_core.protocol.constants import PAYLOAD_TYPE_TXT_MSG, PUB_KEY_SIZE
from openhop_core.protocol.packet_utils import PathUtils

from ..room_settings import MAX_ROOM_CLIENTS
from .acl import PERM_ACL_ADMIN, PERM_ACL_READ_WRITE, PERM_ACL_ROLE_MASK

logger = logging.getLogger("RoomServer")

# Hard limit from C++ simple_room_server
MAX_UNSYNCED_POSTS = 32

# Text message type constants
TXT_TYPE_PLAIN = 0x00
TXT_TYPE_CLI_DATA = 0x01
TXT_TYPE_SIGNED_PLAIN = 0x02

# Push timing constants (from C++ simple_room_server)
PUSH_NOTIFY_DELAY_MS = 2000
SYNC_PUSH_INTERVAL_MS = 1200
POST_SYNC_DELAY_SECS = 6
PUSH_ACK_TIMEOUT_FLOOD_MS = 12000
PUSH_TIMEOUT_BASE_MS = 4000
PUSH_ACK_TIMEOUT_FACTOR_MS = 2000

# Safety limits and protections
# Match C++ MAX_POST_TEXT_LEN from examples/simple_room_server/MyMesh.h:
# #define MAX_POST_TEXT_LEN (160-9) -- 160-byte encrypted text budget minus the
# 9-byte prefix (4-byte timestamp + 1-byte flags/attempt + 4-byte author pubkey prefix).
MAX_POST_TEXT_LEN = 151
MAX_POSTS_PER_CLIENT_PER_MINUTE = 10  # Prevent spam
MAX_CLIENTS_PER_ROOM = MAX_ROOM_CLIENTS
MAX_PUSH_FAILURES = 3  # Evict after this many consecutive failures
INACTIVE_CLIENT_TIMEOUT = 3600  # Evict after 1 hour inactivity (seconds)
MAX_CONSECUTIVE_SYNC_ERRORS = 10  # Circuit breaker threshold
DB_ERROR_RETRY_DELAY = 60  # Wait 1 minute on DB error (seconds)

# Backoff strategy for failed pushes (seconds)
RETRY_BACKOFF_SCHEDULE = [0, 30, 300, 3600]  # 0s, 30s, 5min, 1hr

# Note: Server/system messages now use the room server's actual public key
# This allows clients to identify which room server sent the message

# Global rate limiter (shared across all rooms)
_global_push_limiter = None
_global_push_lock = asyncio.Lock()
GLOBAL_MIN_GAP_BETWEEN_MESSAGES = 1.1  # 1.1s  minimum gap between transmissions


def _truncate_utf8(text: str, max_bytes: int) -> str:
    """Truncate ``text`` to at most ``max_bytes`` of its UTF-8 encoding.

    Cuts at a codepoint boundary so a partial multi-byte sequence is never
    emitted (matches firmware's byte-length text budget, but stays
    UTF-8-safe rather than the firmware's raw ``strncpy``).
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


class GlobalRateLimiter:
    def __init__(self, min_gap_seconds: float = 0.1):
        self.min_gap = min_gap_seconds  # Minimum gap between consecutive messages
        self.lock = asyncio.Lock()  # Only one transmission at a time
        self.last_release_time = 0.0

    async def acquire(self):
        """Acquire the global transmit lock and enforce the inter-message gap.

        The lock is **held on return** and must be released with ``release()``
        (call it in a ``finally``). The previous implementation released the
        lock inside an ``async with`` before returning, so the caller's radio
        transmission ran with no lock held and multiple room loops could push
        concurrently. A monotonic clock is used so a wall-clock jump cannot
        skip or extend the gap.
        """
        await self.lock.acquire()
        try:
            time_since_last = time.monotonic() - self.last_release_time
            if time_since_last < self.min_gap:
                wait_time = self.min_gap - time_since_last
                logger.debug(f"Global rate limiter: waiting {wait_time * 1000:.0f}ms")
                await asyncio.sleep(wait_time)
        except BaseException:
            # Never leak the lock if the gap wait is cancelled.
            self.lock.release()
            raise

    def release(self):
        """Record the transmission time and release the transmit lock."""
        self.last_release_time = time.monotonic()
        if self.lock.locked():
            self.lock.release()


class RoomServer:
    def __init__(
        self,
        room_hash: int,
        room_name: str,
        local_identity,
        sqlite_handler,
        packet_injector,
        acl,
        max_posts: int = 32,
        config_path: str = None,
        config: dict = None,
        config_manager=None,
        send_advert_callback=None,
    ):

        self.room_hash = room_hash
        self.room_name = room_name
        self.local_identity = local_identity
        self.db = sqlite_handler
        self.packet_injector = packet_injector
        self.acl = acl

        # Capture this room's active advert metadata once. The shared config
        # may later contain a renamed/replaced room or settings staged for
        # restart; neither may change an already-running identity's advert.
        active_room_settings = {}
        if isinstance(config, dict):
            from repeater.companion.identity_resolve import derive_companion_public_key_hex

            identities = config.get("identities") or {}
            rooms = identities.get("room_servers") or []
            for entry in rooms:
                if (isinstance(entry, dict)
                        and isinstance(entry.get("name"), str)
                        and entry["name"].strip() == room_name.strip()
                        and derive_companion_public_key_hex(entry.get("identity_key"))
                        == local_identity.get_public_key().hex()):
                    active_room_settings = deepcopy(entry.get("settings") or {})
                    break

        # Create send_advert callback for this room server
        async def send_room_advert():
            """Send advertisement for this specific room server."""
            if not packet_injector or not local_identity:
                logger.error(
                    f"Room '{room_name}': Cannot send advert - missing injector or identity"
                )
                return False

            try:
                from openhop_core.protocol import PacketBuilder
                from openhop_core.protocol.constants import (
                    ADVERT_FLAG_HAS_NAME,
                    ADVERT_FLAG_IS_ROOM_SERVER,
                )

                # Use room-specific name and location
                node_name = active_room_settings.get(
                    "node_name", active_room_settings.get("room_name", room_name),
                )
                latitude = active_room_settings.get("latitude", 0.0)
                longitude = active_room_settings.get("longitude", 0.0)

                flags = ADVERT_FLAG_IS_ROOM_SERVER | ADVERT_FLAG_HAS_NAME

                packet = PacketBuilder.create_advert(
                    local_identity=local_identity,
                    name=node_name,
                    lat=latitude,
                    lon=longitude,
                    feature1=0,
                    feature2=0,
                    flags=flags,
                    route_type="flood",
                )

                # Send via packet injector
                sent = await packet_injector(packet, wait_for_ack=False)
                if not sent:
                    logger.error(f"Room '{room_name}': Failed to send advert")
                    return False

                logger.info(
                    f"Room '{room_name}': Sent flood advert '{node_name}' at ({latitude:.6f}, {longitude:.6f})"
                )
                return True

            except Exception as e:
                logger.error(f"Room '{room_name}': Failed to send advert: {e}", exc_info=True)
                return False

        # Initialize CLI handler for room server commands
        self.cli = None
        if config_path and config and config_manager:
            from .mesh_cli import MeshCLI

            self.cli = MeshCLI(
                config_path,
                config,
                config_manager,
                identity_type="room_server",
                enable_regions=False,  # Room servers don't support region commands
                send_advert_callback=send_room_advert,
                identity=local_identity,
                storage_handler=sqlite_handler,
                room_name=room_name,
            )
            logger.info(f"Room '{room_name}': Initialized CLI handler with identity and storage")

        # Enforce hard limit (match C++ MAX_UNSYNCED_POSTS)
        if max_posts > MAX_UNSYNCED_POSTS:
            logger.warning(
                f"Room '{room_name}': max_posts={max_posts} exceeds hard limit "
                f"of {MAX_UNSYNCED_POSTS}, capping to {MAX_UNSYNCED_POSTS}"
            )
            max_posts = MAX_UNSYNCED_POSTS
        self.max_posts = max_posts

        # Round-robin state
        self.next_client_idx = 0
        self.next_push_time = 0

        # Cleanup tracking
        self.last_cleanup_time = time.time()
        self.cleanup_interval = 600  # Cleanup every 10 minutes

        # Safety and monitoring
        self.client_post_times = {}  # Track last N post times per client for rate limiting
        # Confirmed delivery survives a failed cursor write until it can commit.
        self._acknowledged_pushes = {}
        self.consecutive_sync_errors = 0  # Circuit breaker counter
        self.last_eviction_check = time.time()
        self.eviction_check_interval = 300  # Check every 5 minutes

        # Initialize global rate limiter (singleton)
        global _global_push_limiter
        if _global_push_limiter is None:
            _global_push_limiter = GlobalRateLimiter(GLOBAL_MIN_GAP_BETWEEN_MESSAGES)
        self.global_limiter = _global_push_limiter

        # Background task handle
        self._sync_task = None
        self._running = False

        logger.info(
            f"RoomServer initialized: name='{room_name}', "
            f"hash=0x{room_hash:02X}, max_posts={max_posts}"
        )

    async def start(self):
        if self._running:
            logger.warning(f"Room '{self.room_name}' sync loop already running")
            return

        self._running = True
        self._sync_task = asyncio.create_task(self._sync_loop())
        logger.info(f"Room '{self.room_name}' sync loop started")

    async def stop(self):
        self._running = False
        stop_error = None
        if self._sync_task:
            self._sync_task.cancel()
            try:
                await self._sync_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                stop_error = exc
        try:
            await self._flush_acknowledged_pushes()
        except Exception as exc:
            if stop_error is None:
                stop_error = exc
            else:
                logger.error("Room ACK drain also failed (%s)", type(exc).__name__)
        if stop_error is not None:
            raise stop_error
        logger.info(f"Room '{self.room_name}' sync loop stopped")

    async def add_post(
        self,
        client_pubkey: bytes,
        message_text: str,
        sender_timestamp: int,
        txt_type: int = TXT_TYPE_PLAIN,
        allow_server_author: bool = False,
    ) -> bool:
        # Report the committed post, not later best-effort activity bookkeeping:
        # returning False after insertion would invite a duplicate retry.
        admitted = False
        try:
            # This storage boundary must enforce posting rights independently of
            # the RF handler. Read-only clients still participate in room sync.
            if (
                not isinstance(client_pubkey, (bytes, bytearray))
                or len(client_pubkey) != PUB_KEY_SIZE
            ):
                return False
            client_pubkey = bytes(client_pubkey)
            server_author = (
                allow_server_author is True
                and client_pubkey == self.local_identity.get_public_key()
            )
            if not server_author:
                client = self.acl.get_client(client_pubkey)
                if (
                    client is None
                    or client.id.get_public_key() != client_pubkey
                    or (client.permissions & PERM_ACL_ROLE_MASK)
                    not in (PERM_ACL_READ_WRITE, PERM_ACL_ADMIN)
                ):
                    return False

            # Room text is a C string on the wire, including the ACK preimage.
            message_text = message_text.split("\x00", 1)[0]
            # SAFETY: Validate message length (byte length, matching firmware's
            # MAX_POST_TEXT_LEN text budget), cutting at a UTF-8 codepoint boundary.
            encoded_len = len(message_text.encode("utf-8"))
            if encoded_len > MAX_POST_TEXT_LEN:
                logger.warning(
                    f"Room '{self.room_name}': Message from {client_pubkey[:4].hex()} "
                    f"exceeds max length ({encoded_len} > {MAX_POST_TEXT_LEN} bytes), truncating"
                )
                message_text = _truncate_utf8(message_text, MAX_POST_TEXT_LEN)

            # SAFETY: Rate limit per client
            client_key = client_pubkey.hex()
            now = time.time()

            if client_key not in self.client_post_times:
                self.client_post_times[client_key] = []

            # Remove timestamps older than 1 minute
            self.client_post_times[client_key] = [
                t for t in self.client_post_times[client_key] if now - t < 60
            ]

            # Check rate limit
            if len(self.client_post_times[client_key]) >= MAX_POSTS_PER_CLIENT_PER_MINUTE:
                logger.warning(
                    f"Room '{self.room_name}': Client {client_pubkey[:4].hex()} "
                    f"exceeded rate limit ({MAX_POSTS_PER_CLIENT_PER_MINUTE} posts/min), dropping message"
                )
                return False

            # Use our RTC time for post_timestamp
            post_timestamp = time.time()

            # Store to database
            msg_id = self.db.insert_room_message(
                room_hash=f"0x{self.room_hash:02X}",
                author_pubkey=client_pubkey.hex(),
                message_text=message_text,
                post_timestamp=post_timestamp,
                sender_timestamp=sender_timestamp,
                txt_type=txt_type,
            )

            if msg_id:
                admitted = True
                self.next_push_time = post_timestamp + (PUSH_NOTIFY_DELAY_MS / 1000.0)
                # Failed SQL admission must not consume the client's post quota.
                self.client_post_times[client_key].append(now)
                logger.info(
                    f"Room '{self.room_name}': New post #{msg_id} from "
                    f"{client_pubkey[:4].hex()}: {message_text[:50]}"
                )

                # Log authenticated clients count for debugging distribution
                all_clients = self.acl.get_all_clients()
                logger.info(
                    f"Room '{self.room_name}': Message stored, will distribute to "
                    f"{len(all_clients)} authenticated client(s)"
                )

                # Refresh the author's activity timestamp (they're clearly active
                # if posting). The sync_since watermark is deliberately NOT
                # advanced here: it moves only when a push is ACKed, and self-echo
                # is already prevented by the author exclusion in the
                # unsynced-message query. Advancing it on post would skip other
                # authors' older, still-unsynced messages for this client.
                activity_saved = self.db.upsert_client_sync(
                    room_hash=f"0x{self.room_hash:02X}",
                    client_pubkey=client_pubkey.hex(),
                    last_activity=time.time(),
                )
                if activity_saved is False:
                    logger.warning("Room post saved, but author activity could not be refreshed")

                return True
            else:
                logger.error("Failed to store message to database")
                return False

        except Exception as e:
            if admitted:
                logger.error("Room post saved, but later bookkeeping failed: %s", type(e).__name__)
            else:
                logger.error(f"Error adding post: {e}", exc_info=True)
            return admitted

    async def push_post_to_client(self, client_info, post: Dict) -> bool:

        try:
            client_pubkey = client_info.id.get_public_key()
            if client_pubkey in self._acknowledged_pushes:
                await self._flush_acknowledged_pushes()
            # SAFETY: Check client failure backoff
            sync_state = self.db.get_client_sync(
                room_hash=f"0x{self.room_hash:02X}",
                client_pubkey=client_info.id.get_public_key().hex(),
            )

            if sync_state:
                failures = sync_state.get("push_failures", 0)
                if failures >= MAX_PUSH_FAILURES:
                    logger.debug(
                        f"Room '{self.room_name}': Client 0x{client_info.id.get_public_key()[0]:02X} "
                        f"at max failures ({failures}), skipping push"
                    )
                    return False
                if failures > 0:
                    # Apply exponential backoff
                    backoff_idx = min(failures, len(RETRY_BACKOFF_SCHEDULE) - 1)
                    backoff_delay = RETRY_BACKOFF_SCHEDULE[backoff_idx]
                    last_failure_time = sync_state.get("updated_at", 0)
                    time_since_failure = time.time() - last_failure_time

                    if time_since_failure < backoff_delay:
                        wait_time = backoff_delay - time_since_failure
                        logger.debug(
                            f"Room '{self.room_name}': Client 0x{client_info.id.get_public_key()[0]:02X} "
                            f"in backoff (failure {failures}), waiting {wait_time:.0f}s"
                        )
                        return False  # Skip this client for now

            # Build message payload.
            # Timestamp is the post's own stored timestamp, not the delivery
            # time (MyMesh.cpp:56 serializes the stored post's timestamp).
            timestamp = int(post["post_timestamp"])
            # Low 2 bits carry a random attempt nonce so retries of the same
            # post get a different packet hash/ACK (MyMesh.cpp:59-60).
            flags = (TXT_TYPE_SIGNED_PLAIN << 2) | (secrets.randbits(8) & 0x03)

            # Author prefix (first 4 bytes of pubkey)
            author_pubkey = bytes.fromhex(post["author_pubkey"])
            author_prefix = author_pubkey[:4]

            # Plaintext: timestamp(4) + flags(1) + author_prefix(4) + text
            # SAFETY: Clamp at push time too, in case a legacy stored post
            # (from before this limit was enforced on write) exceeds
            # MAX_POST_TEXT_LEN bytes -- truncate at a UTF-8 codepoint boundary.
            # Legacy stored text may contain a NUL even after new writes have
            # been normalized. Hash exactly what a MeshCore recipient ACKs.
            message_text = post["message_text"].split("\x00", 1)[0]
            if len(message_text.encode("utf-8")) > MAX_POST_TEXT_LEN:
                message_text = _truncate_utf8(message_text, MAX_POST_TEXT_LEN)
            message_bytes = message_text.encode("utf-8")
            plaintext = (
                timestamp.to_bytes(4, "little") + bytes([flags]) + author_prefix + message_bytes
            )
            # Calculate expected ACK (MeshCore signed text):
            # sha256(timestamp + flags + author_prefix + text + recipient_pubkey)[:4]
            ack_hash = CryptoUtils.sha256(plaintext + client_info.id.get_public_key())[:4]
            expected_ack_crc = int.from_bytes(ack_hash, "little")

            # Determine routing based on stored out_path
            route_type = "flood" if client_info.out_path_len < 0 else "direct"

            # Create datagram
            packet = PacketBuilder.create_datagram(
                ptype=PAYLOAD_TYPE_TXT_MSG,
                dest=client_info.id,
                local_identity=self.local_identity,
                secret=client_info.shared_secret,
                plaintext=plaintext,
                route_type=route_type,
            )

            # Add stored path for direct routing. out_path_len is the encoded
            # wire byte (bits 0-5 = hash count, bits 6-7 = hash size - 1);
            # out_path already holds exactly the path bytes.
            if route_type == "direct" and len(client_info.out_path) > 0:
                if PathUtils.is_valid_path_len(client_info.out_path_len):
                    path_byte_len = PathUtils.get_path_byte_len(client_info.out_path_len)
                    packet.path = bytearray(client_info.out_path[:path_byte_len])
                    packet.path_len = client_info.out_path_len
                else:
                    # Legacy fallback: treat stored path as 1-byte-hop path and clamp to
                    # valid encoded range (0-63 hops).
                    legacy_hops = min(len(client_info.out_path), 63)
                    packet.path = bytearray(client_info.out_path[:legacy_hops])
                    packet.path_len = legacy_hops

            # Calculate ACK timeout from the HOP count, not the encoded byte
            # (0x80 = empty 3-byte-hash path would otherwise give a ~4min wait)
            if route_type == "flood":
                ack_timeout = PUSH_ACK_TIMEOUT_FLOOD_MS / 1000.0
            else:
                if PathUtils.is_valid_path_len(client_info.out_path_len):
                    path_len = PathUtils.get_path_hash_count(client_info.out_path_len)
                else:
                    path_len = min(len(client_info.out_path), 63)
                ack_timeout = (
                    PUSH_TIMEOUT_BASE_MS + PUSH_ACK_TIMEOUT_FACTOR_MS * (path_len + 1)
                ) / 1000.0

            current_sync_since = (
                sync_state.get("sync_since", 0)
                if sync_state
                else getattr(client_info, "sync_since", 0)
            )
            prepared_route = (client_info.out_path_len, bytes(client_info.out_path))
            prepared_secret = bytes(client_info.shared_secret)

            # SAFETY: Global transmission lock - only ONE message on the radio at
            # a time, enforced across the entire send. LoRa is serial (0.5-9s
            # airtime per message), and every room has its own sync loop sharing
            # this limiter, so the lock must be held from before we start the ACK
            # timeout clock through the blocking send. Released in `finally` so
            # backoff/exception paths above (which returned before acquiring) do
            # not, and the send path always does, free it exactly once.
            await self.global_limiter.acquire()
            try:
                # Login/path updates may run while another room owns the TX
                # limiter. Never restore an earlier session's cursor or route.
                current_client = self.acl.get_client(client_pubkey)
                latest = self.db.get_client_sync(
                    f"0x{self.room_hash:02X}", client_pubkey.hex(),
                )
                if (
                    current_client is not client_info or latest is None
                    or latest.get("sync_since", 0) != current_sync_since
                    or post["post_timestamp"] <= latest.get("sync_since", 0)
                    or latest.get("pending_ack_crc", 0) != 0
                    or latest.get("last_activity", 0) == 0
                    or latest.get("push_failures", 0) >= MAX_PUSH_FAILURES
                    or (client_info.out_path_len, bytes(client_info.out_path)) != prepared_route
                    or bytes(client_info.shared_secret) != prepared_secret
                ):
                    return False
                # Commit the pending attempt before TX, without rewriting the
                # cursor. A concurrent login reset invalidates its completion.
                pending_saved = self.db.begin_room_push(
                    room_hash=f"0x{self.room_hash:02X}",
                    client_pubkey=client_pubkey.hex(),
                    expected_crc=expected_ack_crc,
                    post_timestamp=post["post_timestamp"],
                    ack_timeout_time=time.time() + ack_timeout,
                    sync_since=latest.get("sync_since", 0),
                )
                if not pending_saved:
                    return False
                # Send and wait for the client's delivery ACK. The injector must
                # be told the crypto ACK CRC we computed above — its default
                # (packet.get_crc()) is a packet-hash CRC no client ever sends.
                # This blocks for the entire transmission duration (0.5-9 seconds)
                success = await self.packet_injector(
                    packet,
                    wait_for_ack=True,
                    expected_crc=expected_ack_crc,
                    ack_timeout_s=ack_timeout,
                )
            finally:
                # SAFETY: Release the transmission lock on every path (ACK,
                # timeout, or exception during the send).
                self.global_limiter.release()

            if success:
                # ACK received! Update sync state
                if not await self._handle_ack_received(
                    client_pubkey, post["post_timestamp"], expected_ack_crc,
                ):
                    logger.info("Room ACK belongs to a superseded push attempt")
                    return False
                logger.info(
                    f"Room '{self.room_name}': Pushed post to "
                    f"0x{client_info.id.get_public_key()[0]:02X} via {route_type.upper()}, ACK received"
                )
            else:
                # ACK timeout
                timed_out = await self._handle_ack_timeout(
                    client_pubkey, post["post_timestamp"], expected_ack_crc,
                )
                if timed_out:
                    logger.warning(
                        f"Room '{self.room_name}': Push to "
                        f"0x{client_info.id.get_public_key()[0]:02X} timed out"
                    )

            return success

        except Exception as e:
            logger.error(f"Error pushing post to client: {e}", exc_info=True)
            # Let the sync loop's existing DB-error backoff act on failures;
            # they must not be reported as successful delivery or RF timeouts.
            raise

    async def _handle_ack_received(
        self, client_pubkey: bytes, post_timestamp: float, expected_crc: int,
    ) -> bool:
        attempt = (expected_crc, post_timestamp)
        self._acknowledged_pushes[client_pubkey] = attempt
        committed = self.db.finish_room_push(
            f"0x{self.room_hash:02X}", client_pubkey.hex(), expected_crc,
            post_timestamp, acknowledged=True,
        )
        # False means a login/reset superseded it; exceptions keep the actual
        # ACK owned in RAM for a later commit, not a false radio timeout.
        if self._acknowledged_pushes.get(client_pubkey) == attempt:
            del self._acknowledged_pushes[client_pubkey]
        return committed

    async def _flush_acknowledged_pushes(self):
        for pubkey, (crc, timestamp) in tuple(self._acknowledged_pushes.items()):
            await self._handle_ack_received(pubkey, timestamp, crc)

    async def _handle_ack_timeout(
        self, client_pubkey: bytes, post_timestamp: float, expected_crc: int,
    ) -> bool:
        if client_pubkey in self._acknowledged_pushes:
            await self._flush_acknowledged_pushes()
            return False
        return self.db.finish_room_push(
            f"0x{self.room_hash:02X}", client_pubkey.hex(), expected_crc,
            post_timestamp, acknowledged=False,
        )

    def get_unsynced_count(self, client_pubkey: bytes) -> int:
        try:
            # Get client's sync state
            sync_state = self.db.get_client_sync(
                room_hash=f"0x{self.room_hash:02X}", client_pubkey=client_pubkey.hex()
            )

            sync_since = sync_state["sync_since"] if sync_state else 0

            return self.db.get_unsynced_count(
                room_hash=f"0x{self.room_hash:02X}",
                client_pubkey=client_pubkey.hex(),
                sync_since=sync_since,
            )
        except Exception as e:
            logger.error(f"Error getting unsynced count: {e}")
            return 0

    async def _evict_failed_clients(self):
        try:
            now = time.time()
            all_sync_states = self.db.get_all_room_clients(f"0x{self.room_hash:02X}")

            for sync_state in all_sync_states:
                client_pubkey_hex = sync_state["client_pubkey"]
                push_failures = sync_state.get("push_failures", 0)
                last_activity = sync_state.get("last_activity", 0)

                # Skip already-evicted clients (marked with last_activity=0)
                if last_activity == 0:
                    continue

                evict = False
                reason = ""

                # Check max failures
                if push_failures >= MAX_PUSH_FAILURES:
                    evict = True
                    reason = f"max failures ({push_failures})"

                # Check inactivity timeout
                elif now - last_activity > INACTIVE_CLIENT_TIMEOUT:
                    evict = True
                    reason = f"inactive for {(now - last_activity) / 60:.0f} minutes"

                if evict:
                    # Remove from database
                    if not self.db.upsert_client_sync(
                        room_hash=f"0x{self.room_hash:02X}",
                        client_pubkey=client_pubkey_hex,
                        last_activity=0,  # Mark as evicted
                    ):
                        raise RuntimeError("Failed to persist room client eviction")

                    # Remove from ACL
                    client_pubkey = bytes.fromhex(client_pubkey_hex)
                    self.acl.remove_client(client_pubkey)

                    logger.info(
                        f"Room '{self.room_name}': Evicted client "
                        f"0x{client_pubkey[0]:02X} ({reason})"
                    )

        except Exception as e:
            logger.error(f"Error evicting failed clients: {e}", exc_info=True)
            raise

    async def _sync_loop(self):

        # SAFETY: Stagger room startup to prevent thundering herd
        startup_delay = secrets.randbelow(5001) / 1000.0  # 0-5 second random delay
        await asyncio.sleep(startup_delay)

        logger.info(f"Room '{self.room_name}' sync loop starting (delayed {startup_delay:.1f}s)")

        while self._running:
            try:
                await asyncio.sleep(SYNC_PUSH_INTERVAL_MS / 1000.0)

                # SAFETY: Circuit breaker - stop if too many consecutive errors
                if self.consecutive_sync_errors >= MAX_CONSECUTIVE_SYNC_ERRORS:
                    logger.error(
                        f"Room '{self.room_name}': Circuit breaker tripped! "
                        f"{self.consecutive_sync_errors} consecutive errors. Pausing for {DB_ERROR_RETRY_DELAY}s"
                    )
                    await asyncio.sleep(DB_ERROR_RETRY_DELAY)
                    self.consecutive_sync_errors = 0  # Reset after pause
                    continue

                # Commit known delivery before eviction or timeout bookkeeping.
                # A database outage is not evidence of an unresponsive client.
                await self._flush_acknowledged_pushes()

                # SAFETY: Periodic eviction check (every 5 minutes)
                if time.time() - self.last_eviction_check > self.eviction_check_interval:
                    await self._evict_failed_clients()
                    self.last_eviction_check = time.time()

                # Periodic cleanup check (every 10 minutes)
                if time.time() - self.last_cleanup_time > self.cleanup_interval:
                    await self._cleanup_old_messages()
                    self.last_cleanup_time = time.time()

                # Check if it's time to push
                if time.time() < self.next_push_time:
                    continue

                # Get all clients for this room
                all_clients = self.acl.get_all_clients()
                if not all_clients:
                    # Only log once when transitioning from clients to no clients
                    # to avoid log spam when room is idle
                    self.next_push_time = time.time() + 1.0  # Check again in 1 second
                    continue

                # SAFETY: Limit number of clients
                if len(all_clients) > MAX_CLIENTS_PER_ROOM:
                    logger.warning(
                        f"Room '{self.room_name}': Too many clients ({len(all_clients)} > {MAX_CLIENTS_PER_ROOM})"
                    )
                    all_clients = all_clients[:MAX_CLIENTS_PER_ROOM]

                # Check for ACK timeouts first
                await self._check_ack_timeouts()

                # Track how many clients we've checked in this iteration
                clients_checked = 0
                max_checks = len(all_clients)

                # Round-robin: find next active client
                while clients_checked < max_checks:
                    # Get next client
                    if self.next_client_idx >= len(all_clients):
                        self.next_client_idx = 0

                    client = all_clients[self.next_client_idx]
                    self.next_client_idx = (self.next_client_idx + 1) % len(all_clients)
                    clients_checked += 1

                    # Get client sync state
                    sync_state = self.db.get_client_sync(
                        room_hash=f"0x{self.room_hash:02X}",
                        client_pubkey=client.id.get_public_key().hex(),
                    )

                    # Skip if already waiting for ACK, evicted, or max failures
                    if sync_state:
                        pending_ack = sync_state.get("pending_ack_crc", 0)
                        last_activity = sync_state.get("last_activity", 0)
                        push_failures = sync_state.get("push_failures", 0)

                        if pending_ack != 0:
                            logger.debug(
                                f"Skipping client 0x{client.id.get_public_key()[0]:02X} (waiting for ACK)"
                            )
                            continue

                        if last_activity == 0:
                            logger.debug(
                                f"Skipping client 0x{client.id.get_public_key()[0]:02X} (evicted)"
                            )
                            continue

                        if push_failures >= MAX_PUSH_FAILURES:
                            logger.debug(
                                f"Skipping client 0x{client.id.get_public_key()[0]:02X} (max failures)"
                            )
                            continue

                        sync_since = sync_state.get("sync_since", 0)
                    else:
                        # Initialize sync state for new client
                        # Use sync_since from ACL client (sent during login) if available
                        sync_since = client.sync_since if hasattr(client, "sync_since") else 0
                        logger.info(
                            f"Room '{self.room_name}': Initializing client "
                            f"0x{client.id.get_public_key()[0]:02X} with sync_since={sync_since}"
                        )
                        if not self.db.upsert_client_sync(
                            room_hash=f"0x{self.room_hash:02X}",
                            client_pubkey=client.id.get_public_key().hex(),
                            sync_since=sync_since,
                            last_activity=time.time(),
                        ):
                            raise RuntimeError("Failed to initialize room client sync state")

                    # Find next unsynced message for this client
                    unsynced = self.db.get_unsynced_messages(
                        room_hash=f"0x{self.room_hash:02X}",
                        client_pubkey=client.id.get_public_key().hex(),
                        sync_since=sync_since,
                        limit=1,
                    )

                    if unsynced:
                        post = unsynced[0]
                        logger.debug(
                            f"Room '{self.room_name}': Client 0x{client.id.get_public_key()[0]:02X} "
                            f"has unsynced message #{post['id']}, post_timestamp={post['post_timestamp']:.1f}"
                        )
                        # Check if enough time has passed since post creation
                        now = time.time()
                        if now >= post["post_timestamp"] + POST_SYNC_DELAY_SECS:
                            # Push this post
                            await self.push_post_to_client(client, post)
                            self.next_push_time = time.time() + (SYNC_PUSH_INTERVAL_MS / 1000.0)
                            break  # Exit the while loop
                        else:
                            # Not ready yet, check sooner
                            self.next_push_time = time.time() + (SYNC_PUSH_INTERVAL_MS / 8000.0)
                            break  # Exit the while loop
                    else:
                        # No unsynced posts for this client, try next client
                        continue

                # If we checked all clients and none were active/ready
                if clients_checked >= max_checks:
                    # All clients skipped or no messages - wait longer before next check
                    self.next_push_time = time.time() + 5.0  # Wait 5 seconds

                # SAFETY: Reset error counter on successful iteration
                self.consecutive_sync_errors = 0

            except asyncio.CancelledError:
                break
            except Exception as e:
                # SAFETY: Track consecutive errors for circuit breaker
                self.consecutive_sync_errors += 1
                logger.error(
                    f"Room '{self.room_name}': Sync loop error #{self.consecutive_sync_errors}: {e}",
                    exc_info=True,
                )

                # SAFETY: Back off on errors
                backoff = min(self.consecutive_sync_errors, 10)  # Cap at 10 seconds
                await asyncio.sleep(backoff)

        logger.info(f"Room '{self.room_name}' sync loop stopped")

    async def _check_ack_timeouts(self):
        await self._flush_acknowledged_pushes()
        now = time.time()
        all_sync_states = self.db.get_all_room_clients(f"0x{self.room_hash:02X}")

        for sync_state in all_sync_states:
            if sync_state["pending_ack_crc"] != 0:
                timeout_time = sync_state.get("ack_timeout_time", 0)
                if now >= timeout_time:
                    client_pubkey = bytes.fromhex(sync_state["client_pubkey"])
                    await self._handle_ack_timeout(
                        client_pubkey, sync_state["push_post_timestamp"],
                        sync_state["pending_ack_crc"],
                    )

    async def _cleanup_old_messages(self):
        try:
            deleted = self.db.cleanup_old_messages(
                room_hash=f"0x{self.room_hash:02X}", keep_count=self.max_posts
            )
            if deleted > 0:
                logger.info(f"Room '{self.room_name}': Cleaned up {deleted} old messages")
        except Exception as e:
            logger.error(f"Error cleaning up old messages: {e}")
