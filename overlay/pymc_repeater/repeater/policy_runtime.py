"""One received-packet policy decision across local and forwarding consumers."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import logging
import math


logger = logging.getLogger("PacketPolicy")
_receipt = ContextVar("received_packet_policy", default=None)


@dataclass(frozen=True)
class PolicyReceipt:
    service_id: int
    engine: object
    raw_packet: bytes
    decision: object
    ingress: str | None

    @property
    def engine_id(self):
        # Hold the evaluated engine for the receipt's lifetime so another live
        # update cannot recycle its id and accidentally reuse a stale decision.
        return id(self.engine)

    @property
    def drop_reason(self):
        return "Policy blocked packet: " + str(self.decision.reason or "drop action")


def _measured(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError, OverflowError):
        return None


def evaluate_received_policy(service, packet, metadata=None, config=None):
    """Reuse a decision only inside this receive chain and for identical bytes.

    A task-local receipt preserves the original path and RF context as the
    router and repeater clone or parse a received packet. Locally generated
    replies and another packet cannot inherit its allow/drop result.
    """
    raw = bytes(packet.write_to())
    metadata = metadata or {}
    packet_context = getattr(packet, "_tx_metadata", None) or {}
    channel = metadata.get("channel") or metadata.get("origin_channel") or packet_context.get("policy_origin_channel")
    current = _receipt.get()
    engine_id = id(service.engine)
    if (current is not None and current.service_id == id(service)
            and current.engine_id == engine_id and current.raw_packet == raw
            and (channel is None or current.ingress == channel)):
        return current
    config = config or getattr(service, "config", {})
    header = getattr(packet, "header", 0)
    context = {
        "mode": config.get("repeater", {}).get("mode", "forward"),
        "local_transmission": False,
        "route_type": header & 3,
        "payload_type": (header >> 2) & 15,
        "payload_length": len(getattr(packet, "payload", None) or b""),
        "path_hash_size": packet.get_path_hash_size(),
        "hop_count": packet.get_path_hash_count(),
        "rssi": _measured(metadata.get("rssi", getattr(packet, "rssi", None))),
        "snr": _measured(metadata.get("snr", getattr(packet, "snr", None))),
        "ingress": channel or "radio",
        "channel": channel,
    }
    decision, engine = service.evaluate_with_engine(packet, context)
    if decision.matched or decision.action in ("drop", "log_only"):
        logger.info("Packet policy: %s", decision.reason or decision.action)
    return PolicyReceipt(id(service), engine, raw, decision, channel)


@contextmanager
def received_policy_scope(receipt):
    token = _receipt.set(receipt)
    try:
        yield
    finally:
        _receipt.reset(token)
