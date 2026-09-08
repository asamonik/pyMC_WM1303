"""Detached contact decoding for already-verified companion advert events."""

import time
from collections.abc import Mapping

from openhop_core.companion.contact_store import ContactStore
from openhop_core.companion.models import Contact
from openhop_core.protocol import Packet
from openhop_core.protocol.constants import ADVERT_FLAG_HAS_LOCATION, PAYLOAD_TYPE_ADVERT
from openhop_core.protocol.packet_utils import PathUtils


def _event_value(data, *names, default=None):
    """Use the existing event/Contact aliases without losing explicit zeroes."""
    for name in names:
        if name in data:
            return data[name]
    return default


def _event_bytes(value, field):
    if isinstance(value, str):
        try:
            return bytes.fromhex(value)
        except ValueError as exc:
            raise ValueError(f"Advert {field} must contain valid hex") from exc
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value)
    raise ValueError(f"Advert {field} must contain bytes or hex")


def contact_from_advert_event(data) -> tuple[Contact, bool | None, bytes, int]:
    """Decode a verified event without mutating contacts, paths or persistence.

    This is not a signature-verification entry point. The retained packet only
    restores the remote timestamp, type nibble and location-presence flag lost
    by the upstream event projection. Its identity must match the event identity.
    Without a packet, legacy producers may supply a boolean ``has_location``;
    otherwise location presence stays unknown, even when coordinates are zero.

    The returned route describes reception, not the contact's outbound route.
    Explicit encoded lengths preserve multi-byte hashes and zero-hop encodings;
    unused path-buffer tails are accepted but omitted from the returned route.
    """
    if not isinstance(data, Mapping):
        raise ValueError("Advert event must be a mapping")

    public_key = _event_bytes(data.get("public_key"), "public_key")
    raw_blob = data.get("raw_advert_packet")
    packet = None
    remote_time = _event_value(data, "advert_timestamp", "last_advert_timestamp", default=0)
    if remote_time is None:
        remote_time = 0
    adv_type = _event_value(data, "contact_type_id", "adv_type", "contact_type", default=0)
    has_location = data.get("has_location")
    if raw_blob is not None:
        raw_blob = _event_bytes(raw_blob, "raw_advert_packet")
        if len(raw_blob) < 2:
            raise ValueError("Advert packet is truncated")
        packet = Packet()
        if not packet.read_from(raw_blob) or packet.get_payload_type() != PAYLOAD_TYPE_ADVERT:
            raise ValueError("Advert event packet must be an ADVERT")
        payload = packet.get_payload()
        if len(payload) < 101 or payload[:32] != public_key:
            raise ValueError("Advert packet payload is truncated or has a different identity")
        remote_time = int.from_bytes(payload[32:36], "little")
        # AdvertDataParser::getType() preserves all four wire type bits. The
        # upstream event maps unknown nonzero types to anonymous type zero.
        adv_type = payload[100] & 0x0F
        has_location = bool(payload[100] & ADVERT_FLAG_HAS_LOCATION)
        if has_location and len(payload) < 109:
            raise ValueError("Advert packet location is truncated")
    elif has_location is not None and type(has_location) is not bool:
        raise ValueError("Advert has_location must be a boolean or None")

    lat = _event_value(data, "lat", "latitude", "gps_lat")
    lon = _event_value(data, "lon", "longitude", "gps_lon")
    if has_location is True and (lat is None or lon is None):
        raise ValueError("Advert with a location must supply both coordinates")
    # Missing legacy coordinates have the Contact default, but do not imply
    # that an existing contact's known location should be cleared.
    if not any(name in data for name in ("lat", "latitude", "gps_lat")):
        lat = 0.0
    if not any(name in data for name in ("lon", "longitude", "gps_lon")):
        lon = 0.0
    local_time = _event_value(data, "timestamp", "lastmod")
    if local_time is None:
        local_time = int(time.time())
    candidate = Contact(
        public_key=public_key,
        name=data.get("name", ""),
        adv_type=adv_type,
        # Advert flags are not local favourites. Outbound route and sync state
        # likewise start unknown; admission/update policy owns any preservation.
        last_advert_timestamp=remote_time,
        lastmod=local_time,
        gps_lat=lat,
        gps_lon=lon,
        last_advert_packet=raw_blob,
        last_rssi=_event_value(data, "rssi", "last_rssi"),
        last_snr=_event_value(data, "snr", "last_snr"),
    )
    staged = ContactStore(max_contacts=1)
    staged.load_from([candidate])
    contact = staged.get_all()[0]
    if not contact.name:
        raise ValueError("Advert event must contain a name")

    inbound = data.get("inbound_path")
    if inbound is None:
        inbound = bytes(packet.path) if packet is not None else b""
    elif isinstance(inbound, list):
        if not all(isinstance(value, int) and not isinstance(value, bool)
                   and 0 <= value <= 255 for value in inbound):
            raise ValueError("Advert inbound_path must contain byte values")
        inbound = bytes(inbound)
    else:
        inbound = _event_bytes(inbound, "inbound_path")
    encoded = data.get("path_len_encoded")
    if encoded is None:
        encoded = (packet.path_len if packet is not None
                   else PathUtils.encode_path_len(1, len(inbound)))
    if (not isinstance(encoded, int) or isinstance(encoded, bool)
            or not 0 <= encoded <= 255 or not PathUtils.is_valid_path_len(encoded)):
        raise ValueError("Advert path_len_encoded has an invalid path encoding")
    required = PathUtils.get_path_byte_len(encoded)
    if not required <= len(inbound) <= 64:
        raise ValueError("Advert inbound_path does not fit its encoded length/64-byte buffer")
    return contact, has_location, inbound[:required], encoded
