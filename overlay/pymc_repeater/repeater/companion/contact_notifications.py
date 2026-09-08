"""Bounded, connection-owned contact notifications ordered after contact dumps."""

import logging

from openhop_core.companion.constants import (
    ADV_TYPE_NONE,
    PUSH_CODE_ADVERT,
    PUSH_CODE_CONTACT_DELETED,
    PUSH_CODE_PATH_UPDATED,
)
from openhop_core.companion.frame_server.frames import _build_advert_push_frames
from openhop_core.companion.models import Contact

logger = logging.getLogger(__name__)


class ContactNotificationsMixin:
    """Publish hints for committed state without another persistence callback."""

    def _contact_notification_client_is_current(self, writer):
        return (writer is not None and self._client_writer is writer
                and not self._closing and not writer.is_closing())

    async def _notify_contact_changes(self, *, removed=(), updated=(), path_updated=()):
        writer = self._client_writer
        if not self._contact_notification_client_is_current(writer):
            return
        try:
            # A dump must finish sending its old snapshot before its changes
            # are announced. Never hold the persistence lock during this wait.
            async with self._contact_stream_lock:
                for keys, notification in (
                    (removed, PUSH_CODE_CONTACT_DELETED),
                    (updated, PUSH_CODE_ADVERT),
                    (path_updated, PUSH_CODE_PATH_UPDATED),
                ):
                    for public_key in keys:
                        if not self._contact_notification_client_is_current(writer):
                            return
                        if not isinstance(public_key, bytes) or len(public_key) != 32:
                            continue
                        current = self.bridge.contacts.get_by_key(public_key)
                        is_real = current is not None and current.adv_type != ADV_TYPE_NONE
                        code = notification
                        if notification == PUSH_CODE_CONTACT_DELETED:
                            # The key may have been reintroduced while a dump
                            # held the stream lock. Refresh it instead of
                            # deleting the client's now-current contact.
                            if is_real:
                                code = PUSH_CODE_ADVERT
                        elif notification == PUSH_CODE_PATH_UPDATED:
                            if current is None:
                                continue
                        elif not is_real:
                            continue
                        if not await self._enqueue_response_frame(bytes([code]) + public_key):
                            return
        except Exception as exc:
            # Notification failure cannot undo a completed database commit.
            logger.warning("Companion contact notification failed (%s)", type(exc).__name__)

    async def _on_contact_deleted(self, public_key):
        await self._notify_contact_changes(removed=(public_key,))

    async def _on_contact_path_updated(self, contact):
        public_key = getattr(contact, "public_key", None)
        await self._notify_contact_changes(path_updated=(public_key,))

    async def _on_node_discovered(self, contact_or_data):
        writer = self._client_writer
        if not self._contact_notification_client_is_current(writer):
            return
        try:
            if isinstance(contact_or_data, Contact):
                contact = contact_or_data
            elif isinstance(contact_or_data, dict):
                contact = Contact.from_dict(contact_or_data)
            else:
                return
            if (not contact.name or not isinstance(contact.public_key, bytes)
                    or len(contact.public_key) != 32):
                return
            async with self._contact_stream_lock:
                if not self._contact_notification_client_is_current(writer):
                    return
                current = self.bridge.contacts.get_by_key(contact.public_key)
                is_stored = current is not None
                # These tiny encoders are synchronous: classification and
                # frame contents describe the same state before output yields.
                short, full = _build_advert_push_frames(current if is_stored else contact)
                frame = short if is_stored else full
                if frame is not None:
                    await self._enqueue_response_frame(frame)
        except Exception as exc:
            logger.warning("Companion advert notification failed (%s)", type(exc).__name__)
