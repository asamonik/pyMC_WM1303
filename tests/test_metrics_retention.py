"""Small SQLite checks for retention, chart conservation and worker shutdown."""
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'overlay/pymc_repeater'))
from repeater import metrics_retention as retention
from repeater.web.tiered_query import tiered_channel_query


class MetricsRetentionTests(unittest.TestCase):
    def test_lifetime_packet_counts_survive_migration_pruning_and_restart(self):
        from repeater.data_acquisition.sqlite_handler import SQLiteHandler

        with tempfile.TemporaryDirectory() as directory:
            handlers = []
            def open_handler():
                handler = SQLiteHandler(Path(directory))
                handler.stop_wal_checkpoint_thread()
                handlers.append(handler)
                return handler
            try:
                legacy = open_handler()
                # Recreate the pre-ledger schema with retained historical rows.
                with legacy._connect() as conn:
                    conn.execute('DROP TRIGGER update_packet_lifetime_counts')
                    conn.execute('DROP TABLE packet_lifetime_counts')
                    conn.execute("DELETE FROM migrations WHERE migration_name='wm1303_packet_lifetime_counts'")
                legacy.store_packet({'timestamp': 1, 'type': 5, 'transmitted': True})
                legacy.store_packet({'timestamp': 2, 'type': -1})
                legacy.close_thread_connection()
                current = open_handler()
                baseline = current.get_cumulative_counts()
                self.assertEqual((baseline['rx_total'], baseline['tx_total'], baseline['drop_total']), (2, 1, 1))
                self.assertEqual(baseline['type_counts']['type_other'], 1)
                current.store_packet({'timestamp': 3, 'type': 5})
                expected = current.get_cumulative_counts()
                self.assertEqual(expected['rx_total'], 3)
                self.assertEqual(expected['type_counts']['type_5'], 2)
                # A rejected insert must not advance any counter.
                self.assertIsNone(current.store_packet({'timestamp': None, 'type': 5}))
                self.assertEqual(current.get_cumulative_counts(), expected)
                current.cleanup_old_data(days=7)
                self.assertEqual(current.get_recent_packets(), [])
                self.assertEqual(current.get_cumulative_counts(), expected)
                current.store_packet({'type': 3})
                current.purge_table('packets')
                current.close_thread_connection()
                restarted = open_handler()
                self.assertEqual(restarted.get_cumulative_counts()['rx_total'], 4)
                self.assertEqual(restarted.get_recent_packets(), [])
                restarted.store_packet({'type': 3})
                self.assertEqual(restarted.get_cumulative_counts()['rx_total'], 5)
                # Reads stay bounded by packet types, independent of log size.
                self.assertLessEqual(restarted._connect().execute(
                    'SELECT COUNT(*) FROM packet_lifetime_counts').fetchone()[0], 17)
            finally:
                for handler in handlers:
                    handler.stop_wal_checkpoint_thread()
                    handler.close_thread_connection()

    def test_late_samples_and_outages_preserve_chart_totals(self):
        with tempfile.TemporaryDirectory() as directory:
            worker = retention.MetricsRetention(db_dir=directory)
            conn = sqlite3.connect(str(Path(directory) / 'repeater.db'))
            self.addCleanup(conn.close)
            conn.execute('CREATE TABLE packet_activity(timestamp REAL, channel_id TEXT, rx_count INTEGER, tx_count INTEGER)')
            now = 1_000_000
            def add(ts, channel, count):
                conn.execute('INSERT INTO packet_activity VALUES (?, ?, ?, 0)', (ts, channel, count))
                conn.commit()
            def total():
                return sum(row['total_rx_count'] for row in tiered_channel_query(
                    conn, 'packet_activity', None, now - 7 * 86400, now, 900))
            try:
                add(now - 8 * 3600, 'channel_a', 2)
                add(now - 4 * 86400, 'channel_b', 3)
                add(now - 2 * 86400, 'channel_d', 11)
                worker._ensure_summary_tables()
                self.assertEqual(total(), 16)  # Tables exist, but data is still raw.
                with patch.object(retention.time, 'time', return_value=now):
                    worker.cleanup_once()
                    self.assertEqual(total(), 16)
                    self.assertEqual(conn.execute('SELECT COUNT(*) FROM packet_activity_10m').fetchone()[0], 0)
                    self.assertEqual(conn.execute("SELECT total_rx_count FROM packet_activity_1m WHERE channel_id='channel_d'").fetchone()[0], 11)
                    add(now - 8 * 3600 + 1, 'channel_a', 5)
                    add(now - 4 * 86400 + 1, 'channel_c', 7)
                    self.assertEqual(total(), 28)  # Split across raw and summaries.
                    worker.cleanup_once()
                    worker.cleanup_once()
                    self.assertEqual(total(), 28)
                # After a multi-day outage, old 1m rows must advance to cold.
                with patch.object(retention.time, 'time', return_value=now + 3 * 86400):
                    worker.cleanup_once()
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM packet_activity_1m').fetchone()[0], 0)
                self.assertEqual(total(), 28)
            finally:
                worker.stop()

        cfg = next(cfg for cfg in retention.DOWNSAMPLE_TABLES if cfg['table'] == 'packet_activity')
        with closing(sqlite3.connect(':memory:')) as conn:
            conn.execute('CREATE TABLE packet_activity(timestamp REAL, channel_id TEXT, rx_count INTEGER, tx_count INTEGER)')
            for suffix in ('1m', '10m', '15m'):
                retention._create_summary_table(conn, cfg, suffix)
            conn.execute("INSERT INTO packet_activity VALUES (150, 'channel_a', 1, 0)")
            retention._aggregate_from_source(conn, cfg, 0, 200, 60, '1m')
            # A summary starting before the requested window still overlaps it.
            self.assertEqual(tiered_channel_query(conn, 'packet_activity', None, 125, 200, 60)[0]['total_rx_count'], 1)
            conn.execute('DELETE FROM packet_activity_1m')
            conn.executemany("INSERT INTO packet_activity VALUES (?, 'channel_a', ?, 0)", [(600, 5), (1000, 7), (1300, 11)])
            def buckets():
                return [(r['bucket_ts'], r['total_rx_count']) for r in tiered_channel_query(
                    conn, 'packet_activity', None, 0, 1800, 900)]
            self.assertEqual(buckets(), [(0, 5), (900, 18)])
            retention._aggregate_from_source(conn, cfg, 0, 1200, 60, '1m')
            retention._aggregate_from_summary(conn, cfg, 0, 1200, '1m', 900, '15m')
            self.assertEqual(buckets(), [(0, 5), (900, 18)])
            # Old 10m data cannot be split into 15m; use a common 30m width.
            conn.execute("INSERT INTO packet_activity_10m(bucket_ts, channel_id, sample_count, total_rx_count, total_tx_count) VALUES (600, 'channel_a', 1, 4, 0)")
            rows = tiered_channel_query(conn, 'packet_activity', None, 0, 1800, 900)
            self.assertEqual([(r['bucket_ts'], r['bucket_seconds'], r['total_rx_count']) for r in rows], [(0, 1800, 27)])
            with self.assertRaises(ValueError):
                retention._aggregate_from_summary(conn, cfg, 0, 1800, '10m', 900, '15m')
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM packet_activity_10m').fetchone()[0], 1)

    def test_failed_source_delete_rolls_back_summary_insert(self):
        cfg = next(cfg for cfg in retention.DOWNSAMPLE_TABLES if cfg['table'] == 'packet_activity')
        with closing(sqlite3.connect(':memory:')) as conn:
            conn.execute('CREATE TABLE packet_activity(timestamp REAL, channel_id TEXT, rx_count INTEGER, tx_count INTEGER)')
            conn.execute("INSERT INTO packet_activity VALUES (120, 'channel_a', 5, 0)")
            retention._create_summary_table(conn, cfg, '1m')
            conn.execute("CREATE TRIGGER block_delete BEFORE DELETE ON packet_activity BEGIN SELECT RAISE(ABORT, 'fixture'); END")
            with self.assertRaises(sqlite3.DatabaseError):
                retention._aggregate_from_source(conn, cfg, 0, 200, 60, '1m')
            conn.commit()  # The production caller logs errors and continues.
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM packet_activity').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM packet_activity_1m').fetchone()[0], 0)

            cfg = next(cfg for cfg in retention.DOWNSAMPLE_TABLES if cfg['table'] == 'noise_floor_history')
            conn.execute('CREATE TABLE noise_floor_history(timestamp REAL, channel_id TEXT, noise_floor_dbm REAL, samples_collected INTEGER, samples_accepted INTEGER, min_rssi REAL, max_rssi REAL)')
            conn.executemany("INSERT INTO noise_floor_history VALUES (?, 'channel_a', ?, 1, 1, NULL, NULL)",
                             [(121, -100)] + [(121, None)] * 9 + [(181, -50)])
            for suffix in ('1m', '15m'):
                retention._create_summary_table(conn, cfg, suffix)
            def average():
                rows = tiered_channel_query(conn, 'noise_floor_history', None, 0, 900, 900,
                                           columns=['avg_noise_floor_dbm'])
                self.assertNotIn('avg_noise_floor_dbm_count', rows[0])
                return rows[0]['avg_noise_floor_dbm']
            self.assertEqual(average(), -75)
            retention._aggregate_from_source(conn, cfg, 0, 180, 60, '1m')
            self.assertEqual(average(), -75)  # Raw + summary, nine NULL samples.
            retention._aggregate_from_source(conn, cfg, 180, 900, 60, '1m')
            retention._aggregate_from_summary(conn, cfg, 0, 900, '1m', 900, '15m')
            self.assertEqual(average(), -75)
            self.assertEqual(conn.execute('SELECT avg_noise_floor_dbm_count FROM noise_floor_history_15m').fetchone()[0], 2)
            # Queries before migration and migration itself both support old
            # summaries; only their available sample_count can be recovered.
            conn.execute('CREATE TABLE noise_floor_history_10m(bucket_ts REAL, channel_id TEXT, sample_count REAL, avg_noise_floor_dbm REAL)')
            conn.execute("INSERT INTO noise_floor_history_10m VALUES (600, 'channel_b', 3, -90)")
            self.assertEqual(tiered_channel_query(conn, 'noise_floor_history', 'channel_b', 0, 1200, 600,
                             columns=['avg_noise_floor_dbm'])[0]['avg_noise_floor_dbm'], -90)
            retention._create_summary_table(conn, cfg, '10m')
            self.assertEqual(conn.execute('SELECT avg_noise_floor_dbm_count FROM noise_floor_history_10m').fetchone()[0], 3)
            # avg_hop_count is an average despite its counter-like suffix.
            cfg = next(cfg for cfg in retention.DOWNSAMPLE_TABLES if cfg['table'] == 'packet_metrics')
            conn.execute('CREATE TABLE packet_metrics(timestamp REAL, channel_id TEXT, direction TEXT, rssi REAL, snr REAL, airtime_ms REAL, length INTEGER, hop_count INTEGER, crc_ok INTEGER)')
            conn.executemany("INSERT INTO packet_metrics VALUES (?, 'channel_a', 'rx', NULL, NULL, NULL, 8, ?, 1)", [(121, 2), (181, 4)])
            for suffix in ('1m', '15m'):
                retention._create_summary_table(conn, cfg, suffix)
            retention._aggregate_from_source(conn, cfg, 0, 900, 60, '1m')
            retention._aggregate_from_summary(conn, cfg, 0, 900, '1m', 900, '15m')
            self.assertEqual(tiered_channel_query(conn, 'packet_metrics', None, 0, 900, 900)[0]['avg_hop_count'], 3)

    def test_worker_uses_config_and_stops_during_initial_delay(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(retention, '_singleton', None):
            conn = sqlite3.connect(str(Path(directory) / 'repeater.db'))
            self.addCleanup(conn.close)
            conn.execute('CREATE TABLE channel_stats_history(timestamp REAL, channel_id TEXT, rx_count INTEGER)')
            now = 2_000_000
            start = now - 10 * 86400
            conn.executemany("INSERT INTO channel_stats_history VALUES (?, 'channel_a', ?)",
                             [(start, 0), (start + 60, 10), (start + 120, 20),
                              (start + 180, 3), (start + 240, 8)])
            conn.commit()
            worker = retention.start({'storage': {'storage_dir': directory, 'retention': {'metrics_days': 14}}})
            try:
                self.assertEqual(worker.db_dir, directory)
                self.assertEqual(worker.retention_days, 14)
                first = worker._thread
                worker.start()
                self.assertIs(worker._thread, first)
                def deltas():
                    return [r['total_rx_count'] for r in tiered_channel_query(
                        conn, 'channel_stats_history', 'channel_a', start + 60, now, 60,
                        columns=['total_rx_count'])]
                self.assertEqual(deltas(), [10, 10, 3, 5])
                with patch.object(retention.time, 'time', return_value=now):
                    worker.cleanup_once()
                self.assertEqual(deltas(), [10, 10, 3, 5])  # Keep transitions and resets after cleanup.
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM channel_stats_history').fetchone()[0], 5)
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM channel_stats_history_15m').fetchone()[0], 0)
                # Expiry keeps one baseline, not an entire obsolete channel.
                conn.execute("INSERT INTO channel_stats_history VALUES (?, 'channel_b', 99)", (start,))
                conn.commit()
                later = start + 14 * 86400 + 90
                with patch.object(retention.time, 'time', return_value=later):
                    worker.cleanup_once()
                rows = tiered_channel_query(conn, 'channel_stats_history', 'channel_a', start + 90, later, 60,
                                            columns=['total_rx_count'])
                self.assertEqual([r['total_rx_count'] for r in rows], [10, 3, 5])
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM channel_stats_history').fetchone()[0], 4)
            finally:
                worker.stop()
            self.assertFalse(first.is_alive())
            self.assertEqual(retention._shared_conn_instances, {})


if __name__ == '__main__':
    unittest.main()
