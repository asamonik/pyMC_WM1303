"""RF Health Correlation uses stored TX metadata, including empty windows."""

import ast
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import threading
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
                self.assertTrue(data["supported"])
                self.assertEqual(data["data_source"], "wm1303_tx_diagnostics")
                self.assertFalse(data["summary"]["has_lbt_data"])
                self.assertIsNone(data["summary"]["first_attempt_success_rate_pct"])
                self.assertIsNone(data["summary"]["retry_rate_pct"])
                self.assertIsNone(data["summary"]["max_attempts"])
                self.assertEqual(data["buckets"], [])
                self.assertEqual(data["packet_types"], [])
                self.assertEqual(data["packet_type_buckets"], [])
                self.assertIn("Legacy packet records are excluded", data["source_limitations"][0])
                self.assertIsNone(data["correlations"]["retry_rate_vs_avg_snr"]["coefficient"])

    def outcome(self, **updates):
        record = dict(timestamp=1_997_400, channel_id="channel_e", packet_type=5,
                      ok=True, tx_result="sent", ack_received=True,
                      cad_enabled=True, cad_detected=False, cad_retries=0, cad_reason="clear",
                      lbt_enabled=False, lbt_pass=None, lbt_retries=None,
                      rssi=None, snr=None, scheduler_attempt=1)
        record.update(updates)
        self.assertIsNotNone(self.db.store_tx_diagnostic(record))

    def test_wm1303_observed_retry_distribution_and_heatmap_exclude_unknown_and_disabled(self):
        self.api.config = {"radio_type": "wm1303"}
        self.outcome()
        self.outcome(timestamp=1_997_410, cad_retries=1, cad_reason="cleared_after_retries")
        self.outcome(timestamp=1_997_420, packet_type=4, ok=False, tx_result="blocked",
                     cad_retries=3, lbt_enabled=True, lbt_pass=False, lbt_retries=0)
        self.outcome(timestamp=1_998_310, channel_id="channel_f", packet_type=4,
                     cad_retries=2, scheduler_attempt=2)
        self.outcome(timestamp=1_998_320, ack_received=False, ok=None, cad_detected=None,
                     cad_retries=None, tx_result="timeout")
        self.outcome(timestamp=1_998_330, cad_reason="scan_error", cad_detected=None, cad_retries=None)
        self.outcome(timestamp=1_998_340, cad_enabled=False, cad_detected=None, cad_retries=None)
        self.outcome(timestamp=1_996_399, cad_retries=9)  # Outside requested window.
        self.outcome(timestamp=2_000_001, cad_retries=9)
        self.db.store_packet(dict(timestamp=1_997_400, transmitted=True, lbt_attempts=8))
        response = self.api.lbt_diagnostics(hours="1")
        self.assertTrue(response["success"], response)
        data, summary = response["data"], response["data"]["summary"]
        self.assertEqual(summary["recorded_transmissions"], 7)
        self.assertEqual(summary["total_transmissions"], 4)
        self.assertEqual(summary["unknown_transmissions"], 2)
        self.assertEqual(summary["disabled_transmissions"], 1)
        self.assertEqual(summary["scheduler_retried_transmissions"], 1)
        self.assertEqual(summary["total_attempts"], 10)
        self.assertEqual(summary["retry_rate_pct"], 75)
        self.assertEqual(summary["first_attempt_success_rate_pct"], 25)
        self.assertEqual(summary["attempts_3_plus_pct"], 50)
        self.assertEqual(summary["severe_contention_count"], 1)
        self.assertEqual(summary["max_attempts"], 4)
        self.assertEqual(summary["failed_transmissions"], 1)
        self.assertEqual(summary["busy_channel_events"], 3)
        self.assertEqual(sum(bucket["transmissions"] for bucket in data["buckets"]), 4)
        types = {entry["packet_type"]: entry for entry in data["packet_types"]}
        self.assertEqual(types[5]["retry_rate_pct"], 50)
        self.assertEqual(types[4]["retry_rate_pct"], 100)
        self.assertEqual(sum(bucket["total_attempts"] for bucket in data["packet_type_buckets"]), 10)
        channels = {entry["channel_id"]: entry for entry in data["channels"]}
        self.assertEqual(channels["channel_f"]["total_attempts"], 3)
        self.assertEqual(channels["channel_e"]["unknown_transmissions"], 2)

    def test_missing_one_enabled_check_cannot_imply_zero_retries(self):
        self.outcome(cad_enabled=False, cad_detected=None, cad_retries=None,
                     lbt_enabled=True, lbt_pass=True, lbt_retries=0)
        self.outcome(lbt_enabled=True, lbt_pass=True, lbt_retries=None)
        self.outcome(lbt_enabled=True, lbt_pass=None, lbt_retries=0)
        self.outcome(cad_detected=False, cad_retries=0, cad_reason="not_run")
        self.outcome(cad_detected=False, cad_retries=0, cad_reason="unsupported_bw")
        data = self.db.get_tx_lbt_diagnostics(1_996_400, 2_000_000)
        self.assertEqual(data["summary"]["total_transmissions"], 1)
        self.assertEqual(data["summary"]["unknown_transmissions"], 4)
        self.assertEqual(data["summary"]["first_attempt_success_rate_pct"], 100)
        self.assertIsNone(data["correlations"]["retry_rate_vs_avg_snr"]["coefficient"])

    def test_unknown_only_and_disabled_only_buckets_keep_null_rates(self):
        self.outcome(timestamp=1_997_400, ack_received=False, cad_detected=None, cad_retries=None)
        self.outcome(timestamp=1_998_600, cad_enabled=False, cad_detected=None, cad_retries=None)
        data = self.db.get_tx_lbt_diagnostics(1_996_400, 2_000_000)
        self.assertFalse(data["summary"]["has_lbt_data"])
        self.assertIsNone(data["summary"]["max_attempts"])
        self.assertEqual(data["summary"]["recorded_transmissions"], 2)
        for bucket in data["buckets"]:
            self.assertIsNone(bucket["retry_rate_pct"])
            self.assertIsNone(bucket["first_attempt_success_rate_pct"])
            self.assertIsNone(bucket["rf"]["avg_snr"])
            self.assertEqual(bucket["rf"]["traffic_volume"], 1)

    def test_rf_correlation_uses_only_measured_pairs_and_never_invents_packet_loss(self):
        for timestamp, retries, snr in ((1_997_400, 0, 5), (1_998_000, 1, 10), (1_998_600, 2, 15)):
            self.outcome(timestamp=timestamp, cad_retries=retries, rssi=-100 + snr, snr=snr)
        self.outcome(timestamp=1_999_200, cad_retries=7, rssi=-128, snr=None)
        data = self.db.get_tx_lbt_diagnostics(1_996_400, 2_000_000)
        correlation = data["correlations"]["retry_rate_vs_avg_snr"]
        self.assertEqual(correlation["sample_count"], 3)
        self.assertAlmostEqual(correlation["coefficient"], 0.8660254038)
        for bucket in data["buckets"]:
            self.assertIsNone(bucket["rf"]["packet_loss_rate_pct"])
        self.assertIsNone(data["buckets"][-1]["rf"]["avg_rssi"])
        self.assertEqual(data["correlations"]["retry_rate_vs_packet_loss_rate"],
                         {"coefficient": None, "sample_count": 0})
        sparse = self.db.get_tx_lbt_diagnostics(1_997_400, 1_998_100)
        self.assertEqual(sparse["correlations"]["retry_rate_vs_avg_snr"]["sample_count"], 2)
        self.assertIsNone(sparse["correlations"]["retry_rate_vs_avg_snr"]["coefficient"])

    def test_migration_preserves_old_packets_and_retention_bounds_outcomes(self):
        from repeater import metrics_retention

        self.db.store_packet(dict(timestamp=1_997_400, transmitted=True))
        with self.db._connect() as conn:
            conn.execute("DROP TABLE tx_diagnostics")
        self.db.close_thread_connection()
        with patch.object(SQLiteHandler, "_start_wal_checkpoint_thread"):
            upgraded = SQLiteHandler(Path(self.directory.name))
        self.addCleanup(upgraded.close_thread_connection)
        self.assertEqual(upgraded.get_recent_packets()[0]["timestamp"], 1_997_400)
        self.assertFalse(upgraded.get_tx_lbt_diagnostics(1_996_400, 2_000_000)["summary"]["has_lbt_data"])
        self.outcome(timestamp=2_000_000 - 9 * 86400)
        self.outcome(timestamp=1_997_400)
        worker = metrics_retention.MetricsRetention(db_dir=self.directory.name)
        self.addCleanup(worker.stop)
        worker.cleanup_once()
        with self.db._connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tx_diagnostics").fetchone()[0], 1)
        tables = {table["name"]: table for table in self.db.get_table_stats()["tables"]}
        self.assertEqual(tables["tx_diagnostics"]["row_count"], 1)
        self.assertEqual(self.db.purge_table("tx_diagnostics"), 1)

    def test_rf_averages_weight_observed_samples_and_constant_retry_rates_are_not_correlations(self):
        self.outcome(timestamp=1_997_400, packet_type=4, snr=None)
        self.outcome(timestamp=1_997_410, packet_type=5, snr=4)
        self.outcome(timestamp=1_997_420, packet_type=5, snr=8)
        self.outcome(timestamp=1_998_000, snr=10)
        self.outcome(timestamp=1_998_600, snr=15)
        data = self.db.get_tx_lbt_diagnostics(1_996_400, 2_000_000)
        self.assertEqual(data["buckets"][0]["rf"]["avg_snr"], 6)
        self.assertEqual(data["buckets"][0]["rf"]["snr_sample_count"], 2)
        self.assertIsNone(data["buckets"][0]["rf"]["avg_rssi"])
        self.assertEqual(data["correlations"]["retry_rate_vs_avg_snr"],
                         {"coefficient": None, "sample_count": 3})

    def test_tx_writer_rejects_invalid_measurements_atomically(self):
        for updates in (dict(timestamp=float("nan")), dict(channel_id=""), dict(cad_retries=-1),
                        dict(ack_received=None), dict(ack_received=1), dict(ok="true"),
                        dict(lbt_rssi_dbm=float("inf")), dict(scheduler_attempt=0), dict(packet_type=16)):
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                self.outcome(**updates)
        with self.db._connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tx_diagnostics").fetchone()[0], 0)

    def test_tx_records_use_bounded_nonblocking_writer_and_drain_on_shutdown(self):
        from test_storage_lifecycle import storage_module

        for module, collaborators in storage_module():
            with tempfile.TemporaryDirectory() as directory, patch.dict(sys.modules, collaborators):
                collector = module.StorageCollector({"storage": {"storage_dir": directory},
                                                     "metrics": {"rrd_enabled": False}})
                collector._write_slots = threading.BoundedSemaphore(2)
                entered, release = threading.Event(), threading.Event()
                stored = collector.sqlite_handler.store_tx_diagnostic

                def slow_store(record):
                    entered.set()
                    if not release.wait(3):
                        raise RuntimeError("Fixture TX writer was not released")
                    return stored(record)

                collector.sqlite_handler.store_tx_diagnostic = slow_store
                first = dict(timestamp=1_997_400, channel_id="channel_e", ack_received=False, pkt_hash="first")
                try:
                    self.assertTrue(collector.record_tx_diagnostic(first))
                    self.assertTrue(entered.wait(1))
                    first["pkt_hash"] = "caller-mutated"
                    self.assertTrue(collector.record_tx_diagnostic(dict(first, pkt_hash="second")))
                    self.assertFalse(collector.record_tx_diagnostic(dict(first, pkt_hash="overflow")))
                    self.assertEqual(collector.get_storage_stats()["pending_writes"], 2)
                    release.set()
                    collector.close()
                    self.assertFalse(collector.record_tx_diagnostic(first))
                    self.assertEqual(collector.get_storage_stats()["pending_writes"], 0)
                    with collector.sqlite_handler._connect() as conn:
                        rows = conn.execute("SELECT pkt_hash FROM tx_diagnostics ORDER BY id").fetchall()
                    self.assertEqual([row[0] for row in rows], ["first", "second"])
                finally:
                    release.set()
                    collector.close()
                    collector.sqlite_handler.close_thread_connection()

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
