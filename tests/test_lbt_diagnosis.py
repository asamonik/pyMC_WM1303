"""RF Health Correlation uses stored TX metadata, including empty windows."""

import ast
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import cherrypy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "overlay/pymc_repeater"))

from repeater.data_acquisition.sqlite_handler import SQLiteHandler


def load_api():
    """Load the real endpoint without optional upstream radio dependencies."""
    source = ROOT / "overlay/pymc_repeater/repeater/web/api_endpoints.py"
    tree = ast.parse(source.read_text())
    cls = next(node for node in tree.body
               if isinstance(node, ast.ClassDef) and node.name == "APIEndpoints")
    cls.body = [node for node in cls.body if isinstance(node, ast.FunctionDef)
                and node.name in ("_success", "_error", "_get_time_range", "lbt_diagnostics")]
    endpoint = next(node for node in cls.body if node.name == "lbt_diagnostics")
    if not any(isinstance(node, ast.Attribute) and node.attr == "expose"
               for node in endpoint.decorator_list):
        raise AssertionError("LBT endpoint must be exposed to CherryPy")
    for method in cls.body:
        method.decorator_list = []
    namespace = dict(cherrypy=cherrypy, logger=Mock(), time=time)
    exec(compile(ast.Module(body=[cls], type_ignores=[]), str(source), "exec"), namespace)
    api = namespace["APIEndpoints"]()
    api._set_cors_headers = Mock()
    api.config = {}
    api.daemon_instance = None
    return api


class LBTDiagnosisTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        with patch.object(SQLiteHandler, "_start_wal_checkpoint_thread"):
            self.db = SQLiteHandler(Path(self.directory.name))
        self.addCleanup(self.db.close_thread_connection)
        self.api = load_api()
        self.api._get_storage = lambda: self.db
        self.request_patch = patch.object(cherrypy, "request", SimpleNamespace(method="GET"))
        self.request_patch.start()
        self.addCleanup(self.request_patch.stop)
        self.clock_patch = patch.object(time, "time", return_value=2_000_000)
        self.clock_patch.start()
        self.addCleanup(self.clock_patch.stop)

    def test_stored_attempts_populate_summary_chart_and_packet_type_heatmap(self):
        records = [
            dict(timestamp=1_997_400, type=5, transmitted=True, lbt_attempts=0),
            dict(timestamp=1_997_410, type=5, transmitted=True, lbt_attempts=1),
            dict(timestamp=1_997_420, type=4, transmitted=False, lbt_attempts=3,
                 lbt_channel_busy=True, drop_reason="TX failed (CAD)"),
            dict(timestamp=1_998_310, type=4, transmitted=True, lbt_attempts=2),
            dict(timestamp=1_997_430, type=4, transmitted=False, drop_reason="no_rule_match"),
            dict(timestamp=1_996_399, type=5, transmitted=True, lbt_attempts=9),
            dict(timestamp=2_000_001, type=5, transmitted=True, lbt_attempts=9),
        ]
        for record in records:
            self.assertIsNotNone(self.db.store_packet(record))
        response = self.api.lbt_diagnostics(hours="1")
        self.assertTrue(response["success"], response)
        data = response["data"]
        self.assertEqual((data["start_time"], data["end_time"]), (1_996_400, 2_000_000))
        summary = data["summary"]
        self.assertTrue(summary["has_lbt_data"])
        self.assertEqual(summary["total_transmissions"], 4)
        self.assertEqual(summary["total_attempts"], 10)
        self.assertEqual(summary["retry_rate_pct"], 75)
        self.assertEqual(summary["first_attempt_success_rate_pct"], 25)
        self.assertEqual(summary["avg_attempts"], 2.5)
        self.assertEqual(summary["attempts_3_plus_pct"], 50)
        self.assertEqual(summary["max_attempts"], 4)
        self.assertEqual(summary["failed_transmissions"], 1)
        self.assertEqual(summary["busy_channel_events"], 1)
        self.assertEqual(summary["severe_contention_count"], 1)
        self.assertEqual(sum(bucket["transmissions"] for bucket in data["buckets"]), 4)
        packet_types = {entry["packet_type"]: entry for entry in data["packet_types"]}
        self.assertEqual(packet_types[5]["retry_rate_pct"], 50)
        self.assertEqual(packet_types[4]["retry_rate_pct"], 100)
        self.assertEqual(sum(entry["total_attempts"] for entry in data["packet_type_buckets"]), 10)
        self.assertEqual(summary["worst_bucket"]["retry_rate_pct"], 100)

    def test_empty_window_is_success_with_unknown_rates(self):
        response = self.api.lbt_diagnostics(hours="1")
        self.assertTrue(response["success"], response)
        summary = response["data"]["summary"]
        self.assertFalse(summary["has_lbt_data"])
        for field in ("retry_rate_pct", "first_attempt_success_rate_pct", "avg_attempts",
                      "attempts_3_plus_pct", "median_attempts", "p95_attempts"):
            self.assertIsNone(summary[field])
        self.assertEqual(response["data"]["packet_types"], [])
        self.assertEqual(response["data"]["packet_type_buckets"], [])

    def test_valid_parameters_reach_storage_as_numbers(self):
        storage = Mock()
        storage.get_lbt_diagnostics.return_value = {"summary": {"has_lbt_data": False}}
        self.api._get_storage = lambda: storage
        response = self.api.lbt_diagnostics(hours="168", bucket_seconds="900",
                                            severe_attempt_threshold="5")
        self.assertTrue(response["success"])
        storage.get_lbt_diagnostics.assert_called_once_with(
            start_timestamp=1_395_200, end_timestamp=2_000_000,
            bucket_seconds=900, severe_attempt_threshold=5)

    def test_wm1303_missing_retry_metadata_cannot_look_like_first_attempt_success(self):
        self.db.store_packet(dict(timestamp=1_997_400, transmitted=True))
        self.db.store_packet(dict(timestamp=1_997_410, transmitted=True))
        # The existing native aggregate interprets the schema's default zero
        # as a measured first attempt, even for these bridge-style records.
        self.assertEqual(self.db.get_lbt_diagnostics(1_996_400, 2_000_000)
                         ["summary"]["first_attempt_success_rate_pct"], 100)
        for config, daemon in (({"radio_type": "wm1303"}, None),
                               ({}, SimpleNamespace(bridge_engine=object()))):
            with self.subTest(config=config):
                self.api.config = config
                self.api.daemon_instance = daemon
                result = self.api.lbt_diagnostics(hours="1")
                self.assertTrue(result["success"])
                data = result["data"]
                self.assertFalse(data["supported"])
                self.assertFalse(data["summary"]["has_lbt_data"])
                self.assertIsNone(data["summary"]["first_attempt_success_rate_pct"])
                self.assertIsNone(data["summary"]["retry_rate_pct"])
                self.assertIsNone(data["summary"]["max_attempts"])
                self.assertEqual(data["buckets"], [])
                self.assertEqual(data["packet_types"], [])
                self.assertEqual(data["packet_type_buckets"], [])
                self.assertIn("do not persist", data["source_limitations"][0])
                self.assertIsNone(data["correlations"]["retry_rate_vs_avg_snr"]["coefficient"])

    def test_invalid_parameters_do_not_query_storage(self):
        storage_getter = self.api._get_storage = Mock()
        for params in (dict(hours="oops"), dict(hours="0"), dict(hours="169"),
                       dict(hours=None), dict(hours=float("inf")),
                       dict(bucket_seconds="0"), dict(bucket_seconds="3601"),
                       dict(severe_attempt_threshold="1"), dict(severe_attempt_threshold="65")):
            with self.subTest(params=params):
                result = self.api.lbt_diagnostics(**params)
                self.assertFalse(result["success"])
                self.assertIn("Invalid parameter format", result["error"])
        storage_getter.assert_not_called()

    def test_storage_failure_is_json_error(self):
        self.api._get_storage = Mock(side_effect=RuntimeError("Storage not initialized"))
        result = self.api.lbt_diagnostics()
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "Storage not initialized")

    def test_cors_preflight_does_not_access_storage(self):
        cherrypy.request.method = "OPTIONS"
        self.api._get_storage = Mock()
        self.assertEqual(self.api.lbt_diagnostics(), "")
        self.api._set_cors_headers.assert_called_once_with()
        self.api._get_storage.assert_not_called()


if __name__ == "__main__":
    unittest.main()
