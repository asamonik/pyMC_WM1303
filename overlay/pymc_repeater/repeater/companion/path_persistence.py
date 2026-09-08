"""Save-first contact route updates for the retained companion protocol handlers."""

import time
from dataclasses import replace

from openhop_core.companion.constants import ADV_TYPE_NONE
from openhop_core.protocol.packet_utils import PathUtils


class PathPersistenceMixin:
    async def _handle_contact_path_update(self, pub, path_len, path_bytes):
        """Persist a known peer's learned route before publishing or notifying."""
        if not isinstance(pub, (bytes, bytearray, memoryview)):
            raise ValueError("Contact path update requires a 32-byte public key")
        public_key = bytes(pub)
        if len(public_key) != 32:
            raise ValueError("Contact path update requires a 32-byte public key")
        if (not isinstance(path_len, int) or isinstance(path_len, bool)
                or not 0 <= path_len <= 255 or not PathUtils.is_valid_path_len(path_len)):
            raise ValueError("Contact path update has an invalid encoded length")
        required = PathUtils.get_path_byte_len(path_len)
        if not isinstance(path_bytes, (bytes, bytearray, memoryview)):
            raise ValueError("Contact path bytes do not match their encoded length")
        path_bytes = bytes(path_bytes)
        if len(path_bytes) != required:
            raise ValueError("Contact path bytes do not match their encoded length")

        async with self._contact_persistence_lock:
            # Commands/adverts may have replaced, demoted or removed the peer
            # while this authenticated PATH waited for earlier persistence.
            real, transient = self._contact_pools()
            pool = real if public_key in real else transient
            current = pool.get(public_key)
            if current is None:
                return False
            contact = replace(
                current,
                out_path_len=path_len,
                # Firmware copyPath overwrites only the route's active prefix.
                # Retain any unused persisted buffer bytes, even for zero hops.
                out_path=path_bytes + current.out_path[required:],
                lastmod=int(time.time()),
            )
            pool[public_key] = contact
            publish = self.bridge.contacts.prepare_load(
                list(real.values()), transient_contacts=list(transient.values()),
            )
            if self.sqlite_handler is not None and contact.adv_type != ADV_TYPE_NONE:
                try:
                    saved = self.sqlite_handler.companion_upsert_contact(
                        self._storage_key, self._contact_to_dict(contact),
                    )
                except Exception as exc:
                    raise RuntimeError("Companion contact path could not be saved") from exc
                if not saved:
                    raise RuntimeError("Companion contact path could not be saved")
            # No await between successful persistence and same-store publication.
            # Anonymous recipients remain RAM-only, but use the same validation.
            publish()

        # A slow client must not delay this authenticated PATH's embedded
        # response/ACK or reciprocal route. Bridge shutdown owns this task;
        # the notifier rechecks current contact/client state before sending.
        self.bridge._schedule_fire_callbacks("contact_path_updated", contact)
        return True
