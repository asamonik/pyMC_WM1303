"""Save-first companion TCP contact mutations over the existing frame protocol."""

import logging
import struct
import time
from dataclasses import replace

from openhop_core.companion.constants import (
    ADV_TYPE_NONE,
    AUTOADD_OVERWRITE_OLDEST,
    ERR_CODE_BAD_STATE,
    ERR_CODE_FILE_IO_ERROR,
    ERR_CODE_ILLEGAL_ARG,
    ERR_CODE_NOT_FOUND,
    ERR_CODE_TABLE_FULL,
    MAX_ANON_CONTACTS,
    PUSH_CODE_CONTACT_DELETED,
    RESP_CODE_ERR,
    RESP_CODE_OK,
)
from openhop_core.companion.contact_store import ContactStore
from openhop_core.companion.models import Contact

logger = logging.getLogger(__name__)


def _parse_contact_update(data, existing):
    """Decode the complete fixed body and only fully supplied optional fields."""
    if len(data) < 135:
        raise ValueError("Contact update requires its complete 135-byte body")
    public_key = bytes(data[:32])
    base = existing if existing is not None else Contact(public_key=public_key)
    encoded_path_len = data[34]
    candidate = replace(
        base,
        public_key=public_key,
        adv_type=data[32],
        flags=data[33],
        out_path_len=-1 if encoded_path_len == 255 else encoded_path_len,
        # The firmware copies all 64 bytes, including unused path-buffer tails.
        out_path=bytes(data[35:99]),
        name=bytes(data[99:131]).split(b"\x00", 1)[0].decode("utf-8", errors="replace"),
        last_advert_timestamp=struct.unpack_from("<I", data, 131)[0],
        # Explicit zero is valid; do not go through add_update_contact(), which
        # treats zero as an unspecified modification time.
        lastmod=struct.unpack_from("<I", data, 143)[0]
        if len(data) >= 147 else int(time.time()),
    )
    if len(data) >= 143:
        candidate.gps_lat = struct.unpack_from("<i", data, 135)[0] / 1e6
        candidate.gps_lon = struct.unpack_from("<i", data, 139)[0] / 1e6
    # Existing sync state, raw advert and signal values remain on the detached
    # candidate, as does existing GPS when its optional fields were omitted.
    staged = ContactStore(max_contacts=1)
    staged.load_from([candidate])
    return staged.get_all()[0]


class PersistentContactCommandsMixin:
    """Publish contact commands only after the real-contact snapshot is saved."""

    def _contact_pools(self):
        real = {}
        transient = {}
        for contact in self.bridge.contacts.get_all():
            candidate = replace(contact)
            pool = transient if candidate.adv_type == ADV_TYPE_NONE else real
            pool[candidate.public_key] = candidate
        return real, transient

    async def _contact_command_response(self, error):
        response = bytes([RESP_CODE_OK]) if error is None else bytes([RESP_CODE_ERR, error])
        # This await is outside the mutation lock and commit error handling. A
        # disconnected client must not turn an already saved mutation into ERR.
        await self._enqueue_response_frame(response)

    async def _cmd_add_update_contact(self, data):
        error = None
        overwritten = None
        async with self._contact_persistence_lock:
            if self._closing:
                error = ERR_CODE_BAD_STATE
            else:
                try:
                    if len(data) < 135:
                        raise ValueError("Contact update requires its complete 135-byte body")
                    real, transient = self._contact_pools()
                    public_key = bytes(data[:32])
                    existing = real.get(public_key)
                    if existing is None:
                        existing = transient.get(public_key)
                    candidate = _parse_contact_update(data, existing)
                    # Remove from both candidate pools first: promotions and
                    # demotions cannot leave the same key in two places.
                    real.pop(public_key, None)
                    transient.pop(public_key, None)
                    if candidate.adv_type == ADV_TYPE_NONE:
                        if len(transient) >= MAX_ANON_CONTACTS:
                            oldest = min(transient, key=lambda key: transient[key].lastmod)
                            del transient[oldest]
                        transient[public_key] = candidate
                    else:
                        if len(real) >= self.bridge.contacts.max_contacts:
                            overwritable = [contact for contact in real.values()
                                            if not contact.flags & 0x01]
                            if (self.bridge.prefs.autoadd_config & AUTOADD_OVERWRITE_OLDEST
                                    and overwritable):
                                oldest = min(overwritable, key=lambda contact: contact.lastmod)
                                del real[oldest.public_key]
                                overwritten = oldest.public_key
                            else:
                                error = ERR_CODE_TABLE_FULL
                        if error is None:
                            real[public_key] = candidate
                    if error is None:
                        # Exact transient replacement also removes an old anon
                        # on promotion, removal or bounded-pool eviction.
                        self._save_and_publish_contacts(
                            list(real.values()), transient_contacts=list(transient.values())
                        )
                except ValueError:
                    error = ERR_CODE_ILLEGAL_ARG
                except RuntimeError:
                    logger.warning("Companion contact update could not be persisted", exc_info=True)
                    error = ERR_CODE_FILE_IO_ERROR
        if error is None and overwritten is not None:
            # Incremental contact sync cannot express deletions. Inform the app
            # only after the replacement committed, as firmware overwrite does.
            await self._enqueue_response_frame(bytes([PUSH_CODE_CONTACT_DELETED]) + overwritten)
        await self._contact_command_response(error)

    async def _change_existing_contact(self, data, *, reset_path):
        error = None
        async with self._contact_persistence_lock:
            if self._closing:
                error = ERR_CODE_BAD_STATE
            else:
                try:
                    if len(data) < 32:
                        raise ValueError("Contact command requires a 32-byte public key")
                    real, transient = self._contact_pools()
                    public_key = bytes(data[:32])
                    pool = real if public_key in real else transient
                    if public_key not in pool:
                        error = ERR_CODE_NOT_FOUND
                    else:
                        if reset_path:
                            # Firmware reset changes neither the buffer nor its
                            # lastmod: the requesting app already knows the edit.
                            pool[public_key] = replace(pool[public_key], out_path_len=-1)
                        else:
                            # Removing the Contact also removes its raw advert;
                            # the next real-contact snapshot cannot retain it.
                            del pool[public_key]
                        self._save_and_publish_contacts(
                            list(real.values()), transient_contacts=list(transient.values())
                        )
                except ValueError:
                    error = ERR_CODE_ILLEGAL_ARG
                except RuntimeError:
                    logger.warning("Companion contact mutation could not be persisted", exc_info=True)
                    error = ERR_CODE_FILE_IO_ERROR
        await self._contact_command_response(error)

    async def _cmd_remove_contact(self, data):
        await self._change_existing_contact(data, reset_path=False)

    async def _cmd_reset_path(self, data):
        await self._change_existing_contact(data, reset_path=True)
