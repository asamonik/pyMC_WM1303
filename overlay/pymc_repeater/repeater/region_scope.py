"""Resolve the configured advert scope using the repeater's region keys."""

import base64


def resolve_default_region(value, storage):
    """Return an existing, allowed region, or None for unscoped adverts."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("default_region must be a region name or null")
    name = value.strip()
    if name in ("", "<null>"):
        return None
    needle = name.removeprefix("#").lower()
    for record in storage.get_transport_keys():
        if str(record.get("name", "")).removeprefix("#").lower() != needle:
            continue
        if record.get("flood_policy") != "allow":
            raise ValueError("Enable flooding for the selected default region first")
        key = base64.b64decode(record.get("transport_key", ""), validate=True)
        if len(key) != 16:
            raise ValueError("Default region must have a 16-byte transport key")
        return record
    raise ValueError("Add the default region to the region list first")


def apply_default_advert_scope(packet, config, storage):
    """Scope an advert; invalid settings fail instead of broadcasting unscoped."""
    record = resolve_default_region(config.get("mesh", {}).get("default_region"), storage)
    if record is None:
        return None
    from openhop_core.protocol.constants import ROUTE_TYPE_TRANSPORT_FLOOD
    from openhop_core.protocol.transport_keys import calc_transport_code

    key = base64.b64decode(record["transport_key"], validate=True)
    packet.transport_codes = [calc_transport_code(key, packet), 0]
    packet.header = (packet.header & ~0x03) | ROUTE_TYPE_TRANSPORT_FLOOD
    return record["name"]
