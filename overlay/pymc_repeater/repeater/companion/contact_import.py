"""Pure preparation of a one-time companion contact seed from repeater adverts."""

import math
from dataclasses import replace

from openhop_core.companion.constants import ADV_TYPE_NONE
from openhop_core.companion.contact_store import ContactStore
from openhop_core.companion.models import Contact

from .utils import select_companion_contacts_to_trim

_ADVERT_TYPES = {"companion": 1, "repeater": 2, "room_server": 3, "sensor": 4}
_ROW_FIELDS = ("pubkey", "node_name", "contact_type", "latitude", "longitude", "last_seen")
_UINT32_MAX = 0xFFFFFFFF


def prepare_contact_import(store, rows, *, now: int) -> tuple[list[Contact], dict]:
    """Return validated real contacts and counts without changing live/storage state.

    Source rows must be newest-first and use canonical contact type names.
    Existing real contacts are not refreshed from potentially stale adverts.
    Transient keys may become real contacts; the caller preserves other transient
    contacts when publishing this returned real-contact collection.
    """
    if not isinstance(now, int) or isinstance(now, bool) or now < 0:
        raise ValueError("Contact import time must be a nonnegative integer")

    existing_store = ContactStore(max_contacts=store.max_contacts)
    existing_store.load_from(
        contact for contact in store.get_all() if contact.adv_type != ADV_TYPE_NONE
    )
    existing = existing_store.get_all()
    existing_keys = {contact.public_key for contact in existing}

    source_store = ContactStore(max_contacts=1)
    source_times = {}
    additions = []
    row_count = 0
    for row in rows:
        if not isinstance(row, dict) or any(field not in row for field in _ROW_FIELDS):
            raise ValueError("Contact import rows must contain the canonical advert fields")
        contact_type = row["contact_type"]
        if not isinstance(contact_type, str) or contact_type not in _ADVERT_TYPES:
            raise ValueError("Contact import row has an unsupported advert type")
        last_seen = row["last_seen"]
        if not isinstance(last_seen, (int, float)) or isinstance(last_seen, bool):
            raise ValueError("Contact import last_seen must be a finite nonnegative number")
        try:
            valid_time = math.isfinite(last_seen) and last_seen >= 0
        except OverflowError:
            valid_time = False
        if not valid_time:
            raise ValueError("Contact import last_seen must be a finite nonnegative number")

        # Validate even existing/duplicate keys. A malformed source row must not
        # silently disappear merely because another row would win selection.
        source_store.load_from([
            Contact(
                public_key=row["pubkey"],
                name="" if row["node_name"] is None else row["node_name"],
                adv_type=_ADVERT_TYPES[contact_type],
                gps_lat=0.0 if row["latitude"] is None else row["latitude"],
                gps_lon=0.0 if row["longitude"] is None else row["longitude"],
                # last_seen is local reception time, not a signed ADVERT time.
                # Keep remote timestamp, raw advert, route, flags and sync state
                # at their unknown/default values for a newly seeded contact.
            )
        ])
        candidate = source_store.get_all()[0]
        row_count += 1
        if candidate.public_key in source_times:
            continue
        source_times[candidate.public_key] = last_seen
        if candidate.public_key not in existing_keys:
            additions.append(candidate)

    # The import must be visible beyond an already-synced contact watermark,
    # including a same-second import or a backwards local clock adjustment.
    import_time = max(now, max((contact.lastmod for contact in existing), default=0) + 1)
    new_keys = {contact.public_key for contact in additions}
    # Selection freshness is independent of the later sync timestamp. Otherwise
    # retrying one import would give its previously skipped older rows a newer
    # lastmod and rotate out the peers retained on the first attempt. Matching
    # source times also protect retained peers when the local clock moved back.
    # The trim helper keeps a stable sort's tail: existing contacts go last so
    # they win ties; new contacts remain oldest-source-first to retain newest.
    selection = [
        {"contact": contact, "flags": contact.flags,
         "lastmod": source_times[contact.public_key]
         if contact.public_key in new_keys else max(
             contact.lastmod, source_times.get(contact.public_key, contact.lastmod)
         )}
        for contact in list(reversed(additions)) + existing
    ]
    kept, _ = select_companion_contacts_to_trim(selection, store.max_contacts)
    kept_keys = {item["contact"].public_key for item in kept}
    imported = len(kept_keys & new_keys)
    if imported and import_time > _UINT32_MAX:
        raise ValueError("Contact import modification time exceeds the uint32 sync watermark")

    # Only actual additions need a new timestamp. A no-op or fully protected
    # table remains valid even when its existing watermark is already uint32max.
    result = [
        replace(item["contact"], lastmod=import_time)
        if item["contact"].public_key in new_keys else item["contact"]
        for item in kept
    ]
    final_store = ContactStore(max_contacts=store.max_contacts)
    final_store.load_from(result)
    return final_store.get_all(), {
        "imported": imported,
        "removed": len(existing_keys - kept_keys),
        "skipped": row_count - imported,
    }
