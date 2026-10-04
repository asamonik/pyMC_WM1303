"""Policies gate original RX before local handling and all bridge destinations."""

from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from test_engine_protocol import PacketFixture, engine, main_module, router_module
from test_packet_policies import PolicyService, load_module
from test_radio_bridge import bridge_engine, frame


runtime = load_module("policy_runtime")
evaluate_received_policy, received_policy_scope = runtime.evaluate_received_policy, runtime.received_policy_scope
try:
    from openhop_core.protocol.packet import Packet
    from openhop_core.protocol.packet_utils import PacketHeaderUtils
    REAL_PACKET = True
except ImportError:
    REAL_PACKET = False

    class Packet(PacketFixture):
        @property
        def rssi(self):
            return getattr(self, "_rssi", 0)

        @property
        def snr(self):
            return getattr(self, "_snr", 0)

        def get_path_hashes_hex(self):
            width = self.get_path_hash_size()
            return [bytes(self.path[index:index + width]).hex() for index in range(0, len(self.path), width)]

    PacketHeaderUtils = SimpleNamespace(parse_header=lambda header: {"payload_type": (header >> 2) & 15,
                                                                    "route_type": header & 3})


def document(action="allow", rules=None, enabled=True):
    return {"policy_engine": {"enabled": enabled, "default_action": action,
                              "rules": rules or [], "objects": {}}}


def packet(kind=15, path=b"", route=1):
    result = Packet()
    if not result.read_from(frame(kind=kind, path=path, route=route)):
        raise AssertionError("invalid wire fixture")
    return result


class PolicyRoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.config = {"repeater": {"mode": "forward"}}
        self.service = PolicyService(self.config, str(Path(directory.name) / "config.yaml"))
        self.service.update(document())
        packet_module = ModuleType("openhop_core.protocol.packet")
        packet_module.Packet = Packet
        aliases = patch.dict(sys.modules, {"repeater.policy_runtime": runtime,
                                           packet_module.__name__: packet_module})
        aliases.start()
        self.addCleanup(aliases.stop)

    def router(self):
        handler = SimpleNamespace(rx_count=0, dropped_count=0, recv_direct_count=0,
                                  recv_flood_count=0, storage=object(), record_packet_only=Mock())
        daemon = SimpleNamespace(policy_service=self.service, config=self.config,
                                 repeater_handler=handler)
        result = router_module.PacketRouter(daemon)
        result._route_packet_unchecked = AsyncMock(return_value=False)
        return result

    async def test_drop_blocks_classic_helpers_ack_delivery_and_forwarding(self):
        self.service.update(document("drop"))
        router = self.router()
        for kind in (0, 2, 3, 4, 5, 7, 9, 10):
            with self.subTest(kind=kind):
                incoming = packet(kind=kind)
                incoming._rssi, incoming._snr = -110, -5
                self.assertTrue(await router._route_packet(incoming))
                self.assertTrue(incoming.drop_reason.startswith("Policy blocked packet"))
        router._route_packet_unchecked.assert_not_awaited()
        handler = router.daemon.repeater_handler
        self.assertEqual((handler.rx_count, handler.dropped_count, handler.recv_flood_count), (8, 8, 8))
        self.assertEqual(handler.record_packet_only.call_count, 8)
        metadata = handler.record_packet_only.call_args.args[1]
        self.assertEqual((metadata["rssi"], metadata["snr"]), (-110, -5))

    async def test_internal_replies_bypass_default_drop_even_inside_receive_scope(self):
        received = packet()
        receipt = evaluate_received_policy(self.service, received)
        self.service.update(document("drop"))
        router = self.router()
        reply = packet(kind=3)
        reply._injected_for_tx = True
        with received_policy_scope(receipt):
            self.assertFalse(await router._route_packet(reply))
        router._route_packet_unchecked.assert_awaited_once_with(reply, origin_channel=None)
        router.daemon.repeater_handler.record_packet_only.assert_not_called()
        # RF packets use that same flag to suppress double-forwarding, not policy.
        reply._tx_metadata = {"policy_rf_received": True}
        self.assertTrue(await router._route_packet(reply))
        self.assertEqual(router._route_packet_unchecked.await_count, 1)

    async def test_bridge_drop_blocks_radio_mqtt_repeater_and_extra_rf_channels(self):
        self.service.update(document("drop"))
        radio = SimpleNamespace(channel_id="channel_b", channel_config={}, send=AsyncMock())
        bridge = bridge_engine.BridgeEngine([radio], rules=[
            {"source": "channel_a", "target": target}
            for target in ("channel_b", "mqtt", "repeater", "channel_e", "channel_f")])
        bridge.policy_service = self.service
        destinations = {name: AsyncMock() for name in ("mqtt", "repeater", "channel_e", "channel_f")}
        bridge._endpoint_handlers.update(destinations)
        state = {}
        self.assertFalse(await bridge._forward_by_rules("channel_a", frame(), "abcdef12", "RAW",
                                                       rssi=-99, snr=-2, _policy_state=state))
        radio.send.assert_not_awaited()
        for destination in destinations.values():
            destination.assert_not_awaited()
        self.assertEqual(bridge.dropped_filtered, 1)
        self.assertIn("Policy blocked packet", state["drop_reason"])

    async def test_bridge_empty_rules_do_not_bypass_drop_but_log_only_reaches_local_handler(self):
        bridge = bridge_engine.BridgeEngine([])
        bridge.policy_service = self.service
        bridge._repeater_handler = AsyncMock()
        self.service.update(document("drop"))
        self.assertFalse(await bridge._forward_by_rules("channel_e", frame(), "hash", "RAW"))
        bridge._repeater_handler.assert_not_awaited()
        self.service.update(document("log_only"))
        self.assertFalse(await bridge._forward_by_rules("channel_e", frame(), "hash", "RAW", rssi=-96, snr=2))
        bridge._repeater_handler.assert_awaited_once_with(frame(), origin_channel="channel_e", rssi=-96, snr=2)

    async def test_original_context_and_one_evaluation_survive_nested_parsing(self):
        conditions = [{"field": field, "value": value} for field, value in (
            ("channel", "channel_e"), ("hop_count", 1), ("rssi", -112), ("snr", -4))]
        self.service.update(document("drop", [{"id": "original", "if": {"all": conditions},
                                                "then": {"action": "allow"}}]))
        router = self.router()
        observed = []

        async def consume(data, **metadata):
            clone = Packet()
            self.assertTrue(clone.read_from(data))
            # A freshly parsed Packet has zero-valued signal fields. The scoped
            # receipt must preserve the actual bridge RX sample and ingress.
            self.assertFalse(await router._route_packet(clone))
            receipt = evaluate_received_policy(self.service, clone, {"snr": 0})
            observed.append(receipt.decision.rule_id)

        bridge = bridge_engine.BridgeEngine([], rules=[{"source": "channel_e", "target": "repeater"}])
        bridge.policy_service = self.service
        bridge._endpoint_handlers["repeater"] = consume
        with patch.object(self.service, "evaluate_with_engine", wraps=self.service.evaluate_with_engine) as evaluate:
            self.assertTrue(await bridge._forward_by_rules("channel_e", frame(path=b"\xab"), "hash", "RAW",
                                                          rssi=-112, snr=-4))
            self.assertEqual(evaluate.call_count, 1)
        self.assertEqual(observed, ["original"])

    async def test_live_update_and_other_packet_never_reuse_a_stale_decision(self):
        first = packet()
        receipt = evaluate_received_policy(self.service, first)
        with received_policy_scope(receipt):
            same = packet()
            self.assertIs(evaluate_received_policy(self.service, same), receipt)
            other = packet(kind=3)
            self.assertIsNot(evaluate_received_policy(self.service, other), receipt)
            self.service.update(document("drop"))
            updated = evaluate_received_policy(self.service, same)
            self.assertEqual(updated.decision.action, "drop")
            self.assertNotEqual(updated.engine_id, receipt.engine_id)
        self.assertIsNot(evaluate_received_policy(self.service, first), updated)

    async def test_same_wire_bytes_at_a_different_ingress_are_evaluated_again(self):
        self.service.update(document("drop", [{"if": {"field": "channel", "value": "channel_e"},
                                                "then": {"action": "allow"}}]))
        incoming = packet()
        receipt = evaluate_received_policy(self.service, incoming, {"channel": "channel_e"})
        self.assertEqual(receipt.decision.action, "allow")
        with received_policy_scope(receipt):
            self.assertIs(evaluate_received_policy(self.service, packet()), receipt)
            changed_ingress = evaluate_received_policy(self.service, packet(), {"channel": "mqtt"})
            self.assertEqual(changed_ingress.decision.action, "drop")
            self.assertIsNot(changed_ingress, receipt)

    async def test_receipt_is_tagged_with_exact_engine_if_swap_occurs_after_evaluation(self):
        original = self.service.engine
        evaluate = self.service.evaluate_with_engine

        def swap_after_evaluation(incoming, context):
            result = evaluate(incoming, context)
            self.service.update(document("drop"))
            return result

        with patch.object(self.service, "evaluate_with_engine", side_effect=swap_after_evaluation):
            receipt = evaluate_received_policy(self.service, packet())
        self.assertEqual(receipt.engine_id, id(original))
        with received_policy_scope(receipt):
            self.assertEqual(evaluate_received_policy(self.service, packet()).decision.action, "drop")

    async def test_process_packet_drop_precedes_path_mutation_and_duplicate_cache(self):
        self.service.update(document("drop"))
        handler = engine.RepeaterHandler.__new__(engine.RepeaterHandler)
        handler.policy_service, handler.config = self.service, self.config
        handler.direct_forward, handler.flood_forward = Mock(), Mock()
        incoming = packet(path=b"\xab", route=2)
        original = incoming.write_to()
        self.assertIsNone(handler.process_packet(incoming))
        self.assertEqual(incoming.write_to(), original)
        handler.direct_forward.assert_not_called()
        handler.flood_forward.assert_not_called()

    async def test_repeater_source_is_internal_tx_not_incoming_policy(self):
        self.service.update(document("drop"))
        radio = SimpleNamespace(channel_id="channel_a", channel_config={}, send=AsyncMock(return_value={"ok": True}))
        bridge = bridge_engine.BridgeEngine([radio], rules=[{"source": "repeater", "target": "channel_a"}])
        bridge.policy_service = self.service
        self.assertTrue(await bridge._forward_by_rules("repeater", frame(), "hash", "RAW"))
        radio.send.assert_awaited_once()

    @unittest.skipUnless(REAL_PACKET, "real Packet __slots__ requires upstream core")
    async def test_real_slotted_packet_bridge_local_delivery_and_trace_origin(self):
        daemon = main_module.RepeaterDaemon.__new__(main_module.RepeaterDaemon)
        daemon.config, daemon.policy_service = self.config, self.service
        daemon.router = SimpleNamespace(_route_packet=AsyncMock(return_value=True))
        daemon.trace_helper = SimpleNamespace(process_trace_packet=AsyncMock())
        daemon.repeater_handler = SimpleNamespace(process_packet=Mock())
        daemon._shutdown_started = False
        daemon.bridge_engine = SimpleNamespace(inject_packet=AsyncMock(return_value=True))
        imports = dict(main_module._test_imports)
        imports["openhop_core.protocol.packet"] = sys.modules["openhop_core.protocol.packet"]
        with patch.dict(sys.modules, imports):
            await daemon._bridge_repeater_handler(frame(), "channel_e", -105, -2.5)
            local = daemon.router._route_packet.await_args.args[0]
            self.assertEqual((local.rssi, local.snr), (-105, -2.5))
            self.assertTrue(local._tx_metadata["policy_rf_received"])
            await daemon._bridge_repeater_handler(frame(kind=9, route=2), "channel_f", -90, 5)
            trace = daemon.trace_helper.process_trace_packet.await_args.args[0]
            self.assertTrue(await daemon._trace_packet_injector(trace))
        daemon.repeater_handler.process_packet.assert_not_called()
        daemon.bridge_engine.inject_packet.assert_awaited_once_with(
            "repeater", trace.write_to(), origin_channel="channel_f", rssi=-90, snr=5)

    async def test_main_trace_handler_is_gated_even_without_outer_bridge_scope(self):
        self.service.update(document("drop"))
        daemon = main_module.RepeaterDaemon.__new__(main_module.RepeaterDaemon)
        daemon.config, daemon.policy_service = self.config, self.service
        daemon.trace_helper = SimpleNamespace(process_trace_packet=AsyncMock())
        daemon.repeater_handler = SimpleNamespace(rx_count=0, dropped_count=0, record_packet_only=Mock())
        await daemon._bridge_repeater_handler(frame(kind=9, route=2), "channel_f", -90, 5)
        daemon.trace_helper.process_trace_packet.assert_not_awaited()
        self.assertEqual(daemon.repeater_handler.rx_count, 1)
        metadata = daemon.repeater_handler.record_packet_only.call_args.args[1]
        self.assertIn("Policy blocked packet", metadata["_repeater_drop_reason"])

    async def test_blocked_trace_is_stored_without_duplicate_successful_trace_rows(self):
        handler = engine.RepeaterHandler.__new__(engine.RepeaterHandler)
        handler.storage = SimpleNamespace(record_packet=Mock())
        handler._build_packet_record = Mock(return_value={"fixture": True})
        handler._packet_record_src_dst = Mock(return_value=(None, None))
        handler._path_hash_display = Mock(return_value="")
        handler._append_recent_packet = Mock()
        trace = packet(kind=9, route=2)
        with patch.object(engine, "PacketHeaderUtils", PacketHeaderUtils):
            handler.record_packet_only(trace, {})
            handler.storage.record_packet.assert_not_called()
            handler.record_packet_only(trace, {"_repeater_drop_reason": "Policy blocked packet: fixture"})
        self.assertEqual(handler._build_packet_record.call_args.kwargs["drop_reason"], "Policy blocked packet: fixture")
        handler.storage.record_packet.assert_called_once()
