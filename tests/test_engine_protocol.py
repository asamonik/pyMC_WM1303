"""Exercise overlay routing logic without requiring radios or upstream services."""

from collections import OrderedDict
import asyncio
import importlib.util
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch


ROOT = Path(__file__).resolve().parents[1]


def load_overlay(name):
    """Stub external collaborators only; execute the actual overlay module."""
    modules = {}

    def module(name, **attributes):
        result = ModuleType(name)
        result.__dict__.update(attributes)
        modules[name] = result
        return result

    module("openhop_core")
    module("openhop_core.node")
    module("openhop_core.node.handlers")
    module("openhop_core.node.handlers.base", BaseHandler=object)
    class MultipartAckStub:
        def __init__(self, log_fn):
            pass
        def extract_ack_crc(self, packet):
            payload = packet.payload
            if len(payload) >= 5 and payload[0] & 15 == 3:
                return int.from_bytes(payload[1:5], "little")
    module("openhop_core.node.handlers.multipart", MultipartAckHandler=MultipartAckStub)
    module("openhop_core.paths", resolve_config_path=lambda name: Path("/unused") / name)
    module("openhop_core.protocol", Packet=object)
    module("openhop_core.protocol.constants", MAX_PATH_SIZE=64, PAYLOAD_TYPE_ADVERT=4,
           PAYLOAD_TYPE_ANON_REQ=7, PAYLOAD_TYPE_TRACE=9, PAYLOAD_TYPE_ACK=3,
           PAYLOAD_TYPE_MULTIPART=10, PAYLOAD_TYPE_GRP_DATA=6, PAYLOAD_TYPE_RAW_CUSTOM=15, PH_ROUTE_MASK=3,
           PH_TYPE_MASK=15, PH_TYPE_SHIFT=2, ROUTE_TYPE_DIRECT=2, ROUTE_TYPE_FLOOD=1,
           ROUTE_TYPE_TRANSPORT_DIRECT=3, ROUTE_TYPE_TRANSPORT_FLOOD=0)
    module("openhop_core.protocol.packet_utils", PacketHeaderUtils=object, PacketTimingUtils=object,
           PathUtils=SimpleNamespace(encode_path_len=lambda width, count: ((width - 1) << 6) | count))
    module("repeater")
    module("repeater.airtime", AirtimeManager=object)
    module("repeater.data_acquisition", StorageCollector=object)
    module("repeater.bridge_engine", _active_bridge=None)
    handlers = {"ack": "AckHandler", "advert": "AdvertHandler", "control": "ControlHandler",
                "group_text": "GroupTextHandler", "login_response": "LoginResponseHandler",
                "login_server": "LoginServerHandler", "path": "PathHandler",
                "protocol_request": "ProtocolRequestHandler", "protocol_response": "ProtocolResponseHandler",
                "text": "TextMessageHandler", "trace": "TraceHandler"}
    payload_types = {"ack": 3, "advert": 4, "control": 11, "group_text": 5,
                     "login_response": 1, "login_server": 7, "path": 8,
                     "protocol_request": 0, "protocol_response": 8, "text": 2, "trace": 9}
    for handler_module, handler_class in handlers.items():
        value = payload_types[handler_module]
        handler = type(handler_class, (), {"payload_type": staticmethod(lambda value=value: value)})
        module("openhop_core.node.handlers." + handler_module, **{handler_class: handler})
    if name == "main":
        module("repeater.companion.utils", validate_companion_node_name=object, normalize_companion_identity_key=object)
        module("repeater.config", get_radio_for_board=object, load_config=object, save_config=object)
        module("repeater.data_acquisition.gps_service", GPSService=object)
        module("repeater.data_acquisition.glass_integration", GlassHandler=object)
        module("repeater.config_manager", ConfigManager=object)
        module("repeater.engine", RepeaterHandler=object)
        module("repeater.handler_helpers", **{name: object for name in (
            "AdvertHelper", "DiscoveryHelper", "LoginHelper", "PathHelper",
            "ProtocolRequestHelper", "TextHelper", "TraceHelper")})
        module("repeater.identity_manager", IdentityManager=object)
        module("repeater.packet_router", PacketRouter=object)
        modules["repeater.room_lifecycle"] = load_overlay("room_lifecycle")
        module("repeater.web.http_server", HTTPStatsServer=object, _log_buffer=[])
        module("repeater.web.packet_trace", trace_event=object)
        module("openhop_core.hardware", wm1303_backend=object, tx_queue=object)
    spec = importlib.util.spec_from_file_location("wm1303_test_" + name, ROOT / "overlay/pymc_repeater/repeater" / (name + ".py"))
    result = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(result)
    result._test_imports = modules
    return result


engine = load_overlay("engine")
router_module = load_overlay("packet_router")
main_module = load_overlay("main")


class PacketFixture:
    def __init__(self, path=b"", hash_size=1, route=1):
        self.path = bytearray(path)
        self.path_len = ((hash_size - 1) << 6) | (len(path) // hash_size)
        self.header = (15 << 2) | route
        self.payload = b"payload"
        self.drop_reason = ""

    def get_path_hash_size(self):
        return (self.path_len >> 6) + 1

    def get_path_hash_count(self):
        return self.path_len & 63

    def calculate_packet_hash(self):
        return b"packet hash"

    def get_payload_type(self):
        return (self.header >> 2) & 15

    def get_raw_length(self):
        return 2 + len(self.path) + len(self.payload)

    def write_to(self):
        return bytes([self.header, self.path_len]) + self.path + self.payload

    def read_from(self, raw):
        self.header = raw[0]
        self.path_len = raw[1]
        end = 2 + self.get_path_hash_count() * self.get_path_hash_size()
        self.path = bytearray(raw[2:end])
        self.payload = raw[end:]
        return True

    def is_route_direct(self):
        return self.header & 3 in (2, 3)

    def is_marked_do_not_retransmit(self):
        return False


class EngineProtocolTests(unittest.TestCase):
    def setUp(self):
        self.handler = engine.RepeaterHandler.__new__(engine.RepeaterHandler)
        self.handler.config = {}
        self.handler.local_hash = 0x12
        self.handler.local_hash_bytes = b"\x12\x34\x56"
        self.handler.loop_detect_mode = "off"
        self.handler.seen_packets = OrderedDict()
        self.handler.cache_ttl = 60
        self.handler.max_cache_size = 1000
        self.handler.max_flood_hops = 63

    def test_direct_consumes_hop_from_full_64_byte_path(self):
        for width in (1, 2, 3):
            for route in (2, 3):
                for path in (b"", b"\xff" * width):
                    with self.subTest(width=width, route=route, path=path):
                        self.assertIsNone(self.handler.direct_forward(PacketFixture(path, width, route)))
                        self.assertFalse(self.handler.seen_packets)
        packet = PacketFixture(b"\x12\x34" + bytes(62), hash_size=2, route=2)
        self.assertIs(self.handler.direct_forward(packet), packet)
        self.assertEqual(len(packet.path), 62)
        self.assertEqual(packet.get_path_hash_size(), 2)
        self.assertEqual(packet.get_path_hash_count(), 31)

    def test_flood_appends_all_three_hash_bytes(self):
        packet = PacketFixture(b"\x01\x02\x03", hash_size=3)
        self.assertIs(self.handler.flood_forward(packet), packet)
        self.assertEqual(packet.path, b"\x01\x02\x03\x12\x34\x56")
        self.assertEqual(packet.path_len, 0x82)
        self.handler.seen_packets.clear()
        for unscoped_allowed in (False, True):
            self.handler.config = {"mesh": {"unscoped_flood_allow": unscoped_allowed}}
            scoped = PacketFixture(route=0)
            with patch.object(self.handler, "_check_transport_codes", return_value=(False, "unknown scope")) as check:
                self.assertIsNone(self.handler.flood_forward(scoped))
                check.assert_called_once_with(scoped)
                self.assertFalse(self.handler.seen_packets)
        self.handler.config = {"mesh": {"unscoped_flood_allow": False}}
        self.assertIsNone(self.handler.flood_forward(PacketFixture()))
        with patch.object(self.handler, "_check_transport_codes", return_value=(True, "matching scope")):
            self.assertIsNotNone(self.handler.flood_forward(PacketFixture(route=0)))
        for payload_type in range(9, 16):
            packet = PacketFixture()
            packet.header = (payload_type << 2) | 1
            self.assertIsNone(self.handler.process_packet(packet))

    def test_flood_rejects_append_beyond_path_capacity(self):
        for width, count in ((1, 63), (2, 32), (3, 21)):
            with self.subTest(width=width):
                packet = PacketFixture(bytes(width * count), hash_size=width)
                self.assertIsNone(self.handler.flood_forward(packet))
                self.assertEqual(len(packet.path), width * count)

    def test_loop_detection_matches_full_hashes_and_upstream_thresholds(self):
        thresholds = {"minimal": (4, 2, 1), "moderate": (2, 1, 1), "strict": (1, 1, 1)}
        for mode, counts in thresholds.items():
            for width, count in enumerate(counts, 1):
                with self.subTest(mode=mode, width=width):
                    local_hash = self.handler.local_hash_bytes[:width]
                    self.assertFalse(self.handler._is_flood_looped(PacketFixture(local_hash * (count - 1), width), mode))
                    self.assertTrue(self.handler._is_flood_looped(PacketFixture(local_hash * count, width), mode))
        # Matching a single byte or crossing a hop boundary is not a matching hash.
        self.assertFalse(self.handler._is_flood_looped(PacketFixture(b"\x00\x12\x34\x00", 2), "strict"))

    def test_refreshed_packet_does_not_hide_expired_cache_entries(self):
        packet = PacketFixture()
        with patch.object(engine.time, "time", return_value=0):
            self.handler.mark_seen(packet, "first")
        with patch.object(engine.time, "time", return_value=1):
            self.handler.mark_seen(packet, "second")
        with patch.object(engine.time, "time", return_value=50):
            self.handler.mark_seen(packet, "first")
        self.handler._evict_expired(now=70)
        self.assertEqual(list(self.handler.seen_packets), ["first"])

    def test_reserved_or_incomplete_encoded_paths_are_rejected(self):
        for path, encoded in ((b"1234", 0xC1), (b"a", 0x41), (b"abc", 0x02)):
            with self.subTest(encoded=encoded):
                packet = PacketFixture(path)
                packet.path_len = encoded
                self.assertFalse(self.handler.validate_packet(packet)[0])


class RouterCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.daemon = SimpleNamespace(
            companion_bridges={}, login_helper=None, text_helper=None,
            path_helper=None,
            protocol_request_helper=None, repeater_handler=AsyncMock(return_value=True),
            dispatcher=SimpleNamespace(_register_ack_received=AsyncMock(), wait_for_ack=AsyncMock(return_value=True)),
        )
        self.daemon.repeater_handler.storage = None
        self.router = router_module.PacketRouter(self.daemon)

    async def test_hash_collision_remains_available_for_forwarding(self):
        for payload_type in (0, 2, 7):
            with self.subTest(payload_type=payload_type):
                self.daemon.repeater_handler.reset_mock()
                self.daemon.companion_bridges = {0x42: SimpleNamespace(
                    process_received_packet=AsyncMock(return_value=SimpleNamespace(authenticated=False)))}
                packet = PacketFixture()
                packet.header = (payload_type << 2) | 1
                packet.payload = b"\x42content"
                await self.router._route_packet(packet)
                self.daemon.repeater_handler.assert_awaited_once()

    async def test_other_local_identity_is_tried_when_companion_fails(self):
        companion = SimpleNamespace(process_received_packet=AsyncMock(side_effect=RuntimeError("unavailable")))
        self.daemon.companion_bridges = {0x42: companion}
        self.daemon.text_helper = SimpleNamespace(handlers={0x42: object()}, process_text_packet=AsyncMock(return_value=True))
        packet = PacketFixture()
        packet.header = (2 << 2) | 1
        packet.payload = b"\x42content"
        await self.router._route_packet(packet)
        self.daemon.text_helper.process_text_packet.assert_awaited_once()
        self.daemon.repeater_handler.assert_not_awaited()

    async def test_ack_registers_meshcore_crc_with_dispatcher(self):
        packet = PacketFixture()
        packet.header = (3 << 2) | 1
        packet.payload = b"\x12\x34\x56\x78"
        await self.router._route_packet(packet)
        self.daemon.dispatcher._register_ack_received.assert_awaited_once_with(0x78563412)
        companion = SimpleNamespace(process_received_packet=AsyncMock())
        self.daemon.companion_bridges = {0x42: companion}
        packet.path = bytearray(b"\x12")
        packet.path_len = 1
        packet.header = (3 << 2) | 2
        await self.router._route_packet(packet)
        companion.process_received_packet.assert_awaited_once_with(packet)
        for path in (b"", b"\x12"):
            companion.process_received_packet.reset_mock()
            routed_ack = PacketFixture(path=path, route=2)
            routed_ack.header = (3 << 2) | 2
            routed_ack.payload = b"\x12\x34\x56\x78\x42\x11"
            await self.router._route_packet(routed_ack)
            delivered = companion.process_received_packet.await_args.args[0]
            self.assertEqual(delivered.payload, b"\x12\x34\x56\x78")
            self.assertEqual(delivered.payload_len, 4)
            self.assertEqual(routed_ack.payload, b"\x12\x34\x56\x78\x42\x11")
        for route, path in ((1, b""), (2, b""), (2, b"\x12")):
            self.daemon.dispatcher._register_ack_received.reset_mock()
            self.daemon.repeater_handler.reset_mock()
            companion.process_received_packet.reset_mock()
            packet = PacketFixture(path=path, route=route)
            packet.header = (10 << 2) | route
            packet.payload = b"\x13\x12\x34\x56\x78"
            await self.router._route_packet(packet)
            if path:
                self.daemon.dispatcher._register_ack_received.assert_not_awaited()
                companion.process_received_packet.assert_not_awaited()
                self.daemon.repeater_handler.assert_awaited_once()
            else:
                self.daemon.dispatcher._register_ack_received.assert_awaited_once_with(0x78563412)
                delivered = companion.process_received_packet.await_args.args[0]
                self.assertEqual(delivered.get_payload_type(), 3)
                self.assertEqual(delivered.payload, b"\x12\x34\x56\x78")
                self.assertEqual(packet.get_payload_type(), 10)
                self.daemon.repeater_handler.assert_not_awaited()

    async def test_injection_waits_for_supplied_crc(self):
        packet = PacketFixture()
        packet.header = (2 << 2) | 1
        self.assertTrue(await self.router.inject_packet(packet, wait_for_ack=True, expected_crc=0x12345678, ack_timeout_s=12))
        self.daemon.dispatcher.wait_for_ack.assert_awaited_once_with(0x12345678, timeout=12)

    async def test_failed_injection_is_not_delivered_as_sent(self):
        self.daemon.repeater_handler.return_value = False
        self.assertFalse(await self.router.inject_packet(PacketFixture()))
        self.assertTrue(self.router.queue.empty())

    async def test_direct_transit_is_not_offered_to_local_companions(self):
        bridge = SimpleNamespace(process_received_packet=AsyncMock(return_value=SimpleNamespace(authenticated=True)))
        self.daemon.companion_bridges = {0x42: bridge}
        packet = PacketFixture(path=b"\x12", route=2)
        packet.header = (2 << 2) | 2
        packet.payload = b"\x42content"
        await self.router._route_packet(packet)
        bridge.process_received_packet.assert_not_awaited()
        self.daemon.repeater_handler.assert_awaited_once()

    async def test_group_and_direct_raw_datagrams_reach_companions(self):
        bridge = SimpleNamespace(process_received_packet=AsyncMock())
        self.daemon.companion_bridges = {0x42: bridge}
        packet = PacketFixture()
        packet.header = (6 << 2) | 1
        await self.router._route_packet(packet)
        bridge.process_received_packet.assert_awaited_once()
        self.daemon.repeater_handler.assert_awaited_once()
        bridge.process_received_packet.reset_mock()
        self.daemon.repeater_handler.reset_mock()
        packet.header = (15 << 2) | 2
        await self.router._route_packet(packet)
        bridge.process_received_packet.assert_awaited_once()
        self.daemon.repeater_handler.assert_not_awaited()

    async def test_failed_path_delivery_can_be_retried_and_consumed(self):
        bridge = SimpleNamespace(process_received_packet=AsyncMock(side_effect=RuntimeError("unavailable")))
        self.daemon.companion_bridges = {0x42: bridge}
        packet = PacketFixture()
        packet.header = (8 << 2) | 1
        packet.payload = b"\x42content"
        await self.router._route_packet(packet)
        bridge.process_received_packet = AsyncMock(return_value=SimpleNamespace(authenticated=True))
        self.daemon.repeater_handler.reset_mock()
        await self.router._route_packet(packet)
        bridge.process_received_packet.assert_awaited_once()
        self.daemon.repeater_handler.assert_not_awaited()

    async def test_path_for_local_acl_reaches_helper_despite_companions(self):
        bridge = SimpleNamespace(process_received_packet=AsyncMock(return_value=SimpleNamespace(authenticated=False)))
        self.daemon.companion_bridges = {0x42: bridge}
        self.daemon.path_helper = SimpleNamespace(acl_dict={0x11: object()}, process_path_packet=AsyncMock(return_value=True))
        packet = PacketFixture()
        packet.header = (8 << 2) | 1
        packet.payload = b"\x11content"
        await self.router._route_packet(packet)
        self.daemon.path_helper.process_path_packet.assert_awaited_once()
        self.daemon.repeater_handler.assert_not_awaited()


class TransmitResultTests(unittest.IsolatedAsyncioTestCase):
    async def test_dispatcher_failure_is_not_counted_as_transmitted(self):
        handler = engine.RepeaterHandler.__new__(engine.RepeaterHandler)
        handler.config = {}
        handler._tx_lock = asyncio.Lock()
        handler.dispatcher = SimpleNamespace(send_packet=AsyncMock(return_value=False))
        handler.sent_flood_count = 0
        with self.assertLogs("RepeaterHandler", level="ERROR"):
            task = await handler.schedule_retransmit(PacketFixture(), 0)
            with self.assertRaisesRegex(RuntimeError, "could not transmit"):
                await task
        self.assertEqual(handler.sent_flood_count, 0)
        handler.config = {"repeater": {"mode": "forward"}}
        handler.dispatcher.send_packet.reset_mock()
        task = await handler.schedule_retransmit(PacketFixture(), 0)
        handler.config["repeater"]["mode"] = "no_tx"
        self.assertFalse(await task)
        handler.dispatcher.send_packet.assert_not_awaited()


class MainInjectorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.daemon = main_module.RepeaterDaemon.__new__(main_module.RepeaterDaemon)
        self.daemon._shutdown_task = None
        self.daemon._shutdown_started = False
        self.daemon._companion_activation_lock = asyncio.Lock()
        self.daemon._room_activation_tasks = set()
        self.daemon._room_sync_failure_event = asyncio.Event()
        self.daemon._room_sync_failure = None
        self.daemon.config = {}
        self.daemon.dispatcher = None
        self.daemon.bridge_engine = SimpleNamespace(inject_packet=AsyncMock(return_value=True))
        self.daemon.router = SimpleNamespace(
            inject_packet=AsyncMock(return_value=True), enqueue=AsyncMock(),
            wait_for_packet_ack=AsyncMock(return_value=True),
            _route_packet=AsyncMock(return_value=False),
        )
        self.daemon.advert_helper = None
        self.daemon.trace_helper = None
        self.daemon.login_helper = None
        self.daemon.text_helper = None
        self.daemon.protocol_request_helper = None

    async def test_both_bridge_injectors_accept_expected_ack_crc(self):
        for method in (self.daemon._response_injector, self.daemon._companion_injector):
            with self.subTest(method=method.__name__):
                self.daemon.router.wait_for_packet_ack.reset_mock()
                self.daemon.router.enqueue.reset_mock()
                packet = PacketFixture()
                self.assertTrue(await method(packet, wait_for_ack=True, expected_crc=123, ack_timeout_s=9))
                self.daemon.router.wait_for_packet_ack.assert_awaited_once_with(packet, 123, 9)
                self.daemon.router.enqueue.assert_awaited_once_with(packet)
                self.assertTrue(packet._injected_for_tx)

    async def test_start_failure_shuts_down_without_signalling_ready(self):
        daemon = self.daemon
        loop = asyncio.get_running_loop()
        class Backend:
            _idle_mode = False
            virtual_radios = {}
            _read_active_ui = lambda self: {}

        async def drain_started_tasks():
            tasks = tuple(daemon._service_tasks)
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

        for failure in ('bridge', 'scheduler', 'rx', 'http'):
            with self.subTest(failure=failure):
                daemon.initialize = AsyncMock()
                daemon.text_helper = SimpleNamespace(room_servers={})
                daemon._shutdown = AsyncMock(side_effect=drain_started_tasks)
                daemon._init_wm1303_bridge = Mock()
                daemon.bridge_engine = None
                daemon.radio = None
                daemon.repeater_handler = None
                daemon.local_identity = None
                daemon._service_tasks = []
                failed_http = SimpleNamespace(start=Mock(side_effect=OSError('http failure')))
                notify = Mock()
                if failure == 'bridge':
                    daemon.radio = Backend()
                    daemon._init_wm1303_bridge.side_effect = OSError('bridge failure')
                elif failure == 'scheduler':
                    daemon.bridge_engine = Mock()
                    daemon.radio = Backend()
                    daemon.radio.ensure_tx_queues_started = AsyncMock(side_effect=OSError('scheduler failure'))
                elif failure == 'rx':
                    daemon.bridge_engine = SimpleNamespace(run=AsyncMock(side_effect=OSError('rx failure')))
                imports = dict(main_module._test_imports)
                imports.update({
                    'openhop_core.hardware': SimpleNamespace(WM1303Backend=Backend),
                    'systemd.daemon': SimpleNamespace(notify=notify),
                    'repeater.channel_e_bridge': SimpleNamespace(ChannelEBridge=Mock()),
                    'repeater.channel_f_bridge': SimpleNamespace(ChannelFBridge=Mock()),
                })
                with patch.object(main_module, 'HTTPStatsServer', return_value=failed_http), \
                        patch.object(loop, 'add_signal_handler'), patch.dict(sys.modules, imports):
                    with self.assertRaisesRegex(OSError, failure + ' failure'):
                        await daemon.run()
                daemon._shutdown.assert_awaited_once()
                notify.assert_not_called()
                if failure != 'http':
                    failed_http.start.assert_not_called()

    async def test_empty_manager_rules_do_not_restore_yaml_routes(self):
        class Backend:
            get_radios = lambda self: [object()]

        daemon = self.daemon
        daemon.radio = Backend()
        daemon.repeater_handler = None
        stale_rules = [{'source': 'channel_a', 'target': 'channel_b'}]
        daemon.config = {'bridge': {'bridge_rules': stale_rules}}
        factory = Mock()
        imports = {
            'openhop_core.hardware': SimpleNamespace(WM1303Backend=Backend),
            'repeater.bridge_engine': SimpleNamespace(BridgeEngine=factory),
        }
        with tempfile.TemporaryDirectory() as directory, patch.dict(sys.modules, imports):
            path = Path(directory) / 'wm1303_ui.json'
            with patch.object(main_module, 'resolve_config_path', return_value=path):
                daemon._init_wm1303_bridge()
                self.assertEqual(factory.call_args.kwargs['rules'], stale_rules)
                path.write_text('{"bridge":{"rules":[]}}')
                daemon._init_wm1303_bridge()
                self.assertEqual(factory.call_args.kwargs['rules'], [])
                path.write_text('{broken')
                with self.assertRaisesRegex(ValueError, 'Cannot load bridge rules'):
                    daemon._init_wm1303_bridge()
                self.assertEqual(factory.call_count, 2)

    async def test_shutdown_stops_room_and_retention_before_storage(self):
        stopped = []
        self.daemon._shutdown_started = False
        self.daemon.bridge_engine = None
        self.daemon.repeater_handler = SimpleNamespace(
            storage=SimpleNamespace(close=lambda: stopped.append("storage")))
        room = SimpleNamespace(stop=AsyncMock(side_effect=lambda: stopped.append("room")))
        self.daemon.text_helper = SimpleNamespace(room_servers={0x42: room})
        self.daemon.discovery_helper = None
        self.daemon._metrics_retention = SimpleNamespace(stop=lambda: stopped.append("retention"))
        self.daemon.gps_service = None
        self.daemon.http_server = None
        self.daemon.glass_handler = None
        self.daemon.radio = SimpleNamespace(stop=lambda: stopped.append("radio"))
        self.daemon.router.stop = AsyncMock()
        await self.daemon._shutdown()
        await self.daemon._shutdown()
        self.assertEqual(stopped, ["radio", "room", "retention", "storage"])

    async def test_bridge_rejection_is_reported_and_not_echoed(self):
        self.daemon.bridge_engine.inject_packet.return_value = False
        self.assertFalse(await self.daemon._companion_injector(PacketFixture()))
        self.assertFalse(await self.daemon._response_injector(PacketFixture()))
        self.daemon.router.enqueue.assert_not_awaited()
        self.daemon.router.inject_packet.assert_not_awaited()
        self.daemon.bridge_engine.inject_packet.side_effect = OSError("radio unavailable")
        with self.assertLogs("RepeaterDaemon", level="WARNING"):
            self.assertFalse(await self.daemon._companion_injector(PacketFixture()))
            self.assertFalse(await self.daemon._response_injector(PacketFixture()))
        self.daemon.router.inject_packet.assert_not_awaited()

    async def test_classic_fallback_preserves_result_and_ack_arguments(self):
        self.daemon.bridge_engine = None
        self.daemon.router.inject_packet.return_value = False
        for method in (self.daemon._response_injector, self.daemon._companion_injector):
            with self.subTest(method=method.__name__):
                self.daemon.router.inject_packet.reset_mock()
                packet = PacketFixture()
                self.assertFalse(await method(packet, wait_for_ack=True, expected_crc=123, ack_timeout_s=9))
                self.daemon.router.inject_packet.assert_awaited_once_with(
                    packet, wait_for_ack=True, expected_crc=123, ack_timeout_s=9)

    async def bridge_receive(self, packet):
        packet_module = ModuleType("openhop_core.protocol.packet")
        packet_module.Packet = PacketFixture
        imports = dict(main_module._test_imports)
        imports[packet_module.__name__] = packet_module
        with patch.dict(sys.modules, imports):
            await self.daemon._bridge_repeater_handler(packet.write_to(), origin_channel="channel_e", snr=8.5, rssi=-80)

    async def test_received_local_message_is_handled_once_before_forwarding(self):
        self.daemon.router._route_packet.return_value = True
        self.daemon.repeater_handler = SimpleNamespace(process_packet=lambda packet: self.fail("Consumed packet forwarded"))
        await self.bridge_receive(PacketFixture())
        self.daemon.router._route_packet.assert_awaited_once()
        self.daemon.router.enqueue.assert_not_awaited()
        self.daemon.bridge_engine.inject_packet.assert_not_awaited()
        received = self.daemon.router._route_packet.await_args.args[0]
        self.assertEqual(received._snr, 8.5)
        self.assertEqual(self.daemon.router._route_packet.await_args.kwargs, {"origin_channel": "channel_e"})

    async def test_bridge_transit_keeps_local_helpers_out_of_forwarding(self):
        self.daemon.text_helper = SimpleNamespace(process_text_packet=AsyncMock(return_value=True))
        observed_paths = []

        async def local_route(packet, *, origin_channel=None):
            observed_paths.append(bytes(packet.path))
            return False

        def forward(packet):
            packet.path = bytearray()
            packet.path_len = 0
            return packet, 0

        self.daemon.router._route_packet.side_effect = local_route
        self.daemon.repeater_handler = SimpleNamespace(process_packet=forward)
        packet = PacketFixture(path=b"\x12", route=2)
        packet.header = (2 << 2) | 2
        await self.bridge_receive(packet)
        self.assertEqual(observed_paths, [b"\x12"])
        self.daemon.text_helper.process_text_packet.assert_not_awaited()
        self.daemon.bridge_engine.inject_packet.assert_awaited_once()

    async def test_bridge_sends_extra_ack_before_primary(self):
        primary = PacketFixture(route=2)
        primary.header = (3 << 2) | 2
        extra = PacketFixture(route=2)
        extra.header = (10 << 2) | 2
        result = engine.ForwardResult(primary, 0, ((extra, 0),))
        self.daemon.repeater_handler = SimpleNamespace(process_packet=lambda packet: result)
        await self.bridge_receive(PacketFixture(path=b"\x12", route=2))
        calls = self.daemon.bridge_engine.inject_packet.await_args_list
        self.assertEqual([call.args[1] for call in calls], [extra.write_to(), primary.write_to()])


class CompanionDedupeTests(unittest.TestCase):
    def test_repeated_packet_is_delivered_again_after_ttl_in_small_cache(self):
        router = router_module.PacketRouter(SimpleNamespace())
        packet = PacketFixture()
        with patch.object(router_module.time, "monotonic", return_value=10):
            self.assertTrue(router._should_deliver_path_to_companions(packet))
            self.assertFalse(router._should_deliver_path_to_companions(packet))
        with patch.object(router_module.time, "monotonic", return_value=70):
            self.assertTrue(router._should_deliver_path_to_companions(packet))
        self.assertEqual(len(router._companion_delivered), 1)


if __name__ == "__main__":
    unittest.main()
