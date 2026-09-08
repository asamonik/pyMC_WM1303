"""Wire-format regressions checked against MeshCore 0679dbeffc50.

Reference: src/Packet.cpp, src/MeshCore.h, and src/Mesh.cpp in
https://github.com/meshcore-dev/MeshCore/tree/0679dbeffc504d562d2f09eb072fdc223f8ffc2a
Run without hardware or installed upstream packages: python -m unittest discover -s tests
"""

import importlib.util
from pathlib import Path
import sys
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "overlay/pymc_repeater/repeater/protocol_validator.py"
SPEC = importlib.util.spec_from_file_location("wm1303_protocol_validator_test", SOURCE)
validator = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = validator
SPEC.loader.exec_module(validator)


def frame(payload_type=0x0F, route=1, path=b"", hash_size=1, payload=b"test"):
    prefix = bytes([(payload_type << 2) | route])
    if route in (0, 3):
        prefix += b"\x11\x22\x33\x44"
    return prefix + bytes([((hash_size - 1) << 6) | (len(path) // hash_size)]) + path + payload


class ProtocolValidatorTests(unittest.TestCase):
    def test_every_upstream_path_encoding_and_route(self):
        for route in range(4):
            for encoded in range(256):
                with self.subTest(route=route, encoded=encoded):
                    width = (encoded >> 6) + 1
                    count = encoded & 63
                    raw = bytes([(0x0F << 2) | route])
                    if route in (0, 3):
                        raw += b"\x11\x22\x33\x44"
                    raw += bytes([encoded]) + bytes(width * count) + b"x"
                    self.assertEqual(
                        validator.validate(raw).is_valid,
                        width < 4 and width * count <= 64,
                    )

    def test_maximum_payload_is_independent_of_path_length(self):
        for route in range(4):
            for path in (b"", bytes(64)):
                with self.subTest(route=route, path_bytes=len(path)):
                    self.assertTrue(validator.validate(frame(route=route, path=path, hash_size=2, payload=bytes(184))))
                    rejected = validator.validate(frame(route=route, path=path, hash_size=2, payload=bytes(185)))
                    self.assertFalse(rejected)
                    self.assertEqual(rejected.reason, "payload_exceeds_max")

    def test_minimum_payload_layouts(self):
        # Fixed protocol prefixes, including their required first data byte.
        expected = {0: 5, 1: 5, 2: 5, 3: 4, 4: 100, 5: 4, 6: 4, 7: 36, 8: 5, 9: 9, 10: 1, 11: 1}
        for payload_type, minimum in expected.items():
            with self.subTest(payload_type=payload_type):
                self.assertTrue(validator.validate(frame(payload_type=payload_type, payload=bytes(minimum))))
                rejected = validator.validate(frame(payload_type=payload_type, payload=bytes(minimum - 1)))
                self.assertEqual(rejected.reason, "payload_too_short_for_type")
        self.assertTrue(validator.validate(frame(payload_type=15, payload=b"")))

    def test_transport_metadata_uses_transport_path_offset(self):
        result = validator.validate(frame(route=0, hash_size=2, path=b"\x10\x20\x30\x40", payload=b"content"))
        self.assertTrue(result)
        self.assertEqual(result.metadata["transport_codes_hex"], "11223344")
        self.assertEqual(result.metadata["path_hex"], "10203040")
        self.assertEqual(result.metadata["hash_size"], 2)
        self.assertEqual(result.metadata["hop_count"], 2)
        self.assertEqual(result.metadata["payload_length"], 7)

    def test_partial_frames_produce_structured_results(self):
        for raw in (b"", b"\x3d", b"\x3c\x01", b"\x3c\x01\x02\x03\x04", b"\x3d\x01"):
            with self.subTest(raw=raw):
                result = validator.validate(raw)
                self.assertFalse(result)
                self.assertEqual(result.metadata["packet_length"], len(raw))

    def test_storage_failure_does_not_replace_validation_result(self):
        def unavailable_store(record):
            raise RuntimeError("storage unavailable")

        validator.set_invalid_packet_store(unavailable_store)
        try:
            with self.assertLogs(validator.__name__, level="DEBUG"):
                result = validator.validate_and_record(b"", channel="A")
            self.assertEqual(result.reason, "too_short")
        finally:
            validator.set_invalid_packet_store(None)


if __name__ == "__main__":
    unittest.main()
