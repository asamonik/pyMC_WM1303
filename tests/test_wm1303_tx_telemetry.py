"""Real scheduler/UDP-ACK regressions for persistent WM1303 TX diagnostics."""

import asyncio
import json
import tempfile
import time
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, Mock, patch

from test_radio_bridge import (
    ChannelEBridge,
    ChannelFBridge,
    backend_module,
    bridge_engine,
    frame,
    tx,
)
from test_storage_lifecycle import storage_module


class ACKObservationTests(TestCase):
    def test_missing_and_malformed_fields_remain_unknown(self):
        fields = backend_module._tx_ack_diagnostics({}, {})
        self.assertTrue(all(value is None for value in fields.values()))
        fields = backend_module._tx_ack_diagnostics(
            {'enabled': 'true', 'detected': 0, 'retries': -1},
            {'enabled': 1, 'pass': 'yes', 'retries': True,
             'rssi_dbm': -128, 'threshold_dbm': float('nan')})
        self.assertTrue(all(value is None for value in fields.values()))

    def test_scan_errors_do_not_become_clear_cad_or_skip_lbt(self):
        fields = backend_module._tx_ack_diagnostics(
            {'enabled': True, 'detected': False, 'retries': 0,
             'reason': 'scan_error', 'tx_noisefloor_dbm': -128}, {})
        self.assertIsNone(fields['cad_detected'])
        self.assertIsNone(fields['tx_noisefloor_dbm'])
        manager = tx.TXQueueManager()
        manager.add_channel('channel_e', 869618000)
        manager.record_hw_cad_result('channel_e', {
            'enabled': fields['cad_enabled'], 'detected': fields['cad_detected'],
            'reason': fields['cad_reason']})
        manager.record_lbt_result('channel_e', {'enabled': None, 'pass': None})
        stats = manager.queues['channel_e'].stats
        self.assertEqual(stats['cad_clear'], 0)
        self.assertEqual(stats['lbt_skipped'], 0)


class TXObservationTests(IsolatedAsyncioTestCase):
    def backend(self, records):
        backend = backend_module.WM1303Backend({})
        backend._loop = asyncio.get_running_loop()
        backend._running = True
        backend._pull_addr = ('127.0.0.1', 9999)
        backend._sock = Mock()
        backend.set_tx_diagnostic_callback(records.append)
        return backend

    def acknowledge(self, backend, outcomes):
        outcomes = iter(outcomes)

        def sendto(datagram, address):
            ack = bytes([2, datagram[1], datagram[2], backend_module.PKT_TX_ACK])
            ack += bytes(8) + json.dumps({'txpk_ack': next(outcomes)}).encode()
            backend._loop.call_soon(backend._handle_udp, ack, address)

        backend._sock.sendto.side_effect = sendto

    @staticmethod
    def sent_ack(retries=0):
        return {
            'error': 'NONE', 'phase': 'post_tx', 'tx_result': 'sent',
            'cad': {'enabled': True, 'detected': False, 'retries': retries,
                    'reason': 'clear' if retries == 0 else 'cleared_after_retries',
                    'tx_noisefloor_dbm': -104},
            'lbt': {'enabled': True, 'pass': True, 'retries': 0,
                    'rssi_dbm': -105, 'threshold_dbm': -80},
        }

    async def test_every_scheduler_retry_is_observed_once_on_a_e_and_f(self):
        for channel in ('channel_a', 'channel_e', 'channel_f'):
            with self.subTest(channel=channel):
                records = []
                backend = self.backend(records)
                manager = backend._tx_queue_manager = tx.TXQueueManager()
                manager.add_channel(channel, 869618000)
                scheduler = tx.GlobalTXScheduler(backend._send_for_scheduler, manager.queues)
                self.acknowledge(backend, [
                    {'error': 'TOO_LATE'}, {'error': 'COLLISION_PACKET'}, self.sent_ack(3),
                ])
                await scheduler.start()
                try:
                    with tx.tx_diagnostic_context(rssi=-112, snr=-4.5):
                        result = await asyncio.wait_for(backend.send(channel, frame(kind=5)), 1)
                    self.assertTrue(result['ok'])
                    self.assertEqual(len(records), 3)
                    self.assertEqual([row['scheduler_attempt'] for row in records], [1, 2, 3])
                    self.assertEqual([row['tx_result'] for row in records], ['dropped', 'dropped', 'sent'])
                    for row in records:
                        self.assertEqual(row['channel_id'], channel)
                        self.assertEqual(row['packet_type'], 5)
                        self.assertEqual((row['rssi'], row['snr']), (-112, -4.5))
                        self.assertTrue(row['ack_received'])
                    for row in records[:2]:
                        self.assertIsNone(row['cad_enabled'])
                        self.assertIsNone(row['cad_retries'])
                        self.assertIsNone(row['lbt_retries'])
                    self.assertEqual(records[2]['cad_retries'], 3)
                    self.assertEqual(records[2]['lbt_retries'], 0)
                    self.assertEqual(backend._hourly_tx_ok, 1)
                    self.assertEqual(backend._hourly_tx_fail, 2)
                    self.assertEqual(tx.get_tx_diagnostic_context(), {})
                finally:
                    await scheduler.stop()

    async def test_bridge_e_and_f_keep_per_packet_rf_metadata(self):
        records = []
        backend = self.backend(records)
        manager = backend._tx_queue_manager = tx.TXQueueManager()
        for channel in ('channel_e', 'channel_f'):
            manager.add_channel(channel, 869618000)
        scheduler = tx.GlobalTXScheduler(backend._send_for_scheduler, manager.queues)
        bridge = bridge_engine.BridgeEngine([], rules=[
            {'source': 'mqtt', 'target': 'channel_e'},
            {'source': 'mqtt', 'target': 'channel_f'},
        ])
        bridge._running = True
        e = ChannelEBridge(bridge, backend=backend)
        f = ChannelFBridge(bridge, backend=backend)
        bridge._endpoint_handlers.update(channel_e=e._tx_handler, channel_f=f._tx_handler)
        self.acknowledge(backend, [self.sent_ack()] * 4)
        await scheduler.start()
        try:
            outcomes = await asyncio.wait_for(asyncio.gather(
                bridge.inject_packet('mqtt', frame(kind=5, payload=b'first'), rssi=-115, snr=-6),
                bridge.inject_packet('mqtt', frame(kind=15, payload=b'second'), rssi=-90, snr=4),
            ), 1)
            self.assertEqual(outcomes, [True, True])
            self.assertEqual(len(records), 4)
            for kind, signal in ((5, (-115, -6)), (15, (-90, 4))):
                rows = [row for row in records if row['packet_type'] == kind]
                self.assertEqual({row['channel_id'] for row in rows}, {'channel_e', 'channel_f'})
                self.assertEqual([(row['rssi'], row['snr']) for row in rows], [signal, signal])
            self.assertEqual(tx.get_tx_diagnostic_context(), {})
        finally:
            await scheduler.stop()

    async def test_direct_send_and_callback_failures_preserve_result(self):
        records = []
        backend = self.backend(records)
        backend.channels = {'channel_a': {'active': True, 'frequency': 869618000}}
        self.acknowledge(backend, [self.sent_ack()])
        result = await backend.send('channel_a', frame(kind=5))
        self.assertTrue(result['ok'])
        self.assertEqual(len(records), 1)
        self.assertIsNone(records[0]['scheduler_attempt'])
        self.assertIsNone(records[0]['rssi'])
        self.assertIsNone(records[0]['snr'])

        backend.set_tx_diagnostic_callback(Mock(side_effect=RuntimeError('storage closed')))
        self.acknowledge(backend, [self.sent_ack()])
        self.assertTrue((await backend.send('channel_a', frame(kind=5)))['ok'])

    async def test_missing_ack_and_cancelled_send_keep_unknown_checks(self):
        records = []
        backend = self.backend(records)
        txpk = tx.ChannelTXQueue('channel_e', 869618000, 125, 8, 5).build_txpk(frame())
        for effect in (asyncio.TimeoutError, asyncio.CancelledError):
            with self.subTest(effect=effect), patch.object(
                backend_module.asyncio, 'wait_for', new=AsyncMock(side_effect=effect)
            ):
                if effect is asyncio.CancelledError:
                    with self.assertRaises(asyncio.CancelledError):
                        await backend._send_pull_resp(txpk, 'channel_e')
                else:
                    result = await backend._send_pull_resp(txpk, 'channel_e')
                    self.assertFalse(result['ack_received'])
            backend._last_tx_end = 0
        self.assertEqual(len(records), 2)
        self.assertFalse(records[0]['ack_received'])
        self.assertFalse(records[1]['ack_received'])
        self.assertIsNone(records[1]['ok'])
        for row in records:
            for key in ('cad_enabled', 'cad_detected', 'cad_retries',
                        'lbt_enabled', 'lbt_pass', 'lbt_retries'):
                self.assertIsNone(row[key])

    async def test_udp_scheduler_records_reach_writer_sqlite_and_diagnosis(self):
        for module, collaborators in storage_module():
            with tempfile.TemporaryDirectory() as directory, patch.dict('sys.modules', collaborators):
                storage = module.StorageCollector({
                    'storage': {'storage_dir': directory},
                    'metrics': {'rrd_enabled': False},
                })
                backend = self.backend([])
                backend.set_tx_diagnostic_callback(storage.record_tx_diagnostic)
                manager = backend._tx_queue_manager = tx.TXQueueManager()
                manager.add_channel('channel_e', 869618000)
                scheduler = tx.GlobalTXScheduler(backend._send_for_scheduler, manager.queues)
                self.acknowledge(backend, [
                    {'error': 'TOO_LATE'}, self.sent_ack(2),
                    {'error': 'NONE', 'phase': 'post_tx', 'tx_result': 'blocked',
                     'cad': {'enabled': True, 'detected': False, 'retries': 0, 'reason': 'clear'},
                     'lbt': {'enabled': True, 'pass': False, 'retries': 0,
                             'rssi_dbm': -60, 'threshold_dbm': -80}},
                ])
                await scheduler.start()
                try:
                    with tx.tx_diagnostic_context(rssi=-111, snr=-3):
                        self.assertTrue((await backend.send('channel_e', frame(kind=5)))['ok'])
                        self.assertFalse((await backend.send('channel_e', frame(kind=15)))['ok'])
                    # A sentinel submitted to the same writer drains accepted
                    # records without blocking the event loop on SQLite work.
                    await asyncio.wrap_future(storage._db_executor.submit(lambda: None))
                    data = storage.sqlite_handler.get_tx_lbt_diagnostics(time.time() - 60, time.time() + 1)
                    summary = data['summary']
                    self.assertEqual(summary['recorded_transmissions'], 3)
                    self.assertEqual(summary['total_transmissions'], 2)
                    self.assertEqual(summary['unknown_transmissions'], 1)
                    self.assertEqual(summary['total_attempts'], 4)
                    self.assertEqual(summary['retry_rate_pct'], 50)
                    self.assertEqual(summary['failed_transmissions'], 1)
                    self.assertEqual(summary['scheduler_retried_transmissions'], 1)
                    self.assertEqual(storage.get_storage_stats()['failed_writes'], 0)
                    rows = storage.sqlite_handler._connect().execute(
                        'SELECT channel_id, packet_type, scheduler_attempt, rssi, snr '
                        'FROM tx_diagnostics ORDER BY id').fetchall()
                    self.assertEqual(rows, [
                        ('channel_e', 5, 1, -111, -3),
                        ('channel_e', 5, 2, -111, -3),
                        ('channel_e', 15, 1, -111, -3),
                    ])
                    self.assertEqual(storage.sqlite_handler._connect().execute(
                        'SELECT COUNT(*) FROM packets').fetchone()[0], 0)

                    # Early rejected operations and cancelled ACK waits also
                    # survive the strict persisted observed-ACK bool contract.
                    backend._pull_addr = None
                    rejected = await backend._send_pull_resp(
                        manager.queues['channel_e'].build_txpk(frame()), 'channel_e')
                    self.assertEqual(rejected['error'], 'no_pull_addr')
                    backend._pull_addr = ('127.0.0.1', 9999)
                    backend._last_tx_end = 0
                    with (patch.object(backend, '_send_pull_resp_impl', side_effect=asyncio.CancelledError),
                          self.assertRaises(asyncio.CancelledError)):
                        await backend._send_pull_resp(
                            manager.queues['channel_e'].build_txpk(frame()), 'channel_e')
                    await asyncio.wrap_future(storage._db_executor.submit(lambda: None))
                    data = storage.get_tx_lbt_diagnostics(time.time() - 60, time.time() + 1)
                    self.assertEqual(data['summary']['recorded_transmissions'], 5)
                    self.assertEqual(data['summary']['unknown_transmissions'], 3)
                    self.assertEqual(data['summary']['total_transmissions'], 2)
                    self.assertEqual(storage.get_storage_stats()['failed_writes'], 0)
                finally:
                    await scheduler.stop()
                    backend.set_tx_diagnostic_callback(None)
                    storage.sqlite_handler.close_thread_connection()
                    await asyncio.to_thread(storage.close)

    async def test_deployed_module_graph_routes_a_e_f_with_one_shared_context(self):
        # Import the deployed package normally here: the core, virtual radio,
        # bridge and scheduler must share the same ContextVar, without stubs.
        try:
            from openhop_core.hardware.base import LoRaRadio  # noqa: F401
        except ModuleNotFoundError as exc:
            if exc.name not in ('openhop_core', 'openhop_core.hardware', 'openhop_core.hardware.base'):
                raise
            self.skipTest('The upstream OpenHop core is not installed in standalone CI')
        from openhop_core.hardware.tx_queue import TXQueueManager
        from openhop_core.hardware.virtual_radio import VirtualLoRaRadio
        from openhop_core.hardware.wm1303_backend import WM1303Backend

        if not hasattr(WM1303Backend, 'set_tx_diagnostic_callback'):
            self.skipTest('The installed OpenHop core lacks the deployed WM1303 telemetry overlay')

        records = []
        backend = WM1303Backend({})
        backend._loop = asyncio.get_running_loop()
        backend._running = True
        backend._pull_addr = ('127.0.0.1', 9999)
        backend._sock = Mock()
        backend.set_tx_diagnostic_callback(records.append)
        manager = backend._tx_queue_manager = TXQueueManager()
        for channel in ('channel_a', 'channel_e', 'channel_f'):
            manager.add_channel(channel, 869618000)
        radio = VirtualLoRaRadio(backend, 'channel_a', {'frequency': 869618000})
        bridge = bridge_engine.BridgeEngine([radio], rules=[
            {'source': 'repeater', 'target': channel}
            for channel in ('channel_a', 'channel_e', 'channel_f')
        ])
        bridge._running = True
        e = ChannelEBridge(bridge, backend=backend)
        f = ChannelFBridge(bridge, backend=backend)
        bridge._endpoint_handlers.update(channel_e=e._tx_handler, channel_f=f._tx_handler)
        self.acknowledge(backend, [self.sent_ack(1)] * 3)
        with patch.object(backend, '_start_noise_floor_monitor'):
            await backend.ensure_tx_queues_started()
        try:
            sent = await asyncio.wait_for(bridge.inject_packet(
                'repeater', frame(), rssi=-110, snr=-2.5), 1)
            self.assertTrue(sent)
            self.assertEqual([row['channel_id'] for row in records],
                             ['channel_a', 'channel_e', 'channel_f'])
            self.assertEqual([(row['rssi'], row['snr']) for row in records], [(-110, -2.5)] * 3)
            self.assertEqual([row['cad_retries'] for row in records], [1, 1, 1])
            self.assertEqual([row['scheduler_attempt'] for row in records], [1, 1, 1])
            self.assertEqual(backend._hourly_tx_ok, 3)
        finally:
            await backend._global_tx_scheduler.stop()
            backend._running = False
            backend.set_tx_diagnostic_callback(None)
