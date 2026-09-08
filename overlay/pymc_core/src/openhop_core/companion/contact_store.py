"""In-memory contact storage compatible with MeshNode's contacts interface."""

from __future__ import annotations

import math
from dataclasses import fields, replace
from typing import Callable, Iterable, Iterator, Optional, Tuple

from ..protocol.packet_utils import PathUtils
from .constants import ADV_TYPE_NONE, DEFAULT_MAX_CONTACTS, MAX_ANON_CONTACTS
from .models import Contact


class ContactProxy:
    """Wraps a Contact to provide the interface expected by MeshNode handlers.

    The existing handlers expect contacts with:
    - public_key as a hex string (not bytes)
    - name as a string
    - out_path as a list
    - type as an int
    """

    def __init__(self, contact: Contact):
        self._contact = contact
        self.public_key = contact.public_key.hex()
        self.name = contact.name
        self.type = contact.adv_type
        self.flags = contact.flags
        self.out_path = list(contact.out_path) if contact.out_path else []
        self.out_path_len = contact.out_path_len
        self.sync_since = contact.sync_since
        self.last_advert_timestamp = contact.last_advert_timestamp
        self.lastmod = contact.lastmod
        self.gps_lat = contact.gps_lat
        self.gps_lon = contact.gps_lon
        self.last_rssi = contact.last_rssi
        self.last_snr = contact.last_snr

    @property
    def public_key_bytes(self) -> bytes:
        """Raw key expected by the core packet builders and send helpers."""
        return bytes.fromhex(self.public_key)

    @property
    def dest_hash(self) -> int:
        return self.public_key_bytes[0]

    def _sync_from_contact(self) -> None:
        """Update proxy fields from the underlying Contact."""
        c = self._contact
        self.public_key = c.public_key.hex()
        self.name = c.name
        self.type = c.adv_type
        self.flags = c.flags
        self.out_path = list(c.out_path) if c.out_path else []
        self.out_path_len = c.out_path_len
        self.sync_since = c.sync_since
        self.last_advert_timestamp = c.last_advert_timestamp
        self.lastmod = c.lastmod
        self.gps_lat = c.gps_lat
        self.gps_lon = c.gps_lon
        self.last_rssi = c.last_rssi
        self.last_snr = c.last_snr


class ContactStore:
    """In-memory contact storage compatible with MeshNode's contacts interface.

    Provides both the interface expected by MeshNode/Dispatcher (contacts property,
    get_by_name, list_contacts) and companion radio CRUD operations (add, update,
    remove, get_by_key, etc.).

    The store can be populated from external sources using load_from() or
    load_from_dicts() for easy integration with databases and configuration files.
    """

    def __init__(self, max_contacts: int = DEFAULT_MAX_CONTACTS):
        self._contacts: dict[bytes, Contact] = {}  # keyed by public_key bytes
        self._proxies: dict[bytes, ContactProxy] = {}  # cached proxies
        self._max_contacts = max_contacts

    @property
    def max_contacts(self) -> int:
        """Maximum number of contacts (read-only). Used by companion protocol device info."""
        return self._max_contacts

    # ------------------------------------------------------------------
    # Interface expected by MeshNode/Dispatcher/Handlers
    # ------------------------------------------------------------------

    @property
    def contacts(self) -> list:
        """Return contacts as list of proxy objects with hex public_key attribute."""
        return list(self._proxies.values())

    def list_contacts(self) -> list:
        """Return contacts list (used by ProtocolResponseHandler)."""
        return self.contacts

    def get_by_name(self, name: str) -> Optional[ContactProxy]:
        """Lookup by name (required by MeshNode._get_contact_or_raise)."""
        for proxy in self._proxies.values():
            if proxy.name == name:
                return proxy
        return None

    def get_proxy_by_key(self, public_key: bytes) -> Optional[ContactProxy]:
        """Resolve the exact recipient, including contacts sharing a name."""
        return self._proxies.get(public_key)

    # ------------------------------------------------------------------
    # Companion radio CRUD operations
    # ------------------------------------------------------------------

    def _upsert_contact(
        self, contact: Contact, *, overwrite: bool = False, transient_only: bool = False,
    ) -> Tuple[bool, Optional[bytes]]:
        """Stage one replacement while keeping the real and anonymous pools separate."""
        candidate = replace(contact)
        key = candidate.public_key
        if self._contacts.get(key) is contact:
            # Getters intentionally expose mutable Contacts for path/sync
            # updates. Retain the requested type on the candidate, but restore
            # the published type before a capacity rejection can leave an
            # in-place promotion in the wrong pool.
            previous_proxy = self._proxies.get(key)
            if previous_proxy is not None:
                contact.adv_type = previous_proxy.type
        anonymous = candidate.adv_type == ADV_TYPE_NONE
        if transient_only and not anonymous:
            return False, None
        candidate_proxy = ContactProxy(candidate)
        others = [(other_key, entry) for other_key, entry in self._contacts.items()
                  if other_key != key and (entry.adv_type == ADV_TYPE_NONE) == anonymous]
        capacity = MAX_ANON_CONTACTS if anonymous else self._max_contacts
        victim_key = None
        if len(others) >= capacity:
            if not anonymous and not overwrite:
                return False, None
            eligible = others if anonymous else [item for item in others if not item[1].flags & 0x01]
            victim = min(eligible, key=lambda item: item[1].lastmod, default=None)
            if victim is None:
                return False, None
            victim_key = victim[0]
        # Construct the replacement and proxy before removing any victim.
        if victim_key is not None:
            self.remove(victim_key)
        self._contacts[key] = candidate
        self._proxies[key] = candidate_proxy
        # Anonymous-pool eviction is never a real-contact deletion notification.
        return True, None if anonymous else victim_key

    def add(self, contact: Contact) -> bool:
        """Add or refresh a contact without evicting any real contact."""
        return self._upsert_contact(contact)[0]

    def add_or_overwrite(self, contact: Contact) -> Tuple[bool, Optional[bytes]]:
        """Add a contact, overwriting the oldest non-favourite if store is full.

        Mirrors C++ BaseChatMesh::allocateContactSlot (BaseChatMesh.cpp:70-90).

        Returns:
            (success, overwritten_pubkey_or_None)
        """
        return self._upsert_contact(contact, overwrite=True)

    def add_transient(self, contact: Contact) -> bool:
        """Reserve a bounded temporary recipient for anonymous requests.

        Match core/firmware's eight-entry pool without evicting real contacts.
        """
        return self._upsert_contact(contact, transient_only=True)[0]

    def update(self, contact: Contact) -> bool:
        """Refresh or add a contact, checking capacity when its pool changes."""
        return self._upsert_contact(contact)[0]

    def remove(self, public_key: bytes) -> bool:
        """Remove a contact by public key. Returns False if not found."""
        if public_key not in self._contacts:
            return False
        del self._contacts[public_key]
        del self._proxies[public_key]
        return True

    def get_by_key(self, public_key: bytes) -> Optional[Contact]:
        """Lookup a contact by full 32-byte public key."""
        return self._contacts.get(public_key)

    def get_by_key_prefix(self, prefix: bytes) -> Optional[Contact]:
        """Lookup a contact by public key prefix (1-32 bytes)."""
        for key, contact in self._contacts.items():
            if key[: len(prefix)] == prefix:
                return contact
        return None

    def get_all(self, since: int = 0) -> list[Contact]:
        """Get all contacts, optionally filtered by lastmod >= since."""
        if since == 0:
            return list(self._contacts.values())
        return [c for c in self._contacts.values() if c.lastmod >= since]

    def get_count(self) -> int:
        """Return the number of stored contacts."""
        return len(self._contacts)

    def count(self) -> int:
        """Real-contact count advertised to the companion app."""
        return sum(entry.adv_type != ADV_TYPE_NONE for entry in self._contacts.values())

    def is_full(self) -> bool:
        """Check real-contact capacity, excluding reserved anonymous recipients."""
        return self.count() >= self._max_contacts

    def clear(self) -> None:
        """Remove all contacts."""
        self._contacts.clear()
        self._proxies.clear()

    # ------------------------------------------------------------------
    # Bulk loading from external sources
    # ------------------------------------------------------------------

    @staticmethod
    def _validated_contact(contact: Contact) -> Contact:
        """Validate a detached contact without changing its supplied identity/data."""
        if not isinstance(contact, Contact):
            raise ValueError("Bulk contact loads require Contact objects")

        def decode_bytes(value, field):
            if isinstance(value, str):
                try:
                    return bytes.fromhex(value)
                except ValueError as exc:
                    raise ValueError(f"Contact {field} must contain valid hex") from exc
            if isinstance(value, (bytes, bytearray, memoryview)):
                return bytes(value)
            if field == "out_path" and isinstance(value, list):
                if all(isinstance(item, int) and not isinstance(item, bool)
                       and 0 <= item <= 255 for item in value):
                    return bytes(value)
            raise ValueError(f"Contact {field} must contain bytes or hex")

        def integer(value, field, minimum, maximum):
            if (not isinstance(value, int) or isinstance(value, bool)
                    or not minimum <= value <= maximum):
                raise ValueError(f"Contact {field} must be an integer in {minimum}..{maximum}")
            return value

        def finite_number(value, field):
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError(f"Contact {field} must be a finite number")
            try:
                finite = math.isfinite(value)
            except OverflowError:
                finite = False
            if not finite:
                raise ValueError(f"Contact {field} must be a finite number")

        candidate = replace(contact)
        candidate.public_key = decode_bytes(contact.public_key, "public_key")
        if len(candidate.public_key) != 32:
            raise ValueError("Contact public_key must contain exactly 32 bytes")
        if not isinstance(contact.name, str):
            raise ValueError("Contact name must be a string")
        for field in ("adv_type", "flags"):
            integer(getattr(contact, field), field, 0, 255)
        for field in ("last_advert_timestamp", "lastmod", "sync_since"):
            integer(getattr(contact, field), field, 0, 0xFFFFFFFF)
        for field in ("gps_lat", "gps_lon"):
            value = getattr(contact, field)
            finite_number(value, field)
            # Match the signed microdegree fields emitted by contact frames,
            # without imposing additional geographic policy on stored data.
            if not -2148 <= value <= 2148 or not -(1 << 31) <= int(value * 1e6) < (1 << 31):
                raise ValueError(f"Contact {field} does not fit signed 32-bit microdegrees")
        for field in ("last_rssi", "last_snr"):
            value = getattr(contact, field)
            if value is not None:
                finite_number(value, field)

        encoded = integer(contact.out_path_len, "out_path_len", -1, 255)
        candidate.out_path_len = -1 if encoded in (-1, 255) else encoded
        if candidate.out_path_len >= 0 and not PathUtils.is_valid_path_len(encoded):
            raise ValueError("Contact out_path_len has an invalid path encoding")
        candidate.out_path = (b"" if contact.out_path is None
                              else decode_bytes(contact.out_path, "out_path"))
        required = PathUtils.get_path_byte_len(encoded) if candidate.out_path_len >= 0 else 0
        # Firmware persists a full 64-byte buffer, not just the encoded route.
        # Keep unused tails and zero hash bytes intact.
        if not required <= len(candidate.out_path) <= 64:
            raise ValueError("Contact out_path does not fit its encoded length/64-byte buffer")
        candidate.last_advert_packet = (
            None if contact.last_advert_packet is None
            else decode_bytes(contact.last_advert_packet, "last_advert_packet")
        )
        return candidate

    def prepare_load(
        self, contacts: Iterable[Contact], *, preserve_transient: bool = False,
        transient_contacts: Optional[Iterable[Contact]] = None,
    ) -> Callable[[], None]:
        """Validate a replacement and return its no-I/O publication callback.

        Callers can persist the prepared snapshot before publishing it into this
        same store object, which protocol handlers retain. They must serialize
        mutations between preparation and publication. Transient recipients can
        optionally survive replacement of the real, persisted contact list, or
        be supplied explicitly as a separate bounded, non-persisted pool.
        """
        replacement = {}
        proxies = {}
        for contact in contacts:
            candidate = self._validated_contact(contact)
            if ((preserve_transient or transient_contacts is not None)
                    and candidate.adv_type == ADV_TYPE_NONE):
                raise ValueError("Real contact replacements must not contain transient recipients")
            key = candidate.public_key
            if key in replacement:
                raise ValueError("Bulk contact load contains duplicate public keys")
            if len(replacement) >= self._max_contacts:
                raise ValueError(f"Bulk contact load exceeds max_contacts={self._max_contacts}")
            replacement[key] = candidate
            proxies[key] = ContactProxy(candidate)
        if transient_contacts is not None or preserve_transient:
            supplied_transients = transient_contacts is not None
            if transient_contacts is None:
                transient_contacts = (contact for contact in self._contacts.values()
                                      if contact.adv_type == ADV_TYPE_NONE)
            transient_count = 0
            for contact in transient_contacts:
                candidate = self._validated_contact(contact)
                key = candidate.public_key
                if candidate.adv_type != ADV_TYPE_NONE:
                    raise ValueError("Transient recipient pool must contain only anonymous contacts")
                if key in replacement:
                    if not supplied_transients and replacement[key].adv_type != ADV_TYPE_NONE:
                        continue  # An imported real contact promotes this transient key.
                    raise ValueError("Transient recipient pool contains a duplicate public key")
                if transient_count >= MAX_ANON_CONTACTS:
                    raise ValueError("Transient recipient pool exceeds its reserved capacity")
                replacement[key] = candidate
                proxies[key] = ContactProxy(candidate)
                transient_count += 1

        def publish():
            self._contacts, self._proxies = replacement, proxies

        return publish

    def load_from(self, contacts: Iterable[Contact]) -> None:
        """Replace contacts only after all rows, keys and capacity validate."""
        self.prepare_load(contacts)()

    def load_from_dicts(self, records: Iterable[dict]) -> None:
        """Bulk-load contacts from dicts.

        Each dict must have 'public_key' (hex string or bytes).
        Optional keys: 'name', 'adv_type', 'flags', 'out_path', 'out_path_len',
        'last_advert_timestamp', 'lastmod', 'gps_lat', 'gps_lon', 'sync_since',
        'last_advert_packet' (hex string of raw ADVERT wire bytes for CMD_SHARE_CONTACT).

        Replaces all existing contacts only after every row validates.
        """
        field_names = tuple(field.name for field in fields(Contact))

        def decoded_contacts():
            for rec in records:
                if not isinstance(rec, dict) or "public_key" not in rec:
                    raise ValueError("Contact records must be dictionaries containing public_key")
                # The dataclass defaults are lossless; Contact.from_dict instead
                # pads/truncates keys, clamps times and coerces invalid fields.
                yield Contact(**{field: rec[field] for field in field_names if field in rec})

        self.load_from(decoded_contacts())

    def to_dicts(self) -> list[dict]:
        """Export all contacts as a list of plain dicts for serialization."""
        result = []
        for c in self._contacts.values():
            if c.adv_type == ADV_TYPE_NONE:
                continue  # Anonymous-request recipients are never persisted.
            result.append(
                {
                    "public_key": c.public_key.hex(),
                    "name": c.name,
                    "adv_type": c.adv_type,
                    "flags": c.flags,
                    "out_path_len": c.out_path_len,
                    "out_path": c.out_path.hex() if c.out_path else "",
                    "last_advert_timestamp": c.last_advert_timestamp,
                    "lastmod": c.lastmod,
                    "gps_lat": c.gps_lat,
                    "gps_lon": c.gps_lon,
                    "sync_since": c.sync_since,
                    "last_advert_packet": c.last_advert_packet.hex()
                    if c.last_advert_packet
                    else "",
                    "last_rssi": c.last_rssi,
                    "last_snr": c.last_snr,
                }
            )
        return result

    # ------------------------------------------------------------------
    # Iterator (matches firmware's iterator pattern)
    # ------------------------------------------------------------------

    def iterate(self, since: int = 0) -> Iterator[Contact]:
        """Iterate over contacts, optionally filtered by lastmod >= since."""
        for contact in self._contacts.values():
            if since == 0 or contact.lastmod >= since:
                yield contact
