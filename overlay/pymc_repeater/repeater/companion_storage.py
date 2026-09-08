"""Companion storage ownership, distinct from the one-byte mesh routing hash."""

from collections.abc import Mapping


def companion_limits_from_settings(settings) -> dict:
    """Return validated optional limits without applying defaults or coercing floats."""
    if not isinstance(settings, Mapping):
        raise ValueError("Companion settings must be a mapping")
    limits = {}
    for field, minimum, maximum in (
        ("max_contacts", 1, 0xffffffff),
        ("offline_queue_size", 0, None),
    ):
        if field not in settings:
            continue
        error = f"settings.{field} must be an integer >= {minimum}"
        if maximum is not None:
            error += f" and <= {maximum}"
        value = settings[field]
        if isinstance(value, str):
            value = value.strip()
            if not value.isascii() or not value.isdecimal():
                raise ValueError(error)
            try:
                value = int(value, 10)
            except ValueError:
                raise ValueError(error) from None
        if (isinstance(value, bool) or not isinstance(value, int)
                or value < minimum or (maximum is not None and value > maximum)):
            raise ValueError(error)
        limits[field] = value
    return limits


def validated_channel_rows(rows, *, max_channels=256) -> list:
    """Validate a complete channel snapshot, returning fresh normalized rows.

    Preserve channel names verbatim and legacy 16-byte secrets by zero-extending
    them to 32 bytes. Never truncate secrets or silently drop duplicate slots.
    """
    if (isinstance(max_channels, bool) or not isinstance(max_channels, int)
            or not 1 <= max_channels <= 256):
        raise ValueError("Companion channel capacity must be an integer between 1 and 256")
    if not isinstance(rows, (list, tuple)):
        raise ValueError("Companion channels must be a list of mappings")
    result = []
    indices = set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("Companion channel rows must be mappings")
        index = row.get("channel_idx")
        if (isinstance(index, bool) or not isinstance(index, int)
                or not 0 <= index < max_channels or index in indices):
            raise ValueError("Companion channel index must be unique and within capacity")
        name = row.get("name")
        if not isinstance(name, str):
            raise ValueError("Companion channel name must be a string")
        secret = row.get("secret")
        try:
            if isinstance(secret, str):
                # Preserve the previous loader's valid whitespace-separated hex.
                secret = bytes.fromhex(secret)
            elif isinstance(secret, (bytes, bytearray, memoryview)):
                secret = bytes(secret)
            else:
                raise ValueError
        except (ValueError, TypeError):
            raise ValueError("Companion channel secret must be bytes or valid hexadecimal") from None
        if len(secret) not in (16, 32):
            raise ValueError("Companion channel secret must contain 16 or 32 bytes")
        indices.add(index)
        result.append({"channel_idx": index, "name": name, "secret": secret.ljust(32, b"\x00")})
    return result


def storage_key_for_public_key(public_key: bytes) -> str:
    """Return an unambiguous owner key without changing legacy hash buckets."""
    if not isinstance(public_key, (bytes, bytearray, memoryview)):
        raise ValueError("Companion storage ownership requires a 32-byte public key")
    key_bytes = bytes(public_key)
    if len(key_bytes) != 32:
        raise ValueError("Companion storage ownership requires a 32-byte public key")
    return "pubkey:" + key_bytes.hex()


def legacy_owner_from_settings(settings: dict):
    """Parse explicit operator confirmation; never derive it from a name/hash."""
    value = settings.get("legacy_storage_owner")
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = value.strip()
            if value.lower().startswith("0x"):
                value = value[2:]
            owner = bytes.fromhex(value)
            if len(owner) == 32:
                return owner
        except ValueError:
            pass
    raise ValueError("settings.legacy_storage_owner must be a full 32-byte public key in hex")
