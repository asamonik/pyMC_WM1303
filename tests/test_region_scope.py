"""Default advert scopes use saved keys and never silently become unscoped."""

import ast
import asyncio
import base64
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'overlay/pymc_repeater'))
from repeater.region_scope import apply_default_advert_scope, resolve_default_region


class RegionScopeTests(unittest.TestCase):
    def test_no_scope_does_not_read_storage_or_change_packet(self):
        packet = SimpleNamespace(header=17, transport_codes=[0, 0])
        for value in (None, '', '<null>'):
            self.assertIsNone(apply_default_advert_scope(packet, {'mesh': {'default_region': value}}, None))
            self.assertEqual((packet.header, packet.transport_codes), (17, [0, 0]))

    def test_invalid_or_denied_scope_cannot_fall_back_to_public(self):
        region = {'name': 'at-stmk', 'flood_policy': 'deny',
                  'transport_key': base64.b64encode(b'k' * 16).decode()}
        storage = SimpleNamespace(get_transport_keys=lambda: [region])
        for name in ('at-stmk', 'unknown', True, []):
            with self.subTest(name=name), self.assertRaises(ValueError):
                resolve_default_region(name, storage)
        region['flood_policy'] = 'allow'
        region['transport_key'] = base64.b64encode(b'short').decode()
        with self.assertRaises(ValueError):
            resolve_default_region('at-stmk', storage)

    def test_repeater_and_room_adverts_use_selected_region_key(self):
        key = b'custom scope key'
        storage = SimpleNamespace(get_transport_keys=lambda: [{
            'name': 'at-stmk', 'flood_policy': 'allow',
            'transport_key': base64.b64encode(key).decode()}])
        packet = SimpleNamespace(header=17, transport_codes=[0, 0])
        builder = SimpleNamespace(create_advert=Mock(return_value=packet))
        calc = Mock(return_value=1234)
        imports = {
            'openhop_core.protocol': SimpleNamespace(PacketBuilder=builder),
            'openhop_core.protocol.constants': SimpleNamespace(
                ADVERT_FLAG_HAS_NAME=16, ADVERT_FLAG_IS_REPEATER=2,
                ADVERT_FLAG_IS_ROOM_SERVER=3, ROUTE_TYPE_TRANSPORT_FLOOD=0),
            'openhop_core.protocol.transport_keys': SimpleNamespace(calc_transport_code=calc),
        }
        # Execute the actual producer methods; only external radio collaborators
        # are mocked. The real scope helper must run before either send path.
        for file, class_name, method_name in (
            ('main.py', 'RepeaterDaemon', 'send_advert'),
            ('web/api_endpoints.py', 'APIEndpoints', '_send_room_server_advert_async'),
        ):
            source = ROOT / 'overlay/pymc_repeater/repeater' / file
            tree = ast.parse(source.read_text())
            cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name)
            method = next(n for n in cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name == method_name)
            namespace = {'logger': Mock()}
            exec(compile(ast.Module(body=[method], type_ignores=[]), str(source), 'exec'), namespace)
            async def verify_send(value, **kwargs):
                self.assertIs(value, packet)
                self.assertEqual(value.header & 3, 0)
                self.assertEqual(value.transport_codes, [1234, 0])
                return True
            daemon = SimpleNamespace(
                config={'mesh': {'default_region': 'at-stmk'}}, local_identity=object(),
                dispatcher=SimpleNamespace(send_packet=AsyncMock(side_effect=verify_send)),
                repeater_handler=SimpleNamespace(storage=storage, mark_seen=Mock()),
                gps_service=None, _response_injector=AsyncMock(side_effect=verify_send))
            packet.header, packet.transport_codes = 17, [0, 0]
            with self.subTest(producer=method_name), patch.dict(sys.modules, imports):
                if method_name == 'send_advert':
                    result = asyncio.run(namespace[method_name](daemon))
                else:
                    api = SimpleNamespace(config=daemon.config, daemon_instance=daemon)
                    result = asyncio.run(namespace[method_name](api, object(), 'room', 0, 0, False))
                self.assertTrue(result)
                calc.assert_called_with(key, packet)
                if method_name == 'send_advert':
                    daemon._response_injector.side_effect = None
                    daemon._response_injector.return_value = True
                    with patch('repeater.region_scope.apply_default_advert_scope') as scope:
                        self.assertTrue(asyncio.run(namespace[method_name](daemon, zero_hop=True)))
                        scope.assert_not_called()
                    self.assertEqual(builder.create_advert.call_args.kwargs['route_type'], 'direct')
                if method_name == '_send_room_server_advert_async':
                    daemon.dispatcher.send_packet.side_effect = None
                    daemon.dispatcher.send_packet.return_value = False
                    daemon.repeater_handler.mark_seen.reset_mock()
                    result = asyncio.run(namespace[method_name](api, object(), 'room', 0, 0, False))
                    self.assertFalse(result)
                    daemon.repeater_handler.mark_seen.assert_not_called()


    def test_room_cli_advert_uses_default_scope_and_nonempty_name(self):
        source = ROOT / 'overlay/pymc_repeater/repeater/handler_helpers/room_server.py'
        fn = next(n for n in ast.walk(ast.parse(source.read_text()))
                  if isinstance(n, ast.AsyncFunctionDef) and n.name == 'send_room_advert')
        packet = SimpleNamespace(header=17, transport_codes=[0, 0])
        builder = SimpleNamespace(create_advert=Mock(return_value=packet))
        storage = SimpleNamespace(get_transport_keys=lambda: [{
            'name': 'at', 'flood_policy': 'allow',
            'transport_key': base64.b64encode(b'k' * 16).decode()}])
        injector = AsyncMock(return_value=True)
        namespace = dict(packet_injector=injector, local_identity=object(), logger=Mock(),
                         active_room_settings={'node_name': ''}, room_name='My room',
                         config={'mesh': {'default_region': 'at'}}, sqlite_handler=storage)
        exec(compile(ast.Module(body=[fn], type_ignores=[]), str(source), 'exec'), namespace)
        with patch.dict(sys.modules, {
            'openhop_core.protocol': SimpleNamespace(PacketBuilder=builder),
            'openhop_core.protocol.constants': SimpleNamespace(
                ADVERT_FLAG_HAS_NAME=16, ADVERT_FLAG_IS_ROOM_SERVER=3, ROUTE_TYPE_TRANSPORT_FLOOD=0),
            'openhop_core.protocol.transport_keys': SimpleNamespace(calc_transport_code=lambda *a: 1234),
        }):
            self.assertTrue(asyncio.run(namespace['send_room_advert']()))
        self.assertEqual(builder.create_advert.call_args.kwargs['name'], 'My room')
        self.assertEqual((packet.header & 3, packet.transport_codes), (0, [1234, 0]))
        injector.assert_awaited_once_with(packet, wait_for_ack=False)


if __name__ == '__main__':
    unittest.main()
