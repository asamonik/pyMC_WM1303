"""Wire-level helpers shared by the radio backend and packet bridge.

Packet identity follows meshcore-dev/MeshCore src/Packet.cpp, including
TRACE's path-length exception so the return trip may revisit a repeater.
"""
from __future__ import annotations

import hashlib


def packet_hash(data: bytes, length: int = 12) -> str:
    """Hash payload type and payload independently of mutable route bytes.

    Current firmware hashes TRACE's uint16 path_len in little-endian order
    before its payload. Malformed input uses its complete bytes as identity;
    validation and forwarding policy remain the caller's responsibility.
    """
    if len(data) < 2:
        return hashlib.sha256(data).hexdigest()[:length]
    path_offset = 5 if data[0] & 3 in (0, 3) else 1
    if path_offset >= len(data):
        return hashlib.sha256(data).hexdigest()[:length]
    path_len = data[path_offset]
    path_size = ((path_len >> 6) + 1) * (path_len & 63)
    payload_offset = path_offset + 1 + path_size
    if path_len >> 6 == 3 or path_size > 64 or payload_offset > len(data):
        return hashlib.sha256(data).hexdigest()[:length]
    payload_type = (data[0] >> 2) & 15
    prefix = bytes([payload_type])
    if payload_type == 9:
        prefix += path_len.to_bytes(2, "little")
    return hashlib.sha256(prefix + data[payload_offset:]).hexdigest()[:length]
