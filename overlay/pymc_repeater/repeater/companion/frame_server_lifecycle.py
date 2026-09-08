"""Owned companion client shutdown over the upstream framing implementation."""

import asyncio
import logging
import struct
import time
from dataclasses import replace

from openhop_core.companion.constants import (
    ADV_TYPE_NONE,
    CHANNEL_NAME_SIZE,
    ERR_CODE_BAD_STATE,
    ERR_CODE_FILE_IO_ERROR,
    ERR_CODE_ILLEGAL_ARG,
    ERR_CODE_NOT_FOUND,
    ERR_CODE_TABLE_FULL,
    ERR_CODE_UNSUPPORTED_CMD,
    FRAME_OUTBOUND_PREFIX,
    MAX_PAYLOAD_SIZE,
    RESP_CODE_CONTACT,
    RESP_CODE_CONTACTS_START,
    RESP_CODE_END_OF_CONTACTS,
    RESP_CODE_ERR,
    RESP_CODE_OK,
)
from openhop_core.companion.frame_server import CompanionFrameServer as _CoreFrameServer
from openhop_core.companion.frame_server.frames import _encode_contact_fields
from openhop_core.companion.models import Channel, QueuedMessage

from repeater.companion_storage import storage_key_for_public_key, validated_channel_rows

from .advert_persistence import AdvertPersistenceMixin
from .command_replies import CommandRepliesMixin
from .contact_commands import PersistentContactCommandsMixin
from .contact_import import prepare_contact_import
from .contact_notifications import ContactNotificationsMixin
from .frame_reads import FrameReadMixin
from .frame_server import CompanionFrameServer as _UpstreamFrameServer
from .login_requests import LoginCommandsMixin
from .message_delivery import MessageDeliveryMixin
from .path_persistence import PathPersistenceMixin
from .request_replies import RequestRepliesMixin

logger = logging.getLogger(__name__)


class CompanionFrameServer(
    FrameReadMixin, RequestRepliesMixin, CommandRepliesMixin, LoginCommandsMixin,
    ContactNotificationsMixin, MessageDeliveryMixin,
    AdvertPersistenceMixin, PathPersistenceMixin,
    PersistentContactCommandsMixin, _UpstreamFrameServer,
):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # companion_hash remains the short routing/UI identifier. Persistent
        # state belongs to the full identity, including after hash reuse.
        self._storage_key = storage_key_for_public_key(self.bridge.get_public_key())
        self._closing = False
        self._client_tasks = set()
        self._client_writers = set()
        self._client_session_lock = asyncio.Lock()
        self._command_reply_captures = {}
        self._request_replies = {}
        self._contact_persistence_lock = asyncio.Lock()
        self._contact_stream_lock = asyncio.Lock()
        self._channel_persistence_lock = asyncio.Lock()
        self._message_persistence_lock = asyncio.Lock()
        self._pending_message_fallbacks = {}
        self._contact_api_tasks = set()
        self._client_drain_task = None
        self._stop_task = None
        self.bridge.set_advert_event_handler(self._handle_advert_event)
        self.bridge.set_contact_path_handler(self._handle_contact_path_update)
        self.bridge.set_message_persistence_lock(self._message_persistence_lock)
        self.bridge.set_message_event_handler(self._handle_message_event)
        self.bridge.set_request_registration_handler(self._register_request_reply)

    def _setup_push_callbacks(self):
        # These are stable bound methods, not per-connection writer closures.
        # Keep startup's three persistence callbacks and unrelated web listeners.
        # Removing/readding during core's live-list iteration could invoke an
        # awaited channel-persistence callback twice, so setup is idempotent.
        registrations = (
            ("message_event", self.bridge.on_message_event, self._on_message_event),
            ("channel_message_event", self.bridge.on_channel_message_event, self._on_channel_message_event),
            ("channel_data_event", self.bridge.on_channel_data_event, self._on_channel_data_event),
            ("send_confirmed", self.bridge.on_send_confirmed, self._on_send_confirmed),
            ("advert_received", self.bridge.on_advert_received, self._on_advert_received),
            ("node_discovered", self.bridge.on_node_discovered, self._on_node_discovered),
            ("contact_path_updated", self.bridge.on_contact_path_updated, self._on_contact_path_updated),
            ("binary_response", self.bridge.on_binary_response, self._on_binary_response),
            ("path_discovery_response", self.bridge.on_path_discovery_response, self._on_path_discovery_response),
            ("contact_deleted", self.bridge.on_contact_deleted, self._on_contact_deleted),
            ("contacts_full", self.bridge.on_contacts_full, self._on_contacts_full),
            ("raw_data_received", self.bridge.on_raw_data_received, self._on_raw_data_received),
            ("trace_received", self.bridge.on_trace_received, self._on_trace_received),
        )
        for event, register, callback in registrations:
            if callback not in self.bridge._push_callbacks.get(event, ()):
                register(callback)

    async def start(self):
        if self._closing:
            raise RuntimeError("Companion server is stopping; create a new instance")
        await super().start()
        if self._closing:
            # Shutdown can overlap the awaited bind in upstream start().
            if self._server is not None:
                self._server.close()
                await self._server.wait_closed()
                self._server = None
            raise RuntimeError("Companion server stopped during startup")

    async def _handle_client(self, reader, writer):
        task = asyncio.current_task()
        self._client_tasks.add(task)
        self._client_writers.add(writer)
        try:
            if not self._closing:
                # A replacement connection closes prior readers immediately,
                # but must not replace shared queues until their commands and
                # cleanup finish. Otherwise an old reply reaches the new app.
                for previous in tuple(self._client_writers):
                    if previous is not writer:
                        previous.transport.abort()
                async with self._client_session_lock:
                    if not self._closing and not writer.is_closing():
                        await super()._handle_client(reader, writer)
        finally:
            try:
                # Abort wakes pending reads without waiting for an unread
                # output buffer. Undelivered replies are dropped on disconnect.
                writer.transport.abort()
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass
            finally:
                self._client_writers.discard(writer)
                self._client_tasks.discard(task)

    async def _enqueue_response_frame(self, data):
        """Wait for bounded queue space, but never outlive its writer."""
        queue = self._write_queue
        writer = self._client_writer
        writer_task = self._writer_task
        if (self._closing or queue is None or writer is None
                or writer.is_closing() or writer_task is None or writer_task.done()):
            return False
        if len(data) > MAX_PAYLOAD_SIZE:
            logger.warning(
                "Outbound frame payload too large (%s > %s); dropping frame",
                len(data), MAX_PAYLOAD_SIZE,
            )
            return False
        frame = bytes([FRAME_OUTBOUND_PREFIX]) + struct.pack("<H", len(data)) + data
        try:
            queue.put_nowait(frame)
        except asyncio.QueueFull:
            pending_put = asyncio.create_task(queue.put(frame), name="companion-contact-frame")
            try:
                done, _ = await asyncio.wait(
                    (pending_put, writer_task), return_when=asyncio.FIRST_COMPLETED,
                    timeout=self._client_idle_timeout_sec,
                )
                if not done:
                    writer.transport.abort()
                    return False
                if self._closing or writer.is_closing() or writer_task.done():
                    return False
                await pending_put
            finally:
                # Only this queue operation is ours to cancel. Cancelling the
                # writer or command could abandon unrelated persistence work.
                if not pending_put.done():
                    pending_put.cancel()
                await asyncio.gather(pending_put, return_exceptions=True)
        return not self._closing and not writer.is_closing() and not writer_task.done()

    async def _cmd_send_control_data(self, data):
        # Retain the firmware's minimum length/high-bit gate and result codes.
        # Discovery responses are broadcast by the daemon independently of
        # ControlHandler callbacks. A no-op callback here would overwrite an
        # unrelated discovery session and outlive failed sends/disconnections.
        if len(data) < 1 or (data[0] & 0x80) == 0:
            self._write_err(ERR_CODE_UNSUPPORTED_CMD)
            return
        send_control = getattr(self.bridge, "send_control_data", None)
        if not send_control:
            self._write_err(ERR_CODE_UNSUPPORTED_CMD)
            return
        try:
            ok = await send_control(data)
        except Exception as exc:
            logger.warning("Companion CONTROL send failed (%s)", type(exc).__name__)
            self._write_err(ERR_CODE_ILLEGAL_ARG)
            return
        if ok:
            self._write_ok()
        else:
            self._write_err(ERR_CODE_TABLE_FULL)

    async def _cmd_get_contacts(self, data):
        # Contact pushes must follow the complete snapshot: a deletion inserted
        # mid-dump could otherwise be undone by a later, copied contact row.
        # This output lock never prevents mutations from committing to storage.
        async with self._contact_stream_lock:
            since = struct.unpack("<I", data[:4])[0] if len(data) >= 4 else 0
            # Contacts contain immutable scalar/bytes fields. Copy their values
            # before yielding so RX updates cannot move END past unsent changes.
            # Firmware sync emits only changes strictly newer than the watermark;
            # the generic store's inclusive filter also serves non-protocol users.
            contacts = [replace(contact) for contact in self.bridge.get_contacts(since=since)
                        if contact.lastmod > since]
            total = self.bridge.get_contact_count()
            most_recent = max((contact.lastmod for contact in contacts), default=0)
            if not await self._enqueue_response_frame(
                bytes([RESP_CODE_CONTACTS_START]) + struct.pack("<I", total)
            ):
                return
            for contact in contacts:
                if not await self._enqueue_response_frame(
                    bytes([RESP_CODE_CONTACT]) + _encode_contact_fields(contact)
                ):
                    return
            await self._enqueue_response_frame(
                bytes([RESP_CODE_END_OF_CONTACTS]) + struct.pack("<I", most_recent)
            )

    def stop_admission(self):
        """Close listeners/clients immediately; leave admitted commands owned."""
        self._closing = True
        if self._server is not None:
            self._server.close()
        for writer in tuple(self._client_writers):
            writer.transport.abort()

    async def stop_clients(self):
        """Drain commands early, keeping receive-side persistence available."""
        self.stop_admission()
        if self._client_drain_task is None:
            self._client_drain_task = asyncio.create_task(
                self._drain_clients(), name=f"companion-clients-{self.port}"
            )
        await asyncio.shield(self._client_drain_task)

    async def _drain_clients(self):
        if self._server is not None:
            await self._server.wait_closed()
            self._server = None
        while self._client_tasks or self._contact_api_tasks:
            # Never cancel command tasks: an awaited SQLite worker would keep
            # running after cancellation and could overwrite a final snapshot.
            tasks = self._client_tasks | self._contact_api_tasks
            results = await asyncio.gather(*tuple(tasks), return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException):
                    logger.warning("Companion command ended with an error: %s", result)

    def _sync_next_from_persistence(self):
        """Decode persisted messages as upstream does, using the full owner key."""
        if not self.sqlite_handler:
            return None
        msg_dict = self.sqlite_handler.companion_pop_message(self._storage_key)
        if not msg_dict:
            return None
        sender_prefix = msg_dict.get("sender_prefix", b"")
        if isinstance(sender_prefix, str):
            sender_prefix = bytes.fromhex(sender_prefix) if sender_prefix else b""
        return QueuedMessage(
            sender_key=msg_dict.get("sender_key", b""),
            txt_type=msg_dict.get("txt_type", 0),
            timestamp=msg_dict.get("timestamp", 0),
            text=msg_dict.get("text", ""),
            is_channel=bool(msg_dict.get("is_channel", False)),
            channel_idx=msg_dict.get("channel_idx", 0),
            path_len=msg_dict.get("path_len", 0),
            snr=float(msg_dict.get("snr") or 0.0),
            rssi=int(msg_dict.get("rssi") or 0),
            channel_data_type=int(msg_dict.get("channel_data_type") or 0),
            channel_data_payload=bytes(msg_dict.get("channel_data_payload") or b""),
            sender_prefix=sender_prefix,
        )

    async def _persist_contact(self, contact):
        if self.sqlite_handler is not None:
            public_key = contact.public_key
            async with self._contact_persistence_lock:
                # RX may have replaced or removed this contact while its
                # callback waited. Never resurrect the callback's old object.
                current = self.bridge.contacts.get_by_key(public_key)
                if current is None or current.adv_type == ADV_TYPE_NONE:
                    return
                saved = await asyncio.to_thread(
                    self.sqlite_handler.companion_upsert_contact,
                    self._storage_key, self._contact_to_dict(current),
                )
                if not saved:
                    raise RuntimeError("Companion contact could not be saved")

    async def _save_contacts(self):
        if self.sqlite_handler is not None:
            async with self._contact_persistence_lock:
                # Snapshot only after earlier imports/upserts have completed.
                # get_contacts excludes transient anonymous identities.
                contacts = [self._contact_to_dict(contact) for contact in self.bridge.get_contacts()]
                saved = await asyncio.to_thread(
                    self.sqlite_handler.companion_save_contacts, self._storage_key, contacts
                )
                if not saved:
                    raise RuntimeError("Companion contacts could not be saved")

    def _save_and_publish_contacts(self, contacts, *, transient_contacts=None):
        """Commit a prepared replacement without yielding to contact mutations."""
        publish = self.bridge.contacts.prepare_load(
            contacts, preserve_transient=True, transient_contacts=transient_contacts,
        )
        if self.sqlite_handler is not None:
            records = [self._contact_to_dict(contact) for contact in contacts]
            # Deliberately synchronous: RX must not mutate the candidate between
            # the successful commit and its publication into the retained store.
            if not self.sqlite_handler.companion_save_contacts(self._storage_key, records):
                raise RuntimeError("Companion contacts could not be saved")
        publish()

    async def import_repeater_contacts(self, contact_types=None, hours=None, limit=None):
        """Import advert candidates as one owned, save-first contact replacement."""
        task = asyncio.current_task()
        self._contact_api_tasks.add(task)
        try:
            if self._closing:
                raise RuntimeError("Companion server is stopping")
            if self.sqlite_handler is None:
                raise RuntimeError("Companion contact import requires SQLite storage")
            if limit is not None and (
                not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0
            ):
                raise ValueError("Contact import limit must be a positive integer")
            capacity = self.bridge.contacts.max_contacts
            async with self._contact_persistence_lock:
                if self._closing:
                    raise RuntimeError("Companion server is stopping")
                rows = await asyncio.to_thread(
                    self.sqlite_handler.companion_load_repeater_contacts,
                    contact_types=contact_types, hours=hours,
                    limit=min(limit or capacity, capacity),
                )
                if self._closing:
                    raise RuntimeError("Companion server is stopping")
                # Snapshot the live store after the read, not before an await
                # during which RX may have learned newer contact information.
                previous_keys = {contact.public_key for contact in self.bridge.get_contacts()}
                contacts, stats = prepare_contact_import(
                    self.bridge.contacts, rows, now=int(time.time()),
                )
                removed = updated = ()
                if stats["imported"] or stats["removed"]:
                    current_keys = {contact.public_key for contact in contacts}
                    removed = tuple(sorted(previous_keys - current_keys))
                    updated = tuple(sorted(current_keys - previous_keys))
                    self._save_and_publish_contacts(contacts)
            if removed or updated:
                await self._notify_contact_changes(removed=removed, updated=updated)
            return stats
        finally:
            self._contact_api_tasks.discard(task)

    async def reset_contact_path(self, pubkey):
        """Persist and publish a real contact's unknown path before reporting success."""
        task = asyncio.current_task()
        self._contact_api_tasks.add(task)
        try:
            if self._closing:
                raise RuntimeError("Companion server is stopping")
            async with self._contact_persistence_lock:
                if self._closing:
                    raise RuntimeError("Companion server is stopping")
                current = self.bridge.contacts.get_by_key(pubkey)
                if current is None or current.adv_type == ADV_TYPE_NONE:
                    return False
                contacts = [replace(contact) for contact in self.bridge.get_contacts()]
                lastmod = max(
                    int(time.time()), max((contact.lastmod for contact in contacts), default=0) + 1,
                )
                if lastmod > 0xFFFFFFFF:
                    raise ValueError("Contact path reset exceeds the uint32 sync watermark")
                for contact in contacts:
                    if contact.public_key == pubkey:
                        contact.out_path_len = -1
                        contact.out_path = b""
                        contact.lastmod = lastmod
                        break
                self._save_and_publish_contacts(contacts)
            await self._notify_contact_changes(path_updated=(pubkey,))
            return True
        finally:
            self._contact_api_tasks.discard(task)

    def _channel_snapshot(self):
        channels = []
        for index in range(self.bridge.channels.max_channels):
            channel = self.bridge.get_channel(index)
            if channel is not None:
                channels.append({"channel_idx": index, "name": channel.name, "secret": channel.secret})
        return channels

    async def _save_channels(self):
        if self.sqlite_handler is not None:
            async with self._channel_persistence_lock:
                channels = self._channel_snapshot()
                saved = await asyncio.to_thread(
                    self.sqlite_handler.companion_save_channels, self._storage_key, channels
                )
                if not saved:
                    raise RuntimeError("Companion channels could not be saved")

    async def _cmd_set_channel(self, data):
        # Keep the existing raw-16, raw-32 and ASCII-hex-32 formats, but never
        # silently ignore trailing bytes or strip meaningful name whitespace.
        if len(data) not in (49, 65, 97):
            await self._enqueue_response_frame(bytes([RESP_CODE_ERR, ERR_CODE_ILLEGAL_ARG]))
            return
        index = data[0]
        capacity = self.bridge.channels.max_channels
        if index >= capacity:
            await self._enqueue_response_frame(bytes([RESP_CODE_ERR, ERR_CODE_NOT_FOUND]))
            return
        name = data[1:1 + CHANNEL_NAME_SIZE].split(b"\x00", 1)[0].decode("utf-8", errors="replace")
        try:
            secret = bytes.fromhex(data[33:].decode("ascii")) if len(data) == 97 else data[33:]
        except (ValueError, UnicodeDecodeError):
            await self._enqueue_response_frame(bytes([RESP_CODE_ERR, ERR_CODE_ILLEGAL_ARG]))
            return

        async with self._channel_persistence_lock:
            if self._closing:
                response = bytes([RESP_CODE_ERR, ERR_CODE_BAD_STATE])
            else:
                try:
                    channels = [row for row in self._channel_snapshot() if row["channel_idx"] != index]
                    channels.append({"channel_idx": index, "name": name, "secret": secret})
                    channels = validated_channel_rows(channels, max_channels=capacity)
                    requested = channels[-1]
                    prepared = Channel(name=requested["name"], secret=requested["secret"])
                except (ValueError, TypeError):
                    response = bytes([RESP_CODE_ERR, ERR_CODE_ILLEGAL_ARG])
                else:
                    try:
                        # No await between committing and publishing. Packet
                        # handlers retain this store and their existing caches.
                        if self.sqlite_handler is not None:
                            if not self.sqlite_handler.companion_save_channels(self._storage_key, channels):
                                raise RuntimeError("Companion channels could not be saved")
                    except Exception as exc:
                        logger.warning("Companion channel save failed (%s)", type(exc).__name__)
                        response = bytes([RESP_CODE_ERR, ERR_CODE_FILE_IO_ERROR])
                    else:
                        self.bridge.channels.set(index, prepared)
                        try:
                            self.bridge._schedule_fire_callbacks("channel_updated", index, prepared)
                        except Exception as exc:
                            # Persistence and publication already succeeded;
                            # notification failure must not report a failed edit.
                            logger.warning("Companion channel notification failed (%s)", type(exc).__name__)
                        response = bytes([RESP_CODE_OK])
        # Backpressure may yield; the channel transaction is already complete.
        await self._enqueue_response_frame(response)

    async def stop(self):
        self.stop_admission()
        if self._stop_task is None:
            self._stop_task = asyncio.create_task(self._finish_stop(), name=f"companion-stop-{self.port}")
        # Retain the operation even if its caller is cancelled while SQLite is
        # saving. Another stop() awaits the same owned task, not a duplicate save.
        await asyncio.shield(self._stop_task)

    async def _finish_stop(self):
        await self.stop_clients()
        failures = []
        for save in (self._flush_message_fallbacks, self._save_contacts, self._save_channels):
            try:
                await save()
            except Exception as exc:
                failures.append(exc)
                logger.warning("Final companion snapshot failed: %s", exc)
        # The repeater wrapper groups both saves in one try and swallows their
        # errors. We performed them above; now use its core transport cleanup.
        await _CoreFrameServer.stop(self)
        if failures:
            raise RuntimeError("Companion stopped, but final persistence was incomplete") from failures[0]
