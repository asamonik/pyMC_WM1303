"""Firmware ACK wire vectors against the overlay, without upstream packages."""

import asyncio
from collections import OrderedDict, deque
import hashlib
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from test_engine_protocol import PacketFixture, load_overlay


engine = load_overlay("engine")


class AckPacket(PacketFixture):
    def __init__(self, wire=None):
        super().__init__()
        self.transport_codes = [0, 0]
        if wire:
            raw = bytes.fromhex(wire)
            self.header = raw[0]
            offset = 5 if (self.header & 3) in (0, 3) else 1
            if offset == 5:
                self.transport_codes = [int.from_bytes(raw[1:3], "little"), int.from_bytes(raw[3:5], "little")]
            self.path_len = raw[offset]
            size = ((self.path_len >> 6) + 1) * (self.path_len & 63)
            self.path = bytearray(raw[offset + 1 : offset + 1 + size])
            self.payload = bytearray(raw[offset + 1 + size :])
        self.payload_len = len(self.payload)

    def write_to(self):
        transport = b""
        if (self.header & 3) in (0, 3):
            transport = b"".join(value.to_bytes(2, "little") for value in self.transport_codes)
        return bytes([self.header]) + transport + bytes([self.path_len]) + bytes(self.path) + bytes(self.payload)

    def calculate_packet_hash(self):
        return hashlib.sha256(bytes([self.get_payload_type()]) + bytes(self.payload)).digest()

    def get_path_hashes_hex(self):
        width = self.get_path_hash_size()
        return [bytes(self.path[i : i + width]).hex() for i in range(0, len(self.path), width)]


def make_handler():
    handler = engine.RepeaterHandler.__new__(engine.RepeaterHandler)
    handler.config = {}
    handler.local_hash = 0xAB
    handler.local_hash_bytes = bytes.fromhex("abcdef")
    handler.loop_detect_mode = "off"
    handler.seen_packets = OrderedDict()
    handler.cache_ttl = 60
    handler.max_cache_size = 1000
    handler.max_flood_hops = 63
    handler.multi_acks = 0
    handler._calculate_tx_delay = MagicMock(return_value=0.0)
    return handler


class AckWireTests(unittest.TestCase):
    def test_direct_and_multipart_firmware_vectors(self):
        vectors = (
            ("0e02abcc4dabaf95", "0e01cc4dabaf95", 0.0),
            ("0f3412785602abcc4dabaf95", "0e01cc4dabaf95", 0.0),
            ("0e42abcd11224dabaf95", "0e4111224dabaf95", 0.0),
            ("2a02abcc234dabaf95", "0e01cc4dabaf95", 0.9),
            ("2b3412785642abcd1122134dabaf95", "0e4111224dabaf95", 0.6),
            ("2a82abcdef112233034dabaf95", "0e811122334dabaf95", 0.3),
        )
        for received, expected, delay in vectors:
            with self.subTest(received=received):
                result = make_handler().process_packet(AckPacket(received))
                self.assertIsNotNone(result)
                self.assertEqual(result[0].write_to(), bytes.fromhex(expected))
                self.assertAlmostEqual(result[1], delay)

    def test_multipart_remaining_count_deduplicates_without_swallowing_plain_ack(self):
        handler = make_handler()
        self.assertIsNotNone(handler.process_packet(AckPacket("2a02abcc234dabaf95")))
        self.assertIsNone(handler.process_packet(AckPacket("2a02abcc134dabaf95")))
        self.assertIsNotNone(handler.process_packet(AckPacket("0e02abcc4dabaf95")))

    def test_non_ack_multipart_and_nonlocal_ack_hops_are_not_regenerated(self):
        for wire in ("2a02abcc224dabaf95", "2a02abcc134dabaf", "0e02aacc4dabaf95", "0e004dabaf95"):
            with self.subTest(wire=wire):
                self.assertIsNone(make_handler().process_packet(AckPacket(wire)))
        # Ordinary DIRECT data is also repeated only by its named next hop.
        ordinary = "0a02aacc4dabaf95"
        packet = AckPacket(ordinary)
        self.assertIsNone(make_handler().process_packet(packet))
        self.assertEqual(packet.write_to(), bytes.fromhex(ordinary))

    def test_redundancy_is_bounded_and_shares_the_primary_deadline(self):
        handler = make_handler()
        handler.multi_acks = 100
        handler._calculate_tx_delay.return_value = 0.25
        with patch.object(engine, "Packet", AckPacket):
            result = handler.process_packet(AckPacket("0e02abcc4dabaf95"))
        self.assertEqual(len(result.extras), 1)
        self.assertEqual(result.extras[0][0].write_to(), bytes.fromhex("2a01cc134dabaf95"))
        self.assertAlmostEqual(result[1], 0.55)
        self.assertEqual(result.extras[0][1], result[1])
        for value, expected in ((5, 1), (-3, 0), ("bad", 0), (None, 0), (float("inf"), 0)):
            self.assertEqual(handler._normalize_multi_acks({"repeater": {"multi_acks": value}}), expected)


class AckTransmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_redundancy_does_not_hide_successful_plain_ack(self):
        handler = make_handler()
        handler.multi_acks = 1
        handler._tx_lock = asyncio.Lock()
        handler.dispatcher = SimpleNamespace(send_packet=AsyncMock(side_effect=[False, True]))
        handler.airtime_mgr = SimpleNamespace(calculate_airtime=MagicMock(return_value=20),
            can_transmit=MagicMock(return_value=(True, 0)), record_tx=MagicMock(), record_rx=MagicMock())
        handler.rx_count = handler.forwarded_count = handler.dropped_count = 0
        handler.recv_direct_count = handler.sent_direct_count = handler.direct_dup_count = 0
        handler.recent_packets = deque()
        handler.storage = None
        handler._build_packet_record = MagicMock(return_value={"packet_hash": "hash"})
        handler._append_recent_packet = MagicMock()
        headers = SimpleNamespace(parse_header=lambda value: {"payload_type": (value >> 2) & 15, "route_type": value & 3})
        with (patch.object(engine, "Packet", AckPacket), patch.object(engine, "PacketHeaderUtils", headers),
              patch.object(engine.asyncio, "sleep", new_callable=AsyncMock), self.assertLogs("RepeaterHandler", level="WARNING")):
            sent = await handler(AckPacket("0e02abcc4dabaf95"))
        self.assertTrue(sent)
        frames = [call.args[0].write_to().hex() for call in handler.dispatcher.send_packet.call_args_list]
        self.assertEqual(frames, ["2a01cc134dabaf95", "0e01cc4dabaf95"])
        self.assertEqual(handler.forwarded_count, 1)
        self.assertEqual(handler.dropped_count, 1)
        handler.airtime_mgr.record_tx.assert_called_once_with(20)


if __name__ == "__main__":
    unittest.main()
