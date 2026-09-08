"""Save-first handling of verified companion adverts, independent of TCP clients."""

import time

from openhop_core.companion.constants import (
    ADV_TYPE_NONE,
    MAX_ANON_CONTACTS,
)
from openhop_core.companion.models import AdvertPath
from openhop_core.protocol.packet_utils import PathUtils

from .contact_adverts import contact_from_advert_event


class AdvertPersistenceMixin:
    """Serialize advert admission with contact commands and persistence callbacks."""

    async def _handle_advert_event(self, data):
        contact, has_location, inbound, encoded = contact_from_advert_event(data)
        public_key = contact.public_key
        deleted = None
        contacts_full = False
        accepted = False
        async with self._contact_persistence_lock:
            real, transient = self._contact_pools()
            existing_real = real.get(public_key)
            existing = existing_real if existing_real is not None else transient.get(public_key)
            # Another advert may have committed while this event waited. Check
            # the original wire timestamp against that latest state under lock.
            if (existing is not None
                    and contact.last_advert_timestamp <= existing.last_advert_timestamp):
                return

            # Reception paths are informational observations, not outbound
            # contact routes. They may survive a subsequent persistence failure.
            self.bridge.path_cache.update(AdvertPath(
                public_key_prefix=public_key[:7], name=contact.name,
                path_len=encoded, path=inbound, recv_timestamp=int(time.time()),
            ))

            # Firmware clears an old anonymous recipient before applying the
            # new-contact filters. Never promote its temporary route or flags.
            removed_transient = transient.pop(public_key, None)
            if existing_real is not None:
                contact.out_path_len = existing_real.out_path_len
                contact.out_path = existing_real.out_path
                contact.flags = existing_real.flags
                contact.sync_since = existing_real.sync_since
                if contact.last_advert_packet is None:
                    contact.last_advert_packet = existing_real.last_advert_packet
                if has_location is not True:
                    contact.gps_lat = existing_real.gps_lat
                    contact.gps_lon = existing_real.gps_lon
                del real[public_key]
                accepted = True
            else:
                max_hops = self.bridge.prefs.autoadd_max_hops
                accepted = (
                    self.bridge.should_auto_add_contact_type(contact.adv_type)
                    and (max_hops == 0 or PathUtils.get_path_hash_count(encoded) < max_hops)
                )

            if accepted:
                if contact.adv_type == ADV_TYPE_NONE:
                    if len(transient) >= MAX_ANON_CONTACTS:
                        oldest = min(transient, key=lambda key: transient[key].lastmod)
                        del transient[oldest]
                    transient[public_key] = contact
                    if existing_real is not None:
                        deleted = public_key
                else:
                    if len(real) >= self.bridge.contacts.max_contacts:
                        overwritable = [entry for entry in real.values() if not entry.flags & 0x01]
                        if self.bridge.should_overwrite_when_full() and overwritable:
                            oldest = min(overwritable, key=lambda entry: entry.lastmod)
                            deleted = oldest.public_key
                            del real[deleted]
                        else:
                            contacts_full = True
                            accepted = False
                    if accepted:
                        real[public_key] = contact

            if accepted:
                # Prepare all validation/proxy work before any SQL. Publication
                # retains the Store object used by the live packet handlers.
                real_contacts = list(real.values())
                publish = self.bridge.contacts.prepare_load(
                    real_contacts, transient_contacts=list(transient.values()),
                )
                if self.sqlite_handler is not None:
                    try:
                        if deleted is not None:
                            # Eviction/demotion must delete and replace in one
                            # transaction; an upsert alone leaves the old peer.
                            saved = self.sqlite_handler.companion_save_contacts(
                                self._storage_key,
                                [self._contact_to_dict(entry) for entry in real_contacts],
                            )
                        elif contact.adv_type != ADV_TYPE_NONE:
                            saved = self.sqlite_handler.companion_upsert_contact(
                                self._storage_key, self._contact_to_dict(contact),
                            )
                        else:
                            saved = True  # Anonymous recipients are RAM-only.
                        if not saved:
                            raise RuntimeError("Companion advert could not be saved")
                    except Exception as exc:
                        raise RuntimeError("Companion advert could not be saved") from exc
                # No await between commit and publication: contact callbacks or
                # RX cannot change the prepared state during this transaction.
                publish()
            elif removed_transient is not None:
                # A filtered/full advert still invalidates its old anonymous
                # recipient, but never rewrites unchanged real-contact rows.
                self.bridge.contacts.prepare_load(
                    list(real.values()), transient_contacts=list(transient.values()),
                )()

        # Persistence does not depend on a connected frame client. Notifications
        # run after publication and outside the lock, including for filtered RX.
        if deleted is not None:
            await self.bridge._fire_callbacks("contact_deleted", deleted)
        if accepted:
            await self.bridge._fire_callbacks("advert_received", contact)
        # Earlier notifications can yield to a later mutation. Let the frame
        # callback inspect the current book: a now-evicted peer needs the full
        # discovery frame, not a short advert that prompts a missing-key fetch.
        await self.bridge._fire_callbacks("node_discovered", contact)
        if contacts_full:
            await self.bridge._fire_callbacks("contacts_full")

    async def _on_advert_received(self, contact):
        """The advert owner already saved this update before invoking callbacks."""
