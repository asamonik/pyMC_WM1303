"""One real SQLite/writer lifecycle scenario; network sinks are fixtures."""

import asyncio
import importlib.util
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "overlay/pymc_repeater"))
sys.path.insert(0, str(ROOT / "overlay/pymc_core/src"))

def storage_module():
    name = "repeater.data_acquisition"
    spec = importlib.util.spec_from_file_location(
        name + "._wm_storage_test",
        ROOT / "overlay/pymc_repeater/repeater/data_acquisition/storage_collector.py",
    )
    module = importlib.util.module_from_spec(spec)
    collaborators = {
        name + ".mqtt_handler": SimpleNamespace(MeshCoreToMqttPusher=Mock()),
        name + ".rrdtool_handler": SimpleNamespace(RRDToolHandler=Mock()),
        name + ".hardware_stats": SimpleNamespace(HardwareStatsCollector=Mock()),
        name + ".storage_utils": SimpleNamespace(PacketRecord=Mock()),
        name + ".websocket_handler": SimpleNamespace(
            broadcast_packet=Mock(),
            broadcast_stats=Mock(),
            has_connected_clients=lambda: False,
        ),
    }
    with patch.dict(sys.modules, collaborators):
        spec.loader.exec_module(module)
        yield module, collaborators


class StorageLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_rrd_rate_totals_cache_and_counter_restart(self):
        spec = importlib.util.spec_from_file_location(
            "_wm_rrd_test", ROOT / "overlay/pymc_repeater/repeater/data_acquisition/rrdtool_handler.py"
        )
        module = importlib.util.module_from_spec(spec)
        native = SimpleNamespace(
            last=Mock(return_value=1200000000),
            lastupdate=Mock(return_value={
                "ds": {"rx_count": 100, "tx_count": 20, "type_0": 100},
            }),
            update=Mock(),
            fetch=Mock(side_effect=lambda path, cf, *args: (
                (0, 60, 60), ("type_0",), [(dict(AVERAGE=2, MAX=8, MIN=1)[cf],)]
            )),
        )
        with tempfile.TemporaryDirectory() as directory, patch.dict(sys.modules, {"rrdtool": native}):
            spec.loader.exec_module(module)
            Path(directory, "metrics.rrd").touch()
            handler = module.RRDToolHandler(Path(directory))
            native.lastupdate.assert_called_once()
            native.last.assert_called_once()
            for resolution, expected in (("average", 2), ("max", 8), ("min", 1), ("average", 2)):
                result = handler.get_data(resolution=resolution)
                self.assertEqual(result["packet_types"]["type_0"], [expected])
                self.assertEqual(result["counter_mode"], "rate")
            self.assertEqual(native.fetch.call_count, 3)

            handler.update_packet_metrics({"timestamp": 1200000001}, {
                "rx_total": 10, "tx_total": 21, "type_counts": {"type_0": 10},
            })
            values = native.update.call_args.args[1].split(":")
            self.assertEqual((values[0], values[1], values[2], values[9]),
                             ("1200000001", "U", "21", "U"))
            self.assertFalse(handler._get_data_cache)
            handler.update_packet_metrics({"timestamp": 1200000001}, {"rx_total": 999})
            self.assertEqual(native.update.call_count, 1)
            handler.update_packet_metrics({"timestamp": 1200000002}, {
                "rx_total": 12, "tx_total": 22, "type_counts": {"type_0": 12},
            })
            self.assertEqual(native.update.call_args.args[1].split(":")[1], "12")

            native.fetch.side_effect = None
            native.fetch.return_value = (
                (0, 180, 60), ("type_0", "type_1"),
                [(2, None), (4, float("nan")), (1000, -1)],
            )
            with patch.object(module.time, "time", return_value=95):
                stats = handler.get_packet_type_stats(hours=80 / 3600)
            self.assertEqual(stats["total_packets"], 45 * 2 + 35 * 4)
            self.assertTrue(stats["approximate"])
            self.assertEqual(stats["coverage"], "observed")
            # Partial first/last buckets only; unknown, negative and out-of-window
            # rates contribute nothing. Sparse but known data remains useful.
            self.assertEqual(stats["packet_type_totals"]["Response (RESPONSE)"], 0)

    async def test_writer_is_nonblocking_ordered_bounded_and_drained(self):
        for module, collaborators in storage_module():
            with (
                tempfile.TemporaryDirectory() as directory,
                patch.dict(sys.modules, collaborators),
            ):
                root = Path(directory)
                broken = root / "broken"
                (broken / "repeater.db").mkdir(parents=True)
                with patch("threading.Thread.start") as start_thread:
                    with self.assertRaises(Exception):
                        module.StorageCollector(
                            {"storage": {"storage_dir": str(broken)}}
                        )
                    start_thread.assert_not_called()

                collector = module.StorageCollector(
                    {
                        "storage": {"storage_dir": directory},
                        "metrics": {"rrd_enabled": False},
                    }
                )
                collector.rrd_handler = SimpleNamespace(
                    update_packet_metrics=Mock(side_effect=RuntimeError("optional RRD unavailable")),
                    get_data=Mock(return_value=None),
                )
                collector._write_slots = threading.BoundedSemaphore(2)
                entered = threading.Event()
                release = threading.Event()
                published = []
                stored = collector.sqlite_handler.store_packet

                def slow_store(record):
                    entered.set()
                    if not release.wait(3):
                        raise RuntimeError("Fixture writer was not released")
                    return stored(record)

                collector.sqlite_handler.store_packet = slow_store
                collector._publish_packet_to_mqtt = lambda record: published.append(
                    record["packet_hash"]
                )
                collector.mqtt_handler = Mock()
                collector.mqtt_handler.disconnect.side_effect = lambda: (
                    published.append("disconnect")
                )
                first = {
                    "timestamp": 100,
                    "type": 1,
                    "packet_hash": "first",
                    "rssi": -100,
                    "snr": 2,
                }
                try:
                    self.assertTrue(
                        collector.record_packet(first, skip_mqtt_if_invalid=False)
                    )
                    self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                    self.assertFalse(
                        release.is_set()
                    )  # Main loop was not blocked by SQLite.
                    first["packet_hash"] = "caller-mutated"
                    self.assertTrue(
                        collector.record_packet(
                            {
                                "timestamp": 110,
                                "type": 1,
                                "packet_hash": "second",
                                "drop_reason": "invalid",
                            }
                        )
                    )
                    self.assertFalse(
                        collector.record_packet({"packet_hash": "overflow"})
                    )
                    self.assertEqual(collector.get_storage_stats()["pending_writes"], 2)
                    release.set()
                    await asyncio.to_thread(collector.close)
                    collector.close()
                    self.assertEqual(published, ["first", "disconnect"])
                    self.assertEqual(collector.rrd_handler.update_packet_metrics.call_count, 2)
                    self.assertFalse(collector._stats_thread.is_alive())
                    self.assertFalse(
                        collector.sqlite_handler._wal_checkpoint_thread.is_alive()
                    )
                    stats = collector.get_storage_stats()
                    self.assertEqual(
                        stats,
                        {"pending_writes": 0, "rejected_writes": 1, "failed_writes": 0},
                    )
                    rows = (
                        collector.sqlite_handler._connect()
                        .execute("SELECT packet_hash FROM packets ORDER BY id")
                        .fetchall()
                    )
                    self.assertEqual([row[0] for row in rows], ["first", "second"])
                    fallback = collector.get_metrics_data(start_time=60, end_time=179)
                    self.assertEqual(fallback["data_source"], "sqlite")
                    self.assertEqual(fallback["counter_mode"], "bucket_count")
                    self.assertEqual(sum(fallback["metrics"]["rx_count"]), 2)
                    self.assertFalse(
                        collector.record_packet({"packet_hash": "after-close"})
                    )
                finally:
                    release.set()
                    await asyncio.to_thread(collector.close)
                    collector.sqlite_handler.close_thread_connection()


if __name__ == "__main__":
    unittest.main()
