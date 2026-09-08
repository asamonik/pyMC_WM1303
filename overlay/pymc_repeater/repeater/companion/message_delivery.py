"""Serialized companion inbox persistence and delivery admission."""

import asyncio
import logging
import time
from dataclasses import fields, replace

from openhop_core.companion.constants import (
    ADV_TYPE_NONE,
    ERR_CODE_FILE_IO_ERROR,
    MAX_PAYLOAD_SIZE,
    PUSH_CODE_MSG_WAITING,
    RESP_CODE_ERR,
    RESP_CODE_NO_MORE_MESSAGES,
    TXT_TYPE_PLAIN,
    TXT_TYPE_SIGNED_PLAIN,
)
from openhop_core.companion.models import QueuedMessage

logger = logging.getLogger(__name__)


class MessageDeliveryMixin:
    async def _handle_message_event(self, event):
        """Own private-message persistence before connection-specific callbacks."""
        if event.queued:
            await self._persist_companion_message({
                "sender_key": event.sender_key,
                "text": event.text,
                "timestamp": event.timestamp,
                "txt_type": event.txt_type,
                "is_channel": False,
                "channel_idx": 0,
                "path_len": event.path_len,
                "packet_hash": event.packet_hash,
                "snr": event.snr,
                "rssi": event.rssi,
                "sender_prefix": event.sender_prefix,
            }, event.queue_entry)

    async def _on_message_event(self, event):
        # This public callback may run with the ingress lock held. Persistence
        # belongs to the permanent owner above; never wait for queue space here.
        self._enqueue_frame(bytes([PUSH_CODE_MSG_WAITING]))

    def _message_queue_snapshot(self):
        # The pinned core has no public iterator. Inspect its deque read-only;
        # all producer/consumer mutations are serialized by the owner's lock.
        entries = tuple(self.bridge.message_queue._queue)
        live = {id(entry): entry for entry in entries}
        for key, (entry, _) in tuple(self._pending_message_fallbacks.items()):
            if live.get(key) is not entry:
                del self._pending_message_fallbacks[key]
        return entries

    async def _save_message_with_contact(self, message):
        """Merge receive metadata at retry time and commit it with the message."""
        retention = self.bridge.message_queue.max_size
        if (message.get("is_channel", False)
                or message.get("txt_type") not in (TXT_TYPE_PLAIN, TXT_TYPE_SIGNED_PLAIN)):
            if self.sqlite_handler is None:
                return True
            return await asyncio.to_thread(
                self.sqlite_handler.companion_push_message,
                self._storage_key, message, retention,
            )

        # The caller already holds the message lock. Every contact operation
        # uses this order; no notification or message-lock wait occurs inside.
        async with self._contact_persistence_lock:
            real, transient = self._contact_pools()
            public_key = message.get("sender_key")
            if isinstance(public_key, (bytes, bytearray, memoryview)):
                public_key = bytes(public_key)
            else:
                public_key = None
            pool = real if public_key in real else transient
            current = pool.get(public_key)
            publish = None
            contact_update = None
            if current is not None:
                sync_since = current.sync_since
                if message["txt_type"] == TXT_TYPE_SIGNED_PLAIN:
                    sync_since = max(sync_since, message["timestamp"])
                contact = replace(current, lastmod=int(time.time()), sync_since=sync_since)
                pool[public_key] = contact
                publish = self.bridge.contacts.prepare_load(
                    list(real.values()), transient_contacts=list(transient.values()),
                )
                if contact.adv_type != ADV_TYPE_NONE:
                    contact_update = self._contact_to_dict(contact)
            # A removed sender never gets recreated. Anonymous metadata remains
            # RAM-only; a real sender snapshot shares the message transaction.
            if self.sqlite_handler is not None:
                saved = self.sqlite_handler.companion_push_message(
                    self._storage_key, message, retention, contact_update=contact_update,
                )
                if not saved:
                    return False
            # No await between the successful commit and same-store publication.
            if publish is not None:
                publish()
            return True

    async def _flush_pending_messages(self):
        """Try the live RAM inbox once, oldest first; caller holds the lock."""
        if self.sqlite_handler is None:
            return
        for entry in self._message_queue_snapshot():
            pending = self._pending_message_fallbacks.get(id(entry))
            if pending is None or pending[0] is not entry:
                # An older entry without metadata must not be overtaken by a
                # later SQL insert. It remains readable through RAM delivery.
                return
            try:
                saved = await self._save_message_with_contact(pending[1])
            except Exception as exc:
                logger.warning("Companion message save failed (%s)", type(exc).__name__)
                return
            if not saved:
                return
            self.bridge.message_queue.remove(entry)
            self._pending_message_fallbacks.pop(id(entry), None)

    async def _persist_companion_message(self, msg_dict, queue_entry=None):
        # The bridge owns the lock before Queue.push and across its callbacks;
        # reacquiring it here would deadlock every received message.
        entries = self._message_queue_snapshot()
        if queue_entry is not None and any(entry is queue_entry for entry in entries):
            if self.sqlite_handler is None:
                # Memory-only operation publishes receive metadata only after
                # the protected RAM inbox actually accepted this exact entry.
                await self._save_message_with_contact(msg_dict)
                return
            self._pending_message_fallbacks[id(queue_entry)] = (queue_entry, dict(msg_dict))
        else:
            logger.warning("Companion message has no live queue entry; retaining RAM inbox")
        await self._flush_pending_messages()

    def _delivery_message_frame(self, message):
        frame = self._build_message_frame(message)
        if not isinstance(frame, bytes) or not frame or len(frame) > MAX_PAYLOAD_SIZE:
            raise ValueError("Companion message cannot fit its response frame")
        return frame

    async def _cmd_sync_next_message(self, data):
        async with self._message_persistence_lock:
            await self._flush_pending_messages()
            record = None
            if self.sqlite_handler is not None:
                try:
                    record = await asyncio.to_thread(
                        self.sqlite_handler.companion_peek_message, self._storage_key,
                    )
                except Exception as exc:
                    logger.warning("Companion message read failed (%s)", type(exc).__name__)
                    await self._enqueue_response_frame(bytes([RESP_CODE_ERR, ERR_CODE_FILE_IO_ERROR]))
                    return

            message_id = None
            try:
                if record is not None:
                    message_id = record["id"]
                    if type(message_id) is not int or message_id <= 0:
                        raise ValueError("Companion message has an invalid stored id")
                    # The SQL reader already decodes binary fields, including
                    # signed author prefixes and channel-data payloads.
                    message = QueuedMessage(**{
                        field.name: record[field.name]
                        for field in fields(QueuedMessage) if field.name in record
                    })
                else:
                    message = self.bridge.message_queue.peek()
                frame = self._delivery_message_frame(message) if message is not None else None
            except Exception as exc:
                logger.warning("Companion message encoding failed (%s)", type(exc).__name__)
                await self._enqueue_response_frame(bytes([RESP_CODE_ERR, ERR_CODE_FILE_IO_ERROR]))
                return

            if message is None:
                await self._enqueue_response_frame(bytes([RESP_CODE_NO_MORE_MESSAGES]))
                return

            # Disconnect/queue rejection before admission leaves the head intact.
            # This protocol has no receipt ACK, so admission is the commit point
            # for delivery; a later disconnect cannot guarantee receipt.
            if not await self._enqueue_response_frame(frame):
                return
            if message_id is not None:
                try:
                    deleted = await asyncio.to_thread(
                        self.sqlite_handler.companion_delete_message,
                        self._storage_key, message_id,
                    )
                    if not deleted:
                        raise RuntimeError("Companion queued message was not deleted")
                except Exception as exc:
                    # The message reply is already queued: never send a second
                    # response. Retain the durable row and expose duplicate risk.
                    logger.warning(
                        "Companion message admitted but deletion failed; delivery may repeat (%s)",
                        type(exc).__name__,
                    )
            else:
                self.bridge.message_queue.remove(message)
                self._pending_message_fallbacks.pop(id(message), None)

    async def _flush_message_fallbacks(self):
        """Drain accepted RAM fallbacks once before final contact snapshots."""
        if self.sqlite_handler is None:
            return
        async with self._message_persistence_lock:
            await self._flush_pending_messages()
            if self._message_queue_snapshot():
                raise RuntimeError("Companion messages remain unsaved in the RAM inbox")
