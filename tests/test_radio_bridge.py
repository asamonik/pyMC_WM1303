"""Hardware-free radio/bridge regressions using real overlay implementations."""
import asyncio
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "overlay/pymc_repeater"))
sys.path.insert(0, str(ROOT / "overlay/pymc_core/src"))

# An installed regular openhop_core package takes precedence over this
# namespace overlay. Always test this checkout's wire helper, even then.
wire_spec = importlib.util.spec_from_file_location(
    "openhop_core.meshcore_wire", ROOT / "overlay/pymc_core/src/openhop_core/meshcore_wire.py")
wire = importlib.util.module_from_spec(wire_spec)
sys.modules[wire_spec.name] = wire
wire_spec.loader.exec_module(wire)

from openhop_core.meshcore_wire import packet_hash
from repeater import bridge_engine
from repeater.channel_e_bridge import ChannelEBridge
from repeater.channel_f_bridge import ChannelFBridge
from repeater.uniform_tracer import _packet_hash8


def load_hardware(name):
    # The base class is supplied by installed OpenHop, outside this overlay.
    # Stub that interface only; do not import/start any real radio backend.
    parent = ModuleType("wm1303_test_hardware")
    parent.__path__ = []
    base = ModuleType("wm1303_test_hardware.base")
    base.LoRaRadio = object
    spec = importlib.util.spec_from_file_location(
        "wm1303_test_hardware." + name,
        ROOT / "overlay/pymc_core/src/openhop_core/hardware" / (name + ".py"))
    result = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {parent.__name__: parent, base.__name__: base}):
        spec.loader.exec_module(result)
    return result


tx = load_hardware("tx_queue")
virtual = load_hardware("virtual_radio")
regions = load_hardware("region_config")
with patch.dict(sys.modules, {"openhop_core.hardware.tx_queue": tx,
                              "yaml": SimpleNamespace(safe_load=lambda stream: {})}):
    with patch("subprocess.check_output", side_effect=OSError("no service in unit tests")):
        backend_module = load_hardware("wm1303_backend")


def frame(kind=15, route=1, path=b"", width=1, payload=b"content"):
    header = bytes([(kind << 2) | route])
    transport = b"\x01\x02\x03\x04" if route in (0, 3) else b""
    return header + transport + bytes([((width - 1) << 6) | (len(path) // width)]) + path + payload


class PacketIdentityTests(unittest.TestCase):
    def test_routing_changes_keep_upstream_packet_identity(self):
        expected = hashlib.sha256(b"\x0fcontent").hexdigest()
        for route in range(4):
            for width in (1, 2, 3):
                data = frame(route=route, width=width, path=bytes(width * 3))
                with self.subTest(route=route, width=width):
                    self.assertEqual(packet_hash(data, 64), expected)
                    self.assertEqual(_packet_hash8(data), bridge_engine._stable_hash(data, 8))

    def test_trace_return_path_is_distinct(self):
        # Packet.cpp hashes the uint16 path_len, not just the wire byte.
        outbound = frame(kind=9, path=b"\x10")
        returning = frame(kind=9, path=b"\x10\x20")
        expected = hashlib.sha256(b"\x09\x01\x00content").hexdigest()
        self.assertEqual(packet_hash(outbound, 64), expected)
        self.assertNotEqual(packet_hash(outbound), packet_hash(returning))

    def test_empty_custom_payload_retains_packet_identity(self):
        expected = hashlib.sha256(b"\x0f").hexdigest()
        for route in range(4):
            self.assertEqual(packet_hash(frame(route=route, payload=b""), 64), expected)

    def test_malformed_paths_have_full_packet_identity(self):
        for data in (b"", b"\x3d", b"\x3c\x00", b"\x3d\xc0payload", b"\x3d\x3fshort"):
            with self.subTest(data=data):
                self.assertEqual(packet_hash(data, 64), hashlib.sha256(data).hexdigest())

    def test_dedup_ttl_expires_before_periodic_cleanup(self):
        bridge = bridge_engine.BridgeEngine([], dedup_ttl=0.5)
        with patch.object(bridge_engine.time, "monotonic", side_effect=[100, 100.1, 100.5]):
            self.assertFalse(bridge._is_duplicate(frame()))
            self.assertTrue(bridge._is_duplicate(frame(path=b"\x12")))
            self.assertFalse(bridge._is_duplicate(frame()))


class TXParametersTests(unittest.TestCase):
    def test_six_channels_include_e_and_f(self):
        manager = tx.TXQueueManager()
        for channel in "abcdef":
            manager.add_channel("channel_" + channel, 869618000)
        self.assertEqual(len(manager.queues), 6)
        with self.assertRaises(ValueError):
            manager.add_channel("extra", 869618000)

    def test_duplicate_registration_does_not_replace_pending_queue(self):
        manager = tx.TXQueueManager()
        manager.add_channel("channel_a", 869618000)
        original = manager.queues["channel_a"]
        with self.assertRaises(ValueError):
            manager.add_channel("channel_a", 868000000)
        self.assertIs(manager.queues["channel_a"], original)

    def test_exact_narrow_bandwidth_and_hal_coding_rate(self):
        queue = tx.ChannelTXQueue("channel_e", 869618000, 62.5, 8, 1)
        txpk = queue.build_txpk(b"test")
        self.assertEqual(txpk["datr"], "SF8BW62")  # patched HAL token for 62.5 kHz
        self.assertEqual(txpk["codr"], "4/5")
        self.assertEqual(txpk["freq"], 869.618)
        self.assertEqual(txpk["rfch"], 0)
        self.assertFalse(txpk["ncrc"])
        with self.assertRaises(ValueError):
            tx.ChannelTXQueue("channel_e", 869618000, 31.25, 8, 5)

    def test_low_data_rate_airtime_at_narrow_and_wide_bandwidths(self):
        self.assertAlmostEqual(tx.estimate_lora_airtime_ms(60, sf=10, bw_hz=62500), 1789.952)
        self.assertAlmostEqual(tx.estimate_lora_airtime_ms(60, sf=12, bw_hz=250000), 1462.272)
        self.assertAlmostEqual(tx.estimate_lora_airtime_ms(60, sf=8, cr=1), 223.744)


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    def queue(self, **kwargs):
        return tx.ChannelTXQueue("channel_a", 869618000, 125, 8, 5, **kwargs)

    async def test_empty_scheduler_accepts_channels_added_after_start(self):
        queues = {}
        send = AsyncMock(return_value={"ok": True})
        scheduler = tx.GlobalTXScheduler(send, queues)
        await scheduler.start()
        try:
            await asyncio.sleep(0)
            self.assertFalse(scheduler._task.done())
            queue = self.queue()
            queues[queue.channel_id] = queue
            self.assertTrue((await asyncio.wait_for(queue.enqueue(b"test"), 1))["ok"])
            send.assert_awaited_once()
        finally:
            await scheduler.stop()

    async def test_shutdown_resolves_active_and_queued_callers(self):
        entered = asyncio.Event()
        async def send(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        queue = self.queue()
        scheduler = tx.GlobalTXScheduler(send, {queue.channel_id: queue})
        await scheduler.start()
        first = asyncio.create_task(queue.enqueue(b"first"))
        await asyncio.wait_for(entered.wait(), 1)
        second = asyncio.create_task(queue.enqueue(b"second"))
        await asyncio.sleep(0)
        await scheduler.stop()
        self.assertEqual((await first)["error"], "scheduler_stopped")
        self.assertEqual((await second)["error"], "queue_stopped")
        self.assertEqual((await queue.enqueue(b"later"))["error"], "queue_stopped")

    async def test_cancellation_during_hold_never_reaches_radio(self):
        queue = self.queue()
        send = AsyncMock(return_value={"ok": True})
        entered = asyncio.Event()
        hold_until = asyncio.get_running_loop().time() + 0.03
        def hold():
            entered.set()
            return hold_until
        scheduler = tx.GlobalTXScheduler(send, {queue.channel_id: queue}, tx_hold_getter=hold)
        await scheduler.start()
        try:
            caller = asyncio.create_task(queue.enqueue(b"cancel me"))
            await asyncio.wait_for(entered.wait(), 1)
            caller.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await caller
            await asyncio.sleep(0.05)
            send.assert_not_awaited()
            self.assertEqual(queue.stats["dropped_stale"], 1)
        finally:
            await scheduler.stop()

    async def test_ttl_is_rechecked_after_transmit_hold(self):
        queue = self.queue(ttl_seconds=0.01)
        send = AsyncMock(return_value={"ok": True})
        hold_until = asyncio.get_running_loop().time() + 0.03
        scheduler = tx.GlobalTXScheduler(send, {queue.channel_id: queue}, tx_hold_getter=lambda: hold_until)
        await scheduler.start()
        try:
            result = await asyncio.wait_for(queue.enqueue(b"expires"), 1)
            self.assertEqual(result["error"], "ttl_expired")
            send.assert_not_awaited()
        finally:
            await scheduler.stop()

    async def test_overflow_resolves_oldest_and_keeps_new_packet(self):
        for policy in ('drop_oldest', 'drop_newest'):
            with self.subTest(policy=policy):
                queue = self.queue(queue_size=1, overflow_policy=policy)
                oldest = asyncio.create_task(queue.enqueue(b"oldest"))
                await asyncio.sleep(0)
                newest = asyncio.create_task(queue.enqueue(b"newest"))
                await asyncio.sleep(0)
                dropped, retained = (oldest, newest) if policy == 'drop_oldest' else (newest, oldest)
                self.assertEqual((await dropped)["error"], "dropped_overflow")
                queue.stop()
                self.assertEqual((await retained)["error"], "queue_stopped")
                await queue.queue.join()

        # E/F cancellation frees admission capacity while RF is occupied.
        for channel, plugin in (('channel_e', ChannelEBridge), ('channel_f', ChannelFBridge)):
            for policy in ('drop_oldest', 'drop_newest'):
                queue = tx.ChannelTXQueue(channel, 869618000, 125, 8, 5,
                                          queue_size=2, overflow_policy=policy)
                backend = SimpleNamespace(_tx_queue_manager=SimpleNamespace(queues={channel: queue}))
                endpoint = plugin(None, backend=backend)
                retained = asyncio.create_task(endpoint._tx_handler(frame(payload=b'first')))
                cancelled = asyncio.create_task(endpoint._tx_handler(frame(payload=b'cancelled')))
                await asyncio.sleep(0)
                cancelled.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await cancelled
                newest = asyncio.create_task(endpoint._tx_handler(frame(payload=b'last')))
                await asyncio.sleep(0)
                sent = []
                async def send(txpk, *args, **kwargs):
                    sent.append(base64.b64decode(txpk['data']))
                    return {'ok': True}
                scheduler = tx.GlobalTXScheduler(send, {channel: queue})
                await scheduler.start()
                try:
                    results = await asyncio.wait_for(asyncio.gather(retained, newest), 1)
                    self.assertTrue(all(result['ok'] for result in results))
                    self.assertEqual(sent, [frame(payload=b'first'), frame(payload=b'last')])
                    self.assertEqual(queue.stats['dropped_overflow'], 0)
                    self.assertEqual(queue.stats['pending'], 0)
                finally:
                    await scheduler.stop()

    async def test_retry_keeps_original_ttl(self):
        queue = self.queue(ttl_seconds=0.01)
        async def send(*args, **kwargs):
            await asyncio.sleep(0.02)
            return {"ok": False, "tx_result": "dropped"}
        backend = AsyncMock(side_effect=send)
        scheduler = tx.GlobalTXScheduler(backend, {queue.channel_id: queue})
        await scheduler.start()
        try:
            result = await asyncio.wait_for(queue.enqueue(b"test"), 1)
            self.assertEqual(result["error"], "ttl_expired")
            backend.assert_awaited_once()
        finally:
            await scheduler.stop()

    async def test_bad_backend_result_does_not_kill_scheduler(self):
        queue = self.queue()
        send_times = []
        async def send(*args, **kwargs):
            send_times.append(asyncio.get_running_loop().time())
            return None if len(send_times) == 1 else {"ok": True}
        scheduler = tx.GlobalTXScheduler(send, {queue.channel_id: queue}, inter_packet_delay_ms=15)
        await scheduler.start()
        try:
            first = await asyncio.wait_for(queue.enqueue(b"first"), 1)
            self.assertEqual(first["error"], "invalid_send_result")
            self.assertTrue((await asyncio.wait_for(queue.enqueue(b"second"), 1))["ok"])
            self.assertGreaterEqual(send_times[1] - send_times[0], 0.015)
        finally:
            await scheduler.stop()


class BridgeResultTests(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_drains_accepted_batch_without_blocking_loop(self):
        bridge = bridge_engine.BridgeEngine([])
        entered, release = threading.Event(), threading.Event()
        batches, writer_threads, closed_threads = [], [], []

        def store(events):
            writer_threads.append(threading.get_ident())
            entered.set()
            if not release.wait(2):
                raise RuntimeError("test did not release writer")
            batches.extend(events)

        bridge.set_sqlite_handler(SimpleNamespace(
            store_dedup_events_batch=store,
            close_thread_connection=lambda: closed_threads.append(threading.get_ident())))
        bridge._record_dedup_event("accepted", "channel_a", "first")
        self.assertTrue(await asyncio.to_thread(entered.wait, 1))
        bridge._record_dedup_event("accepted", "channel_b", "second")
        worker = bridge._sqlite_writer_thread
        # Stop also covers a startup failure before bridge.run was entered.
        bridge.stop()
        bridge._record_dedup_event("late", "channel_a", "rejected")
        callback = Mock()
        bridge.register_on_raw_rx(callback)
        bridge._fire_raw_rx_callbacks(frame(), "channel_a")
        waiter = asyncio.create_task(bridge.wait_closed())
        try:
            await asyncio.sleep(0)
            self.assertFalse(waiter.done())
            self.assertIs(bridge._sqlite_writer_thread, worker)
            # Cancellation cannot discard ownership of the unfinished writer.
            waiter.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiter
            self.assertIs(bridge._sqlite_writer_thread, worker)
        finally:
            release.set()
            await bridge.wait_closed()
        self.assertEqual([event["pkt_hash"] for event in batches], ["first", "second"])
        self.assertEqual(closed_threads, [worker.ident])
        self.assertEqual(set(writer_threads), {worker.ident})
        self.assertIsNone(bridge._sqlite_writer_thread)
        self.assertFalse(worker.is_alive())
        self.assertTrue(bridge._dedup_queue.empty())
        callback.assert_not_called()

    async def test_live_modes_distinguish_forwarding_and_local_transmissions(self):
        radio = SimpleNamespace(channel_id="channel_b", channel_config={}, send=AsyncMock(return_value={"ok": True}))
        bridge = bridge_engine.BridgeEngine([radio], rules=[
            {"source": source, "target": "channel_b"} for source in ("channel_a", "repeater")])
        engine = SimpleNamespace(config={"repeater": {"mode": "forward"}})
        bridge.set_repeater_engine(engine)
        for mode in ("forward", "monitor", "no_tx"):
            engine.config["repeater"]["mode"] = mode
            for source, origin in (("channel_a", None), ("repeater", "channel_a"), ("repeater", None)):
                with self.subTest(mode=mode, source=source, origin=origin):
                    radio.send.reset_mock()
                    sent = await bridge._forward_by_rules(source, frame(), packet_hash(frame()), "RAW_CUSTOM", origin_channel=origin)
                    allowed = mode == "forward" or (mode == "monitor" and source == "repeater" and origin is None)
                    self.assertEqual(sent, allowed)
                    self.assertEqual(radio.send.await_count, int(allowed))

    async def test_empty_rules_receive_locally_with_stable_sparse_channel_names(self):
        radios = [SimpleNamespace(channel_id="channel_" + cid, channel_config={}, send=AsyncMock()) for cid in "bd"]
        ui_path = Mock(exists=Mock(return_value=True), read_text=Mock(return_value=json.dumps({
            "channels": [{"name": "ui-" + cid, "friendly_name": "Radio " + cid.upper()} for cid in "abcd"]})))
        with patch.object(bridge_engine, "resolve_config_path", return_value=ui_path):
            bridge = bridge_engine.BridgeEngine(radios)
        self.assertEqual(bridge._resolve_channel("ui-b"), "channel_b")
        self.assertEqual(bridge._resolve_channel("ui-d"), "channel_d")
        self.assertEqual(bridge._dn("channel_d"), "ui-d")
        receiver = AsyncMock()
        bridge.set_repeater_handler(receiver)
        self.assertFalse(await bridge._forward_by_rules("channel_d", frame(), packet_hash(frame()), "RAW_CUSTOM", rssi=-80, snr=4))
        receiver.assert_awaited_once_with(frame(), origin_channel="channel_d", rssi=-80, snr=4)
        self.assertFalse(await bridge._forward_by_rules("repeater", frame(), packet_hash(frame()), "RAW_CUSTOM"))
        for radio in radios:
            radio.send.assert_not_awaited()
            radio.wait_for_rx = AsyncMock(side_effect=asyncio.Event().wait)
        task = asyncio.create_task(bridge.run())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        bridge.stop()
        await asyncio.wait_for(task, 0.2)
        self.assertFalse(bridge._running)

    async def test_radio_failure_does_not_count_or_emit_self_echo(self):
        target = SimpleNamespace(channel_id="channel_b", channel_config={}, send=AsyncMock(return_value={"ok": False}))
        bridge = bridge_engine.BridgeEngine([target], rules=[{"source": "channel_a", "target": "channel_b"}])
        bridge._fire_raw_rx_callbacks = Mock()
        await bridge._forward_by_rules("channel_a", frame(), packet_hash(frame()), "RAW_CUSTOM")
        self.assertEqual(bridge.forwarded_packets, 0)
        self.assertFalse(bridge._tx_echo_hashes)
        bridge._fire_raw_rx_callbacks.assert_not_called()

    async def test_upstream_radio_none_failure_and_empty_metadata_success(self):
        target = SimpleNamespace(channel_id="channel_b", channel_config={}, send=AsyncMock(side_effect=[None, {}]))
        bridge = bridge_engine.BridgeEngine([target], rules=[{"source": "repeater", "target": "channel_b"}])
        self.assertFalse(await bridge.inject_packet("repeater", frame()))
        bridge._running = True
        self.assertFalse(await bridge.inject_packet("repeater", frame()))
        self.assertTrue(await bridge.inject_packet("repeater", frame()))
        self.assertEqual(bridge.forwarded_packets, 1)

    async def test_legacy_endpoint_none_succeeds_and_explicit_false_fails(self):
        bridge = bridge_engine.BridgeEngine([], rules=[{"source": "channel_a", "target": "repeater"}])
        bridge._endpoint_handlers["repeater"] = AsyncMock(side_effect=[False, None])
        self.assertFalse(await bridge._forward_by_rules("channel_a", frame(), packet_hash(frame()), "RAW_CUSTOM"))
        self.assertTrue(await bridge._forward_by_rules("channel_a", frame(), packet_hash(frame()), "RAW_CUSTOM"))

    async def test_endpoint_success_and_failure_propagate_to_bridge(self):
        for channel, plugin_class in (("channel_e", ChannelEBridge), ("channel_f", ChannelFBridge)):
            for ok in (False, True):
                for queued in (False, True):
                    with self.subTest(channel=channel, ok=ok, queued=queued):
                        result = {"ok": ok, "error": "blocked"}
                        backend = SimpleNamespace(send=AsyncMock(return_value=result))
                        if queued:
                            backend._tx_queue_manager = SimpleNamespace(queues={channel: SimpleNamespace(enqueue=AsyncMock(return_value=result))})
                        bridge = bridge_engine.BridgeEngine([], rules=[{"source": "repeater", "target": channel}])
                        plugin = plugin_class(bridge, backend=backend)
                        bridge._endpoint_handlers[channel] = plugin._tx_handler
                        bridge._fire_raw_rx_callbacks = Mock()
                        await bridge._forward_by_rules("repeater", frame(), packet_hash(frame()), "RAW_CUSTOM")
                        self.assertEqual(bridge.forwarded_packets, int(ok))
                        self.assertEqual(plugin.tx_packets, int(ok))
                        self.assertEqual(plugin.tx_errors, int(not ok))
                        self.assertEqual(bridge._fire_raw_rx_callbacks.call_count, int(ok))

    async def test_missing_endpoint_backend_reports_failure(self):
        for plugin_class in (ChannelEBridge, ChannelFBridge):
            plugin = plugin_class(None)
            result = await plugin._tx_handler(frame())
            self.assertEqual(result, {"ok": False, "error": "no_backend"})
            self.assertEqual(plugin.tx_errors, 1)


class VirtualRadioTests(unittest.IsolatedAsyncioTestCase):
    async def test_early_rx_preserves_per_packet_signal_metadata(self):
        radio = virtual.VirtualLoRaRadio(SimpleNamespace(register_virtual_radio=Mock()), "channel_a", {})
        # Receive before any loop is registered, as during daemon startup.
        await asyncio.to_thread(radio.enqueue_rx, b"first", -110, -5.5)
        await asyncio.to_thread(radio.enqueue_rx, b"second", -80, 7.0)
        radio.begin()
        self.assertEqual(await asyncio.wait_for(radio.wait_for_rx(), 1), b"first")
        self.assertEqual((radio.get_last_rssi(), radio.get_last_snr()), (-110, -5.5))
        self.assertEqual(await asyncio.wait_for(radio.wait_for_rx(), 1), b"second")
        self.assertEqual((radio.get_last_rssi(), radio.get_last_snr()), (-80, 7.0))

    async def test_backend_thread_callback_executes_on_event_loop(self):
        radio = virtual.VirtualLoRaRadio(SimpleNamespace(register_virtual_radio=Mock()), "channel_a", {})
        radio.begin()
        called = asyncio.Event()
        callback_threads = []
        def callback(data):
            callback_threads.append(threading.get_ident())
            called.set()
        radio.set_rx_callback(callback)
        await asyncio.to_thread(radio.enqueue_rx, b"test", -100, 2.5)
        await asyncio.wait_for(called.wait(), 1)
        self.assertEqual(callback_threads, [threading.get_ident()])

    async def test_send_honors_explicit_power(self):
        backend = SimpleNamespace(register_virtual_radio=Mock(), send=AsyncMock(return_value={"ok": True}))
        radio = virtual.VirtualLoRaRadio(backend, "channel_a", {"tx_power": 14})
        await radio.send(b"test", tx_power=10)
        backend.send.assert_awaited_once_with("channel_a", b"test", tx_power=10, trace_hash=None)

    async def test_dispatcher_callback_receives_per_packet_metadata(self):
        radio = virtual.VirtualLoRaRadio(SimpleNamespace(register_virtual_radio=Mock()), "channel_a", {})
        radio.begin()
        seen = []
        def callback(data, rssi=None, snr=None):
            seen.append((data, rssi, snr))
        radio.set_rx_callback(callback)
        radio.enqueue_rx(b"first", -110, -5)
        radio.enqueue_rx(b"second", -90, 3)
        await asyncio.sleep(0)
        self.assertEqual(seen, [(b"first", -110, -5), (b"second", -90, 3)])

    async def test_legacy_async_callback_getters_keep_own_packet_metadata(self):
        radio = virtual.VirtualLoRaRadio(SimpleNamespace(register_virtual_radio=Mock()), "channel_a", {})
        radio.begin()
        seen = []
        done = asyncio.Event()
        async def callback(data):
            await asyncio.sleep(0)
            seen.append((data, radio.get_last_rssi(), radio.get_last_snr()))
            if len(seen) == 2:
                done.set()
        radio.set_rx_callback(callback)
        radio.enqueue_rx(b"first", -110, -5)
        radio.enqueue_rx(b"second", -90, 3)
        await asyncio.wait_for(done.wait(), 1)
        self.assertEqual(seen, [(b"first", -110, -5), (b"second", -90, 3)])
        self.assertEqual(await radio.wait_for_rx(), b"first")
        self.assertEqual(radio.get_last_rssi(), -110)

    async def test_failed_send_matches_dispatcher_none_contract(self):
        failure = {"ok": False, "error": "blocked"}
        backend = SimpleNamespace(register_virtual_radio=Mock(), send=AsyncMock(return_value=failure))
        radio = virtual.VirtualLoRaRadio(backend, "channel_a", {})
        self.assertIsNone(await radio.send(b"test"))
        self.assertEqual(radio._last_tx_metadata, failure)


class BackendHelperTests(unittest.TestCase):
    def test_saved_channel_positions_and_empty_config_match_hal(self):
        ui = json.loads((ROOT / 'config/wm1303_ui.json').read_text())
        backend = backend_module.WM1303Backend.__new__(backend_module.WM1303Backend)
        backend.virtual_radios, backend.channels = {}, {}
        backend.config = {'wm1303': {'channels': {'channel_a': {'frequency': 869000000}}}}
        ui_path = Mock()
        ui_path.exists.return_value = True
        ui_path.read_text.side_effect = lambda: json.dumps(ui)
        with patch.object(backend_module, 'resolve_config_path', return_value=ui_path), \
                patch.dict(sys.modules, {'wm1303_test_hardware.virtual_radio': virtual}):
            self.assertEqual(backend.get_radios(), [])  # never resurrect legacy YAML channels
            conf = backend_module._generate_bridge_conf(backend.config['wm1303']['channels'], ui)
            self.assertFalse(conf['SX130x_conf']['chan_multiSF_0']['enable'])
            self.assertFalse(conf['SX130x_conf']['sx1261_conf']['lora_rx']['enable'])
            ui['channels'] = [dict(active=False, frequency=869000000),
                              dict(active=True, frequency=869000000, name='B', bandwidth=125000)]
            radios = backend.get_radios()
            self.assertEqual([r.channel_id for r in radios], ['channel_b'])
            conf = backend_module._generate_bridge_conf(backend.channels, ui)['SX130x_conf']
            self.assertFalse(conf['chan_multiSF_0']['enable'])
            self.assertTrue(conf['chan_multiSF_1']['enable'])

    def test_shared_radio_settings_and_unsupported_combinations(self):
        import copy
        ui = {'channels': [dict(active=True, frequency=869000000, bandwidth=125000)],
              'channel_e': dict(enabled=False, frequency=0, lbt_enabled=True),
              'channel_f': dict(enabled=True, frequency=869525000, bandwidth=250000,
                                spreading_factor=9, lbt_enabled=True),
              'spi_devices': {'sx1302_spi_path': '/dev/spidev1.0', 'sx1261_spi_path': '/dev/spidev1.1'},
              'sync_word': {'value': 0x3444}}
        original = copy.deepcopy(ui)
        conf = backend_module._generate_bridge_conf({}, ui)['SX130x_conf']
        self.assertEqual(ui, original)
        self.assertTrue(conf['lorawan_public'])
        self.assertEqual(conf['com_path'], '/dev/spidev1.0')
        self.assertEqual(conf['sx1261_conf']['spi_path'], '/dev/spidev1.1')
        self.assertEqual(conf['sx1261_conf']['lora_rx']['sync_word'], 0x3444)
        self.assertEqual({c['freq_hz'] for c in conf['sx1261_conf']['lbt']['channels']},
                         {869000000, 869525000})
        ui['channel_e'] = dict(enabled=True, frequency=869618000, bandwidth=62500,
                               spreading_factor=10, coding_rate=8)
        self.assertEqual(backend_module._generate_bridge_conf({}, ui)['SX130x_conf']
                         ['sx1261_conf']['lora_rx']['coding_rate'], 4)
        for section, key, value in (('channel_f', 'frequency', 915000000),
                                    ('channel_f', 'bandwidth', 500000),
                                    ('channel_e', 'spreading_factor', 5)):
            candidate = copy.deepcopy(ui)
            candidate[section][key] = value
            with self.subTest(section=section, key=key), self.assertRaises(ValueError):
                backend_module._generate_bridge_conf({}, candidate)

    def test_receive_datarate_is_exact_and_rejects_bad_values(self):
        self.assertEqual(backend_module._parse_datr("SF8BW62"), (8, 62500))
        self.assertEqual(backend_module._parse_datr("SF8BW62.5"), (8, 62500))
        self.assertEqual(backend_module._parse_datr("SF12BW250"), (12, 250000))
        for data in (50000, None, "SF8BW125junk", "SF13BW125", "SF8BW31"):
            self.assertEqual(backend_module._parse_datr(data), (None, None))

    def test_fallback_tx_uses_same_encoding_as_scheduler(self):
        backend = backend_module.WM1303Backend.__new__(backend_module.WM1303Backend)
        config = {"frequency": 869618000, "spreading_factor": 8,
                  "bandwidth": 62500, "coding_rate": 1}
        fallback = backend._build_txpk(config, b"test")
        queued = tx.ChannelTXQueue("channel_e", 869618000, 62.5, 8, 1).build_txpk(b"test")
        self.assertEqual(fallback, queued)
        self.assertAlmostEqual(backend._lora_airtime_s(10, 62500, 60), 1.789952)

    def test_failed_database_setup_closes_partial_connection(self):
        connection = Mock()
        connection.execute.side_effect = OSError("read-only")
        shared = backend_module._SharedConn("unused-test-database")
        with patch.object(backend_module.sqlite3, "connect", return_value=connection):
            with self.assertRaises(OSError):
                shared._ensure_conn()
        connection.close.assert_called_once_with()
        self.assertIsNone(shared._conn)


class BackendLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def backend(self):
        backend = backend_module.WM1303Backend({})
        backend._loop = asyncio.get_running_loop()
        backend._running = True
        backend._pull_addr = ('127.0.0.1', 9999)
        backend._sock = Mock()
        return backend

    async def test_startup_handshake_cleanup_and_scheduler_ownership(self):
        original_thread = threading.Thread
        ui = {'channels': [], 'channel_e': {'enabled': True},
              'adv_config': {'tx_packet_ttl_seconds': 7, 'tx_overflow_policy': 'drop_newest'}}
        config_path = Mock(exists=Mock(return_value=True), read_text=Mock(return_value=json.dumps(ui)))
        for failure in (None, 'bind', 'spawn'):
            with self.subTest(failure=failure):
                # A changing desired file cannot split HAL and queue settings.
                config_path.read_text.reset_mock()
                config_path.read_text.side_effect = [json.dumps(ui), ValueError('file changed')]
                backend = backend_module.WM1303Backend({
                    'storage': {'storage_dir': '/tmp/owned-repeater-storage'},
                    'wm1303': {'tx_queue': {'queue_size': 2, 'tx_delay_ms': 10}}})
                self.assertEqual(backend._db_path, '/tmp/owned-repeater-storage/repeater.db')
                sock = Mock()
                if failure == 'bind':
                    sock.bind.side_effect = OSError('address in use')
                process = Mock()
                process.pid = 543210
                process.poll.return_value = None
                started = []
                def thread(*args, target=None, **kwargs):
                    worker = Mock()
                    worker.is_alive.return_value = False
                    def start():
                        started.append(target.__name__)
                        worker.is_alive.return_value = True
                    worker.start.side_effect = start
                    worker.join.side_effect = lambda **kwargs: setattr(
                        worker.is_alive, 'return_value', False)
                    return worker
                def spawn(*args, **kwargs):
                    self.assertIn('_udp_loop', started)
                    self.assertEqual(args[0][-1], str(backend_module.ACTIVE_BRIDGE_CONF))
                    self.assertEqual(args[0][:2], ['sudo', '-n'])
                    self.assertTrue(kwargs['start_new_session'])
                    self.assertEqual(kwargs['stdin'], subprocess.DEVNULL)
                    if failure == 'spawn':
                        raise OSError('missing executable')
                    backend._handle_udp(bytes([2, 0, 1, backend_module.PKT_PULL_DATA]) + bytes(8),
                                        ('127.0.0.1', 9999))
                    return process
                def ready(**kwargs):
                    self.assertIn('_pktfwd_stdout_reader', started)
                    self.assertTrue(backend._pktfwd_ready_event.is_set())
                    return True
                with (patch.object(backend, 'get_radios'),
                      patch.object(backend, '_write_pktfwd_config'),
                      patch.object(backend, '_ensure_active_pktfwd_config'),
                      patch.object(backend, '_init_sx1261_lbt'),
                      patch.object(backend, '_write_cad_config_json'),
                      patch.object(backend, '_start_hourly_timer'),
                      patch.object(backend, '_start_noise_floor_monitor'),
                      patch.object(backend, '_signal_pktfwd_group'),
                      patch.object(backend._pktfwd_ready_event, 'wait', side_effect=ready),
                      patch.object(backend_module, 'UI_JSON_PATH', config_path),
                      patch.object(backend_module, 'resolve_config_path', return_value=config_path),
                      patch.object(backend_module.socket, 'socket', return_value=sock),
                      patch.object(backend_module.threading, 'Thread', side_effect=thread),
                      patch.object(backend_module.subprocess, 'Popen', side_effect=spawn),
                      patch.object(backend_module.subprocess, 'run', return_value=SimpleNamespace(stdout=b'', returncode=0)),
                      patch.object(backend_module.time, 'sleep')):
                    if failure:
                        with self.assertRaises(OSError):
                            backend.begin()
                    else:
                        self.assertTrue(backend.begin())
                        await backend.ensure_tx_queues_started()
                        scheduler = backend._global_tx_scheduler
                        await backend.ensure_tx_queues_started()
                        self.assertIs(backend._global_tx_scheduler, scheduler)
                        queue = backend._tx_queue_manager.queues['channel_e']
                        self.assertEqual((queue._queue_size, queue.ttl_seconds, queue.overflow_policy),
                                         (2, 7, 'drop_newest'))
                        self.assertEqual(scheduler._inter_packet_delay_s, 0.01)
                        producers = [backend._thread, backend._watchdog_thread,
                                     backend._snapshot_thread]
                        # Match the daemon's off-loop stop contract; only the
                        # executor thread is real, hardware producers stay mocked.
                        with patch.object(backend_module.threading, 'Thread', original_thread):
                            await asyncio.to_thread(backend.stop)
                        for producer in producers:
                            producer.join.assert_called_once_with()
                    await asyncio.sleep(0)
                    await asyncio.sleep(0)
                    self.assertFalse(backend._running)
                    self.assertIsNone(backend._sock)
                    self.assertIsNone(backend._proc)
                    sock.close.assert_called_once()
                    self.assertFalse(backend._recreate_socket())
                    self.assertIsNone(await backend.send(frame()))
                    self.assertEqual(config_path.read_text.call_count, 1)

    async def test_runtime_config_recovers_without_applying_desired_changes(self):
        backend = self.backend()
        ui = {'channels': [], 'channel_e': {'enabled': True, 'frequency': 869618000,
                                          'bandwidth': 62500, 'spreading_factor': 8}}
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            active, desired = directory / 'active.json', directory / 'desired.json'
            with (patch.object(backend_module, 'ACTIVE_BRIDGE_CONF', active),
                  patch.object(backend_module, 'BRIDGE_CONF', desired),
                  patch.object(backend_module, 'PKTFWD_DIR', directory)):
                backend._write_pktfwd_config(ui_config=ui)
                expected = active.read_text()
                desired.write_text('{"saved": "not yet active"}')
                for damage in (None, '{incomplete', '{}'):
                    if damage is None:
                        active.unlink()
                    else:
                        active.write_text(damage)
                    def stopped():
                        self.assertEqual(active.read_text(), expected)
                    with (patch.object(backend, '_stop_pktfwd_process', side_effect=stopped) as stop,
                          patch.object(backend, '_start_pktfwd') as start,
                          patch.object(backend, '_load_radio_settings', side_effect=AssertionError('must not apply desired'))):
                        backend._restart_pkt_fwd()
                        stop.assert_called_once()
                        start.assert_called_once()
                    self.assertEqual(desired.read_text(), '{"saved": "not yet active"}')
                active.unlink()
                with (patch.object(backend, '_write_pktfwd_file', side_effect=OSError('read-only')),
                      patch.object(backend, '_stop_pktfwd_process') as stop,
                      patch.object(backend_module.subprocess, 'run') as reset):
                    with self.assertRaises(OSError):
                        backend._restart_pkt_fwd()
                    backend._do_watchdog_restart('fixture')
                    stop.assert_not_called()
                    reset.assert_not_called()

    async def test_owned_process_group_stops_child_and_preserves_failed_cleanup(self):
        # Harmless sudo-like wrapper: ignores TERM, but reaps its child. The
        # child resets inherited SIG_IGN so group signalling can stop it.
        child_code = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_DFL); print('ready',flush=True); time.sleep(30)"
        wrapper_code = ("import signal,subprocess,sys; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                        f"child=subprocess.Popen([sys.executable,'-c',{child_code!r}]); "
                        "print('pid='+str(child.pid),flush=True); child.wait()")
        process = subprocess.Popen([sys.executable, '-c', wrapper_code], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, text=True, start_new_session=True)
        backend = self.backend()
        backend._proc, backend._pktfwd_pgid = process, process.pid
        try:
            lines = [await asyncio.wait_for(asyncio.to_thread(process.stdout.readline), 2) for _ in range(2)]
            child_pid = int(next(line[4:] for line in lines if line.startswith('pid=')))
            self.assertIn('ready\n', lines)
            with patch.object(backend_module.os, 'geteuid', return_value=0):
                await asyncio.to_thread(backend._stop_pktfwd_process)
            self.assertEqual(process.returncode, 0)  # wrapper reaped the stopped child
            self.assertFalse(Path(f'/proc/{child_pid}').exists())
            self.assertIsNone(backend._proc)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=2)
            process.stdout.close()

        # Privileged signalling is mocked: never invoke sudo in this suite.
        owned = Mock(pid=543210)
        backend._proc, backend._pktfwd_pgid = owned, owned.pid
        with (patch.object(backend_module.os, 'geteuid', return_value=1000),
              patch.object(backend_module.os, 'killpg', side_effect=PermissionError),
              patch.object(backend_module.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as privileged):
            backend._signal_pktfwd_group(owned, signal.SIGKILL)
            self.assertEqual(privileged.call_args.args[0],
                             ['sudo', '-n', '/bin/kill', '-9', '--', '-543210'])
            privileged.return_value.returncode = 1
            with self.assertRaises(RuntimeError):
                backend._stop_pktfwd_process()
            self.assertIs(backend._proc, owned)
        with (patch.object(backend, '_signal_pktfwd_group') as send_signal,
              patch.object(backend, '_ensure_active_pktfwd_config'),
              patch.object(backend, '_start_pktfwd') as start):
            owned.wait.side_effect = subprocess.TimeoutExpired('owned', 3)
            with self.assertRaises(subprocess.TimeoutExpired):
                backend._restart_pkt_fwd()
            self.assertEqual([call.args[1] for call in send_signal.call_args_list],
                             [signal.SIGTERM, signal.SIGKILL])
            self.assertIs(backend._proc, owned)
            start.assert_not_called()

    async def test_bridge_scheduler_udp_ack_outcome_controls_success_and_echoes(self):
        outcomes = [
            ({'error': 'NONE', 'phase': 'post_tx', 'tx_result': 'blocked',
              'lbt': {'enabled': True, 'pass': False}}, False),
            ({'error': 'NONE', 'phase': 'post_tx', 'tx_result': 'send_failed',
              'lbt': {'enabled': True, 'pass': None, 'rssi_dbm': None}}, False),
            ({'error': 'NONE', 'phase': 'post_tx', 'tx_result': 'send_failed',
              'lbt': {'enabled': True}}, False),
            ({'error': 'NONE', 'phase': 'post_tx', 'tx_result': 'blocked',
              'cad': {'enabled': True, 'detected': True},
              'lbt': {'enabled': True, 'pass': None}}, False),
            ({'error': 'TX_FREQ'}, False),
            ({'error': 'NONE', 'phase': 'post_tx', 'tx_result': 'sent'}, True),
            ({'error': 'NONE', 'phase': 'post_tx', 'tx_result': 'sent',
              'lbt': {'enabled': True, 'pass': True, 'rssi_dbm': -90}}, True),
        ]
        for ack, expected in outcomes:
            with self.subTest(ack=ack):
                backend = self.backend()
                manager = tx.TXQueueManager()
                manager.add_channel('channel_e', 869618000, bw_khz=62.5, sf=10, cr=8)
                backend._tx_queue_manager = manager
                scheduler = tx.GlobalTXScheduler(backend._send_for_scheduler, manager.queues)
                backend._global_tx_scheduler = scheduler
                bridge = bridge_engine.BridgeEngine([], rules=[{'source': 'repeater', 'target': 'channel_e'}])
                bridge._running = True
                endpoint = ChannelEBridge(bridge, backend=backend)
                bridge._endpoint_handlers['channel_e'] = endpoint._tx_handler
                packet = frame()
                def sendto(datagram, address):
                    self.assertEqual(address, backend._pull_addr)
                    self.assertNotIn(packet_hash(packet), backend._tx_echo_hashes)
                    txpk = json.loads(datagram[4:])['txpk']
                    self.assertEqual((txpk['datr'], txpk['codr']), ('SF10BW62', '4/8'))
                    wire_ack = bytes([2, datagram[1], datagram[2], backend_module.PKT_TX_ACK]) + bytes(8)
                    wire_ack += json.dumps({'txpk_ack': ack}).encode()
                    backend._loop.call_soon(backend._handle_udp, wire_ack, address)
                backend._sock.sendto.side_effect = sendto
                await scheduler.start()
                try:
                    result = await asyncio.wait_for(bridge.inject_packet('repeater', packet), 1)
                    self.assertEqual(result, expected)
                    self.assertEqual(bridge.forwarded_packets, int(expected))
                    self.assertEqual(manager.queues['channel_e'].stats['total_sent'], int(expected))
                    lbt = ack.get('lbt', {})
                    stats = manager.queues['channel_e'].stats
                    self.assertEqual(stats['lbt_passed'], int(lbt.get('pass') is True))
                    self.assertEqual(stats['lbt_blocked'], int(lbt.get('pass') is False))
                    # E's direct queue submission must count once. Returning
                    # that same result through send() must not double-count it.
                    parsed_ack = next(iter(backend._tx_ack_cache.values()))[1]
                    with patch.object(manager, 'enqueue', AsyncMock(return_value=parsed_ack)):
                        await backend.send('channel_e', packet)
                    confirmed_block = (ack.get('tx_result') == 'blocked' and
                                       (lbt.get('pass') is False or ack.get('cad', {}).get('enabled', False)))
                    self.assertEqual(backend._hourly_tx_lbt_block, int(confirmed_block))
                    self.assertEqual(backend._hourly_tx_fail, int(not expected and not confirmed_block))
                    self.assertEqual(backend._hourly_tx_ok, int(expected))
                    self.assertEqual(backend._tx_packets_sent_total, int(expected))
                    if lbt.get('enabled') and lbt.get('pass') is None:
                        with patch.object(bridge_engine, '_trace') as trace:
                            bridge_engine.emit_lbt_cad_trace_steps('fixture', 'channel_e', {
                                'lbt_enabled': True, 'lbt_pass': None})
                        self.assertIn('UNAVAILABLE', trace.call_args.kwargs['detail'])
                        self.assertEqual(trace.call_args.kwargs['status'], 'partial')
                    self.assertEqual(packet_hash(packet) in backend._tx_echo_hashes, expected)
                    self.assertFalse(backend._pending_tx_acks)
                    backend._sock.sendto.assert_called_once()
                finally:
                    await scheduler.stop()

    async def test_transport_failures_and_cancellation_leave_no_ack_or_echo_state(self):
        for failure in ('socket', 'timeout', 'cancel'):
            with self.subTest(failure=failure):
                backend = self.backend()
                backend.channels = {'channel_a': {'frequency': 869618000, 'bandwidth': 62500}}
                if failure == 'socket':
                    backend._sock.sendto.side_effect = OSError('disconnected')
                    backend._recreate_socket = Mock()
                    result = await backend.send(frame())
                    self.assertIsNone(result)  # OpenHop Dispatcher contract
                elif failure == 'timeout':
                    with patch.object(backend_module.asyncio, 'wait_for', side_effect=asyncio.TimeoutError):
                        result = await backend.send(frame())
                    self.assertIsNone(result)
                else:
                    sent = asyncio.Event()
                    backend._sock.sendto.side_effect = lambda *args: sent.set()
                    request = asyncio.create_task(backend.send(frame()))
                    await asyncio.wait_for(sent.wait(), 1)
                    request.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await request
                self.assertFalse(backend._pending_tx_acks)
                self.assertFalse(backend._tx_echo_hashes)
                self.assertFalse(backend._tx_lock.locked())

    async def test_rx_demultiplexes_e_f_and_multisf_by_complete_radio_settings(self):
        backend = self.backend()
        # Disabled A must not suppress enabled B at the same frequency, or
        # move B's settings into A's list position. Disabled E/F TX stays off.
        ui = {
            'channels': [
                {'name': 'off-A', 'active': False, 'frequency': 869525000, 'spreading_factor': 8},
                {'name': 'on-B', 'active': True, 'frequency': 869525000, 'spreading_factor': 9,
                 'lbt_enabled': True, 'lbt_threshold': -91},
            ],
            'channel_e': {'enabled': False, 'tx_enabled': True, 'coding_rate': 8,
                          'frequency': 869618000},
            'channel_f': {'enabled': True, 'tx_enabled': False, 'frequency': 869525000,
                          'bandwidth': 250000, 'spreading_factor': 9,
                          'lbt_enabled': True, 'lbt_threshold': -92},
        }
        backend.channels = {'channel_b': {'frequency': 868000000}}
        config_path = Mock()
        config_path.exists.return_value = True
        config_path.read_text.side_effect = lambda: json.dumps(ui)
        with patch.object(backend_module, 'resolve_config_path', return_value=config_path):
            backend._init_tx_queues()
            self.assertEqual(list(backend._tx_queue_manager.queues), ['channel_b'])
            self.assertEqual(backend._tx_queue_manager.queues['channel_b'].sf, 9)
            self.assertEqual(backend._tx_queue_manager.queues['channel_b'].freq_hz, 869525000)
            ui['channel_e'].update(enabled=True, tx_enabled=False)
            backend._init_tx_queues()
            self.assertEqual(list(backend._tx_queue_manager.queues), ['channel_b'])
            ui['channel_e']['tx_enabled'] = True
            ui['channel_f']['tx_enabled'] = True
            backend._init_tx_queues()
            self.assertEqual(list(backend._tx_queue_manager.queues), ['channel_b', 'channel_e', 'channel_f'])
            self.assertEqual(backend._tx_queue_manager.queues['channel_e'].cr, 8)
            # A Save must not retune routing before the HAL is restarted.
            ui['channel_e']['frequency'] = 868000000
            ui['channel_f']['frequency'] = 868000000
            ui['channels'][1]['lbt_threshold'] = -10
            self.assertEqual(backend._load_channel_e_cache(), 869618000)
            self.assertEqual(backend._load_channel_f_cache(), (True, 869525000, 250000, 9))
            self.assertEqual(backend._get_channel_lbt_config('channel_b'),
                             {'lbt_enabled': True, 'lbt_rssi_target': -91})
            self.assertEqual(backend._get_channel_lbt_config('channel_f'),
                             {'lbt_enabled': True, 'lbt_rssi_target': -92})
            manager = backend._tx_queue_manager
            for invalid in ('{incomplete', '[]', '{"channel_e":null}'):
                config_path.read_text.side_effect = lambda value=invalid: value
                with self.assertRaises(ValueError):
                    backend._init_tx_queues()
                self.assertIs(backend._tx_queue_manager, manager)
        legacy = self.backend()
        legacy.channels = {'channel_b': {'frequency': 869525000, 'tx_enable': False}}
        missing_path = Mock(exists=Mock(return_value=False))
        with patch.object(backend_module, 'resolve_config_path', return_value=missing_path):
            legacy._init_tx_queues()
        self.assertFalse(legacy._tx_queue_manager.queues)  # Preserve YAML TX disable when SSOT is absent.
        backend._update_rx_stats = Mock()
        e_cfg = {'enabled': True, 'frequency': 869618000, 'bandwidth': 62500, 'spreading_factor': 8}
        backend._channel_e_config_cache = e_cfg
        backend._load_channel_e_cache = Mock(return_value=e_cfg['frequency'])
        backend._load_channel_f_cache = Mock(return_value=(True, 869525000, 250000, 9))
        bridge = SimpleNamespace(inject_packet=AsyncMock(return_value=True))
        e, f = ChannelEBridge(bridge, backend=backend), ChannelFBridge(bridge, backend=backend)
        e._loop = f._loop = backend._loop
        e._running = f._running = True
        backend._channel_e_rx_callback = e._rx_from_backend
        backend._channel_f_rx_callback = f._rx_from_backend
        a = virtual.VirtualLoRaRadio(backend, 'channel_a', {
            'frequency': 869525000, 'bandwidth': 125000, 'spreading_factor': 9})
        a.begin()
        receptions = [
            (869.525, 'SF9BW250', b'for F'),
            (869.618, 'SF8BW62', b'for E'),
            (869.525, 'SF9BW125', b'for A'),
        ]
        for frequency, datarate, payload in receptions:
            packet = frame(payload=payload)
            backend._dispatch_rx({'freq': frequency, 'datr': datarate, 'stat': 1,
                                  'rssi': -105, 'lsnr': -2.5,
                                  'data': base64.b64encode(packet).decode()})
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertEqual(await asyncio.wait_for(a.wait_for_rx(), 1), frame(payload=b'for A'))
        self.assertEqual([call.args[0] for call in bridge.inject_packet.await_args_list], ['channel_f', 'channel_e'])
        self.assertEqual(a._rx_queue.qsize(), 0)
        bridge._endpoint_handlers = {}
        bridge.inject_packet.reset_mock()
        async def waiting_receive(*args, **kwargs):
            await asyncio.Event().wait()
        with (patch.object(backend_module.socket, 'socket', return_value=Mock()) as socket_factory,
              patch.object(backend._loop, 'sock_recv', side_effect=waiting_receive)):
            tasks = [asyncio.create_task(endpoint.run()) for endpoint in (e, f)]
            await asyncio.sleep(0)
            self.assertEqual(set(bridge._endpoint_handlers), {'channel_e', 'channel_f'})
            # Backend callbacks create tasks independent of the listener.
            # Stopping the endpoint must cancel those queued forwards too.
            bridge.inject_packet.side_effect = waiting_receive
            for endpoint in (e, f):
                endpoint._schedule_rx(frame(), -90, 1)
            await asyncio.sleep(0)
            pending_injections = tuple(e._rx_tasks | f._rx_tasks)
            self.assertEqual(len(pending_injections), 2)
            for endpoint in (e, f):
                endpoint.stop()
            await asyncio.wait_for(asyncio.gather(*tasks), 0.2)
            self.assertTrue(all(task.cancelled() for task in pending_injections))
            self.assertFalse(e._rx_tasks | f._rx_tasks)
            self.assertFalse(bridge._endpoint_handlers)
            self.assertIsNone(backend._channel_e_rx_callback)
            self.assertIsNone(backend._channel_f_rx_callback)
            socket_factory.return_value.close.assert_called_once()
            bridge.inject_packet.reset_mock()
            e._schedule_rx(frame(), -90, 1)
            f._schedule_rx(frame(), -90, 1)
            await asyncio.sleep(0)
            bridge.inject_packet.assert_not_awaited()

    def test_failed_database_open_releases_lock_for_other_threads(self):
        connection = backend_module._SharedConn("unused-test-database")
        with patch.object(connection, "_ensure_conn", side_effect=OSError("open failed")):
            with self.assertRaises(OSError):
                with connection:
                    self.fail("must not enter after open failure")
        acquired = []
        def check_lock():
            success = connection._lock.acquire(timeout=0.1)
            acquired.append(success)
            if success:
                connection._lock.release()
        thread = threading.Thread(target=check_lock)
        thread.start()
        thread.join(timeout=1)
        self.assertEqual(acquired, [True])


class RegionTests(unittest.TestCase):
    def test_unknown_region_derives_calibration_from_frequency(self):
        self.assertEqual(regions.get_sx1261_calib("NEW_REGION", 915000000), (0xE1, 0xE9))

    def test_region_lookup_cannot_mutate_defaults(self):
        result = regions.get_region("EU868")
        result["tx_freq_min"] = 1
        self.assertEqual(regions.get_region("EU868")["tx_freq_min"], 863000000)

    def test_custom_bounds_reject_inversion(self):
        with self.assertRaises(ValueError):
            regions.get_tx_bounds("CUSTOM", 870000000, 860000000)


if __name__ == "__main__":
    unittest.main()
