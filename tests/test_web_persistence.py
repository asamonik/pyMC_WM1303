"""Exercise API persistence and debug collection without services or hardware."""

import json
import io
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "overlay/pymc_repeater"))
sys.path.insert(0, str(ROOT / "overlay/pymc_core/src"))

with patch("subprocess.check_output", return_value=""), \
        patch("threading.Thread.start"), \
        patch("repeater.web.spectrum_collector.get_collector"):
    from repeater.web import debug_collector, wm1303_api


class WebPersistenceTests(unittest.TestCase):
    def test_settings_save_before_live_changes_and_refresh_airtime_limit(self):
        import ast
        import math
        from copy import deepcopy
        import cherrypy
        import yaml
        from repeater.config_manager import ConfigManager

        source = ROOT / 'overlay/pymc_repeater/repeater/web/api_endpoints.py'
        definition = next(node for node in ast.parse(source.read_text()).body
                          if isinstance(node, ast.ClassDef) and node.name == 'APIEndpoints')
        methods = {'_success', '_error', '_require_post', 'set_mode', 'set_duty_cycle',
                   'update_duty_cycle_config', 'update_advert_rate_limit_config',
                   'global_flood_policy', '_save_unscoped_flood_policy',
                   'unscoped_flood_policy', 'default_region',
                   'update_radio_config', 'save_cad_settings'}
        definition.body = [node for node in definition.body
                           if isinstance(node, ast.FunctionDef) and node.name in methods]
        for method in definition.body:
            method.decorator_list = []
        namespace = dict(math=math, logger=Mock(), cherrypy=cherrypy)
        exec(compile(ast.Module(body=[definition], type_ignores=[]), str(source), 'exec'), namespace)
        request = SimpleNamespace(method='POST', json={})
        with tempfile.TemporaryDirectory() as directory, patch.object(cherrypy, 'request', request):
            config = {'radio_type': 'sx1262', 'radio': {'tx_power': 22},
                      'repeater': {'node_name': 'fixture', 'advert_rate_limit': {'refill_tokens': 2}},
                      'mesh': {'unscoped_flood_allow': True},
                      'duty_cycle': {'max_airtime_percent': 10, 'max_airtime_per_minute': 6000}}
            airtime = SimpleNamespace(max_airtime_per_minute=6000, tx_history=[(1, 2)])
            handler = SimpleNamespace(airtime_mgr=airtime, reload_runtime_config=Mock())
            radio = SimpleNamespace(set_custom_cad_thresholds=Mock(return_value=None))
            daemon = SimpleNamespace(config=config, radio=radio, repeater_handler=handler,
                                     advert_helper=SimpleNamespace(reload_config=Mock()))
            path = Path(directory) / 'config.yaml'
            manager = ConfigManager(str(path), config, daemon)
            self.assertTrue(manager.save_to_file())
            api = namespace['APIEndpoints']()
            api.config, api.config_manager, api.daemon_instance = config, manager, daemon
            api._set_cors_headers = lambda: None
            import base64
            region = {'name': 'at-stmk', 'flood_policy': 'allow',
                      'transport_key': base64.b64encode(b'k' * 16).decode()}
            api._get_storage = Mock(return_value=SimpleNamespace(get_transport_keys=lambda: [region]))
            requests = (
                ('set_mode', {'mode': 'no_tx'}),
                ('set_duty_cycle', {'enabled': False}),
                ('update_duty_cycle_config', {'max_airtime_percent': 5}),
                ('update_advert_rate_limit_config', {'bucket_capacity': 3}),
                ('global_flood_policy', {'global_flood_allow': False}),
                ('update_radio_config', {'tx_power': 24}),
                ('save_cad_settings', {'peak': 20, 'min_val': 10}),
            )
            before, disk_before = deepcopy(config), path.read_bytes()
            for name, payload in requests + (
                ('unscoped_flood_policy', {'unscoped_flood_allow': False}),
                ('default_region', {'default_region': 'at-stmk'}),
            ):
                with self.subTest(failed_save=name), patch.object(manager, 'save_to_file', return_value=False):
                    request.json = deepcopy(payload)
                    self.assertFalse(getattr(api, name)()['success'])
                    self.assertEqual(request.json, payload)
                    self.assertEqual(config, before)
                    self.assertEqual(path.read_bytes(), disk_before)
            handler.reload_runtime_config.assert_not_called()
            radio.set_custom_cad_thresholds.assert_not_called()
            for name, payload in (
                ('set_duty_cycle', {'enabled': 'false'}),
                ('unscoped_flood_policy', {'unscoped_flood_allow': 'false'}),
                ('default_region', {'default_region': True}),
                ('default_region', {'default_region': 'missing'}),
                ('update_duty_cycle_config', {'max_airtime_percent': 5, 'enforcement_enabled': 'false'}),
                ('update_duty_cycle_config', {'max_airtime_percent': float('nan')}),
                ('update_advert_rate_limit_config', {'bucket_capacity': 3, 'ewma_alpha': float('nan')}),
                ('update_advert_rate_limit_config', {'rate_limit_enabled': 'false'}),
                ('update_advert_rate_limit_config', {'bucket_capacity': 3, 'quiet_max': 6}),
                ('update_radio_config', {'tx_power': 24, 'latitude': 100}),
                ('update_radio_config', {'flood_advert_interval_hours': 169}),
                ('save_cad_settings', {'peak': 20, 'min_val': 10, 'detection_rate': 'invalid'}),
            ):
                with self.subTest(invalid=name), patch.object(manager, 'save_to_file') as save:
                    request.json = payload
                    self.assertFalse(getattr(api, name)()['success'])
                    self.assertEqual(config, before)
                    save.assert_not_called()
            for name, payload in requests[:5]:
                request.json = payload
                self.assertTrue(getattr(api, name)()['success'])
                self.assertEqual(yaml.safe_load(path.read_text()), config)
            self.assertEqual(airtime.max_airtime_per_minute, 3000)
            self.assertEqual(airtime.tx_history, [(1, 2)])
            self.assertEqual(config['duty_cycle']['max_airtime_percent'], 5)
            self.assertFalse(config['mesh']['unscoped_flood_allow'])
            request.method = 'GET'
            self.assertEqual(api.default_region(), {'success': True, 'data': {'default_region': None}})
            request.method = 'POST'
            request.json = {'default_region': ' #at-stmk '}
            self.assertTrue(api.default_region()['success'])
            self.assertEqual(yaml.safe_load(path.read_text())['mesh']['default_region'], 'at-stmk')
            request.json = {'default_region': None}
            self.assertTrue(api.default_region()['success'])
            self.assertIsNone(yaml.safe_load(path.read_text())['mesh']['default_region'])
            request.json = {'unscoped_flood_allow': True}
            self.assertTrue(api.unscoped_flood_policy()['success'])
            self.assertTrue(config['mesh']['unscoped_flood_allow'])
            self.assertTrue(config['mesh']['global_flood_allow'])
            self.assertEqual(config['repeater']['advert_rate_limit']['refill_tokens'], 2)
            daemon.advert_helper.reload_config.assert_called()
            request.json = {'quiet_max': 0.05, 'normal_max': 0.2, 'busy_max': 0.5}
            self.assertTrue(api.update_advert_rate_limit_config()['data']['live_update'])
            self.assertEqual(config['repeater']['advert_adaptive']['thresholds'],
                             {'quiet_max': 0.05, 'normal_max': 0.2, 'busy_max': 0.5,
                              'normal': 0.05, 'busy': 0.2, 'congested': 0.5})
            for applied in (None, False):
                radio.set_custom_cad_thresholds.return_value = applied
                request.json = {'peak': 20, 'min_val': 10}
                result = api.save_cad_settings()
                self.assertTrue(result['saved'])
                self.assertEqual(result['restart_required'], applied is False)

            config['radio_type'] = 'wm1303'
            for name, payload in requests[-2:]:
                request.json = payload
                with patch.object(manager, 'save_to_file') as save:
                    self.assertIn('Manager', getattr(api, name)()['error'])
                    save.assert_not_called()
            request.json = {'node_name': 'renamed', 'flood_advert_interval_hours': 72}
            with patch.object(manager, '_apply_live_radio_config') as tune:
                self.assertTrue(api.update_radio_config()['data']['live_update'])
                tune.assert_not_called()
            self.assertEqual(yaml.safe_load(path.read_text())['repeater']['send_advert_interval_hours'], 72)
            request.json = {'max_airtime_percent': 8}
            with patch.object(manager, 'live_update_daemon', return_value=False):
                result = api.update_duty_cycle_config()['data']
            self.assertTrue(result['persisted'])
            self.assertTrue(result['restart_required'])

    def test_config_import_and_vanity_apply_are_transactional(self):
        import ast
        from copy import deepcopy
        import yaml
        from repeater.config_manager import ConfigManager

        source = ROOT / 'overlay/pymc_repeater/repeater/web/api_endpoints.py'
        api_class = next(node for node in ast.parse(source.read_text()).body
                         if isinstance(node, ast.ClassDef) and node.name == 'APIEndpoints')
        api_class.body = [node for node in api_class.body if isinstance(node, ast.FunctionDef)
                          and node.name in ('config_import', 'generate_vanity_key', '_require_post', '_error',
                                            '_validate_configured_identity_hashes')]
        for method in api_class.body:
            method.decorator_list = []
        request = SimpleNamespace(method='POST')
        namespace = {
            'logger': Mock(),
            'cherrypy': SimpleNamespace(request=request, HTTPError=wm1303_api.cherrypy.HTTPError),
            'derive_companion_public_key_hex': lambda key: (
                bytes.fromhex(key) if isinstance(key, str) else key
            )[:32].hex(),
        }
        exec(compile(ast.Module(body=[api_class], type_ignores=[]), str(source), 'exec'), namespace)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.yaml'
            config = {'radio_type': 'wm1303', 'radio': {'frequency': 869525000},
                      'repeater': {'identity_key': b'a' * 32,
                                   'security': {'admin_password': 'keep-admin', 'guest_password': 'old-guest',
                                                'jwt_secret': 'keep-jwt', 'max_clients': 3}},
                      'identities': {'companions': [{'name': 'local', 'identity_key': '22' * 32}]},
                      'web': {'port': 8000, 'cors_enabled': False}}
            daemon = SimpleNamespace(config=config, radio=Mock(), local_identity=object(),
                                     login_helper=SimpleNamespace(refresh_repeater_security=Mock()))
            manager = ConfigManager(str(path), config, daemon)
            self.assertTrue(manager.save_to_file())
            api = namespace['APIEndpoints']()
            api.config, api.config_manager = config, manager
            api._set_cors_headers = Mock()
            request.json = {'config': {
                'repeater': {'identity_key': '33' * 32, 'security': {
                    'admin_password': '*** REDACTED ***', 'guest_password': 'new-guest', 'jwt_secret': '*** REDACTED ***'}},
                'identities': {'companions': [{'name': 'local', 'identity_key': '*** REDACTED ***'}]},
                'web': {'port': 8123}, 'radio': {'frequency': 915000000}}}
            original, original_request, original_file = deepcopy(config), deepcopy(request.json), path.read_bytes()
            with patch.object(manager, 'save_to_file', return_value=False), \
                    patch.object(manager, 'live_update_daemon') as live:
                result = api.config_import()
                self.assertFalse(result['success'])
                self.assertFalse(result['saved'])
                live.assert_not_called()
            self.assertEqual((config, request.json, path.read_bytes()), (original, original_request, original_file))
            with patch.object(manager, 'save_to_file', wraps=manager.save_to_file) as save, \
                    patch.object(manager, 'live_update_daemon', wraps=manager.live_update_daemon) as live:
                result = api.config_import()
                save.assert_called_once()
                live.assert_called_once_with(['repeater'])
            self.assertTrue(result['success'])
            self.assertTrue(result['restart_required'])
            self.assertEqual(result['skipped_sections'], ['radio'])
            self.assertIn('Manager', result['message'])
            self.assertEqual(request.json, original_request)
            self.assertEqual(config['radio'], original['radio'])
            self.assertEqual(config['repeater']['identity_key'], b'3' * 32)
            self.assertEqual(config['repeater']['security'], dict(original['repeater']['security'], guest_password='new-guest'))
            self.assertEqual(config['identities'], original['identities'])
            self.assertEqual(config['web'], {'port': 8123, 'cors_enabled': False})
            self.assertEqual(yaml.safe_load(path.read_text()), config)
            daemon.radio.configure_radio.assert_not_called()

            request.json = {'prefix': '55', 'apply': True}
            generated = {'private_hex': '44' * 64, 'public_hex': '55' * 32, 'attempts': 1}
            generator = Mock(return_value=generated)
            original, original_file, active_identity = deepcopy(config), path.read_bytes(), daemon.local_identity
            with patch.dict(sys.modules, {'repeater.keygen': SimpleNamespace(generate_vanity_key=generator)}), \
                    patch.object(manager, 'live_update_daemon') as live:
                request.json['apply'] = 'false'
                self.assertFalse(api.generate_vanity_key()['success'])
                generator.assert_not_called()
                request.json['apply'] = True
                with patch.object(manager, 'save_to_file', return_value=False):
                    self.assertFalse(api.generate_vanity_key()['applied'])
                self.assertEqual((config, path.read_bytes()), (original, original_file))
                result = api.generate_vanity_key()
                live.assert_not_called()
            self.assertTrue(result['success'])
            self.assertTrue(result['data']['applied'])
            self.assertTrue(result['data']['restart_required'])
            self.assertEqual(config['repeater']['identity_key'], b'D' * 64)
            self.assertEqual(yaml.safe_load(path.read_text()), config)
            self.assertIs(daemon.local_identity, active_identity)
            self.assertNotIn('applied', generated)
            self.assertEqual(request.json, {'prefix': '55', 'apply': True})

    def test_identity_crud_is_transactional_and_staged(self):
        import ast
        from copy import deepcopy
        import logging
        import cherrypy
        from repeater.companion_storage import companion_limits_from_settings, legacy_owner_from_settings
        from repeater.config_manager import ConfigManager

        path = ROOT / 'overlay/pymc_repeater/repeater/web/api_endpoints.py'
        api_class = next(node for node in ast.parse(path.read_text()).body
                         if isinstance(node, ast.ClassDef) and node.name == 'APIEndpoints')
        api_class.body = [node for node in api_class.body if isinstance(node, ast.FunctionDef)
                          and node.name in ('identities', 'identity', 'create_identity', 'update_identity',
                                            'delete_identity', '_validate_configured_identity_hashes')]
        for method in api_class.body:
            method.decorator_list = []
        def find(entries, name=None, **kwargs):
            index = next((i for i, entry in enumerate(entries) if entry['name'] == name), None)
            return index, None if index is not None else 'not found'
        def heal(entries):
            entries[0]['name'] = 'display-only'
            return True
        namespace = dict(deepcopy=deepcopy, cherrypy=cherrypy, logger=logging.getLogger('IdentityTest'),
                         find_companion_index=find, heal_companion_empty_names=heal,
                         derive_companion_public_key_hex=lambda key: key.hex() if isinstance(key, bytes) else key,
                         companion_limits_from_settings=companion_limits_from_settings,
                         legacy_owner_from_settings=legacy_owner_from_settings)
        exec(compile(ast.Module(body=[api_class], type_ignores=[]), str(path), 'exec'), namespace)
        request = SimpleNamespace(method='GET', json={})
        fake_identity = SimpleNamespace(get_public_key=lambda: bytes([17]) * 32,
                                        get_address_bytes=lambda: bytes([17]))
        with tempfile.TemporaryDirectory() as directory, patch.object(cherrypy, 'request', request):
            config = {'repeater': {'identity_key': '00' * 32}, 'identities': {
                'room_servers': [{'name': 'room', 'identity_key': '11' * 32,
                                  'settings': {'admin_password': 'admin', 'guest_password': 'guest'}}],
                'companions': [{'name': 'companion', 'identity_key': '22' * 32, 'settings': {}}],
            }}
            original = deepcopy(config)
            named = {entry['name']: (fake_identity, entry, kind) for kind, section in
                     (('room_server', 'room_servers'), ('companion', 'companions'))
                     for entry in config['identities'][section]}
            identity_manager = SimpleNamespace(named_identities=named, identities={17: named['room']},
                list_identities=lambda: [], get_identity_by_name=lambda name: named.get(name))
            activate_room = AsyncMock(return_value=True)
            async def add_companion(entry):
                return None
            daemon = SimpleNamespace(identity_manager=identity_manager, companion_bridges={17: object()},
                                     companion_frame_servers=[object()], add_room_from_config=activate_room,
                                     add_companion_from_config=add_companion)
            runtime_maps = (dict(named), dict(identity_manager.identities), dict(daemon.companion_bridges),
                            list(daemon.companion_frame_servers))
            manager = ConfigManager(str(Path(directory) / 'config.yaml'), config)
            api = namespace['APIEndpoints']()
            api.config, api.config_manager, api.daemon_instance = config, manager, daemon
            api.event_loop = SimpleNamespace(is_running=lambda: True)
            api._set_cors_headers = lambda: None
            api._require_post = lambda: None
            api._fmt_hash = lambda key: '0x11'
            api._error = lambda error: {'success': False, 'error': str(error)}
            api._success = lambda data, **kwargs: {'success': True, 'data': data, **kwargs}
            with patch.object(manager, 'save_to_file', return_value=False):
                for kind, name in (('room_server', 'room'), ('companion', 'companion')):
                    request.method, request.json = 'POST', {'name': 'new', 'type': kind, 'identity_key': '33' * 32}
                    self.assertFalse(api.create_identity()['success'])
                    request.method, request.json = 'PUT', {'name': name, 'type': kind, 'settings': {'node_name': 'unsaved'}}
                    self.assertFalse(api.update_identity()['success'])
                    request.method = 'DELETE'
                    self.assertFalse(api.delete_identity(name=name, type=kind)['success'])
                    self.assertEqual(config, original)
            request.method, request.json = 'PUT', {'name': 'room', 'settings': {'admin_password': 'same', 'guest_password': 'same'}}
            self.assertFalse(api.update_identity()['success'])
            self.assertEqual(config, original)
            request.method = 'GET'
            self.assertTrue(api.identities()['success'])
            detail = api.identity(name='room')['data']
            detail['settings']['node_name'] = 'caller change'
            self.assertEqual(config, original)
            for kind, name in (('room_server', 'room'), ('companion', 'companion')):
                request.method, request.json = 'PUT', {'name': name, 'type': kind, 'settings': {'node_name': 'staged'}}
                updated = api.update_identity()
                self.assertTrue(updated['saved'] and updated['restart_required'])
                self.assertFalse(updated['live_updated'])
                request.method = 'DELETE'
                deleted = api.delete_identity(name=name, type=kind)
                self.assertTrue(deleted['saved'] and deleted['restart_required'])
                self.assertFalse(deleted['live_updated'])
            self.assertEqual((named, identity_manager.identities, daemon.companion_bridges,
                              daemon.companion_frame_servers), runtime_maps)
            self.assertNotIn('node_name', named['room'][1]['settings'])
            activate_room.assert_not_called()
            def submit(coro, loop):
                coro.close()
                self.assertFalse(manager._lock._is_owned())
                return SimpleNamespace(result=lambda timeout: True)
            with patch('asyncio.run_coroutine_threadsafe', side_effect=submit) as activate_identity:
                for kind in ('room_server', 'companion'):
                    key = ('33' if kind == 'room_server' else '44') * 32
                    request.method, request.json = 'POST', {'name': 'new-' + kind, 'type': kind, 'identity_key': key}
                    created = api.create_identity()
                    self.assertTrue(created['saved'] and created['live_updated'])
                    self.assertFalse(created['restart_required'])
                activate_room.assert_called_once()
                self.assertEqual(activate_identity.call_count, 2)

    def test_password_and_wm_setup_share_durable_live_state(self):
        import ast
        import importlib.util
        import logging
        import os
        from copy import deepcopy
        import cherrypy
        import yaml
        from repeater.config_manager import ConfigManager

        web = ROOT / 'overlay/pymc_repeater/repeater/web'
        spec = importlib.util.spec_from_file_location('repeater.web._auth_flow_test', web / 'auth_endpoints.py')
        auth_module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'repeater.web.auth.middleware': SimpleNamespace(require_auth=lambda f: f)}):
            spec.loader.exec_module(auth_module)
        api_class = next(node for node in ast.parse((web / 'api_endpoints.py').read_text()).body
                         if isinstance(node, ast.ClassDef) and node.name == 'APIEndpoints')
        api_class.body = [node for node in api_class.body if isinstance(node, ast.FunctionDef)
                          and node.name in ('setup_wizard', 'needs_setup', '_require_post')]
        for method in api_class.body:
            method.decorator_list = []
        namespace = dict(__file__=str(web / 'api_endpoints.py'), Path=Path, os=os,
                         logger=logging.getLogger('SetupTest'), cherrypy=cherrypy)
        exec(compile(ast.Module(body=[api_class], type_ignores=[]), 'setup-flow', 'exec'), namespace)
        request = SimpleNamespace(method='POST', headers={'Authorization': 'Bearer fixture'},
                                  remote=SimpleNamespace(ip='127.0.0.1'))
        jwt = SimpleNamespace(verify_jwt=lambda _: {'sub': 'admin', 'client_id': 'fixture'},
                              create_jwt=lambda *args: 'fixture-jwt', expiry_minutes=15)
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(cherrypy, 'request', request), \
                patch.object(cherrypy, 'response', SimpleNamespace(headers={}, status=200)), \
                patch.object(cherrypy, 'config', {'jwt_handler': jwt, 'token_manager': Mock()}):
            root = Path(directory)
            config = {'repeater': {'node_name': 'mesh-repeater-01',
                                  'security': {'admin_password': 'old-password', 'jwt_secret': 'preserved'}},
                      'radio_type': 'wm1303', 'radio': {'frequency': 869525000},
                      'storage': {'storage_dir': directory}}
            acl = SimpleNamespace(password='old-password')
            refresh = Mock(side_effect=lambda current: setattr(acl, 'password', current['repeater']['security']['admin_password']))
            daemon = SimpleNamespace(config=config, login_helper=SimpleNamespace(refresh_repeater_security=refresh))
            manager = ConfigManager(str(root / 'config.yaml'), config, daemon)
            self.assertTrue(manager.save_to_file())
            auth = auth_module.AuthEndpoints(config, jwt, Mock(), manager)
            def change(current, new):
                request.body = io.BytesIO(json.dumps({'current_password': current, 'new_password': new}).encode())
                return json.loads(auth.change_password())
            before = deepcopy(config)
            with patch.object(manager, 'save_to_file', return_value=False):
                self.assertFalse(change('old-password', 'new-password')['success'])
            self.assertEqual(config, before)
            refresh.assert_not_called()
            self.assertTrue(change('old-password', 'new-password')['live_updated'])
            self.assertEqual(acl.password, 'new-password')
            self.assertEqual(yaml.safe_load((root / 'config.yaml').read_text()), config)
            request.body = io.BytesIO(json.dumps({'username': 'admin', 'password': 'new-password', 'client_id': 'fixture'}).encode())
            self.assertTrue(json.loads(auth.login())['success'])
            with patch.object(manager, 'live_update_daemon', return_value=False):
                changed = change('new-password', 'saved-password')
            self.assertTrue(changed['saved'])
            self.assertTrue(changed['restart_required'])

            api = namespace['APIEndpoints']()
            api.config, api.config_manager = config, manager
            request.json = {'hardware_key': 'wm1303', 'node_name': 'configured-node', 'admin_password': 'setup-password'}
            (root / 'wm1303_ui.json').write_text('{"channels":[{"active":true,"frequency":869525000}]}')
            ui_before = (root / 'wm1303_ui.json').read_bytes()
            before = deepcopy(config)
            with patch.object(manager, 'save_to_file', return_value=False), patch('threading.Thread') as thread:
                self.assertFalse(api.setup_wizard()['success'])
                thread.assert_not_called()
            self.assertEqual(config, before)
            restart = Mock(return_value=(False, 'fixture restart unavailable'))
            with patch.dict(sys.modules, {'repeater.service_utils': SimpleNamespace(restart_service=restart)}), \
                    patch('time.sleep'), \
                    patch('threading.Thread', side_effect=lambda **kw: SimpleNamespace(start=kw['target'])):
                result = api.setup_wizard()
            self.assertTrue(result['saved'])
            self.assertTrue(result['live_updated'])
            self.assertEqual(result['restart']['status'], 'failed')
            self.assertEqual(api.needs_setup()['restart']['error'], 'fixture restart unavailable')
            self.assertFalse(api.needs_setup()['needs_setup'])
            self.assertEqual(acl.password, 'setup-password')
            self.assertEqual(config['radio'], before['radio'])
            self.assertEqual(config['repeater']['security']['jwt_secret'], 'preserved')
            self.assertEqual(yaml.safe_load((root / 'config.yaml').read_text()), config)
            self.assertEqual((root / 'wm1303_ui.json').read_bytes(), ui_before)

    def test_console_charts_preserve_native_counts_rates_and_missing_gauges(self):
        import ast
        # Execute the actual chart methods without importing/starting the
        # unrelated Console authentication, companion and hardware services.
        path = ROOT / 'overlay/pymc_repeater/repeater/web/api_endpoints.py'
        tree = ast.parse(path.read_text())
        api_class = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'APIEndpoints')
        methods = {'_success', '_error', '_get_time_range', '_process_gauge_data', 'metrics_graph_data'}
        api_class.body = [node for node in api_class.body if isinstance(node, ast.FunctionDef) and node.name in methods]
        for method in api_class.body:
            method.decorator_list = []
        namespace = {'time': SimpleNamespace(time=lambda: 180), 'logger': Mock()}
        exec(compile(ast.Module(body=[api_class], type_ignores=[]), str(path), 'exec'), namespace)
        api = namespace['APIEndpoints']()
        for source, mode, values, unit in (
            ('sqlite', 'bucket_count', [5, 2, None], 'packets/bucket'),
            ('rrd', None, [0.5, 0.2, None], 'packets/s'),
        ):
            data = {'start_time': 0, 'end_time': 180, 'step': 60, 'timestamps': [0, 60, 120],
                    'data_source': source, 'counter_mode': mode,
                    'metrics': {'rx_count': values, 'avg_rssi': [-100, None, -95],
                                'neighbor_count': [None, 0, 1]}}
            api._get_storage = lambda: SimpleNamespace(get_rrd_data=lambda **kwargs: data)
            result = api.metrics_graph_data()
            self.assertTrue(result['success'])
            chart = result['data']
            self.assertEqual(chart['data_source'], source)
            self.assertEqual(chart['counter_mode'], mode or 'rate')
            self.assertEqual(chart['series'][0]['data'], [[0, values[0]], [60000, values[1]], [120000, None]])
            self.assertEqual(chart['series'][0]['unit'], unit)
            self.assertEqual(chart['series'][1]['data'], [[0, -100], [60000, None], [120000, -95]])
            self.assertEqual(chart['series'][2]['data'], [[0, None], [60000, 0], [120000, 1]])

    def test_http_failed_start_cleans_owned_resources_and_allows_new_instance(self):
        import ast
        import logging
        import os
        import secrets
        from typing import Callable, Optional
        from cherrypy._cptree import Tree

        # Execute the actual lifecycle class, with a real isolated routing tree
        # and mocked listener/auth workers. No port, service or updater is used.
        source = ROOT / 'overlay/pymc_repeater/repeater/web/http_server.py'
        definition = next(node for node in ast.parse(source.read_text()).body
                          if isinstance(node, ast.ClassDef) and node.name == 'HTTPStatsServer')
        states = SimpleNamespace(STOPPED='stopped', STARTED='started', EXITING='exiting')
        engine = SimpleNamespace(state=states.STARTED, states=states, start=Mock(), exit=Mock())
        engine.exit.side_effect = lambda: setattr(engine, 'state', states.EXITING)
        tree = Tree()
        previous = tree.apps[''] = object()
        cp = SimpleNamespace(engine=engine, tree=tree, config=Mock(),
                             log=SimpleNamespace(access_log=Mock(), error_log=Mock()))
        handlers = []
        def sqlite_handler(_path):
            handler = Mock()
            handlers.append(handler)
            return handler
        app_factory = Mock(side_effect=RuntimeError('app construction failed'))
        namespace = dict(__file__=str(source), __package__='repeater.web',
                         cherrypy=cp, threading=threading, Optional=Optional, Callable=Callable,
                         logger=logging.getLogger('HTTPTest'), logging=logging, os=os, secrets=secrets, Path=Path,
                         SQLiteHandler=sqlite_handler, JWTHandler=Mock(), APITokenManager=Mock(),
                         StatsApp=app_factory, AuthEndpoints=Mock(return_value=SimpleNamespace()),
                         DocEndpoint=Mock(return_value=SimpleNamespace()), WEBSOCKET_AVAILABLE=False,
                         register_require_auth_tool=Mock())
        exec(compile(ast.Module(body=[definition], type_ignores=[]), str(source), 'exec'), namespace)
        server_type = namespace['HTTPStatsServer']
        api = Mock()
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(wm1303_api, 'WM1303API', return_value=api):
            config = {'repeater': {'security': {'jwt_secret': 'test-secret'}},
                      'storage': {'storage_dir': directory}, 'web': {'web_path': directory}}
            with self.assertRaisesRegex(RuntimeError, 'construction failed'):
                server_type(config=config)
            handlers[-1].stop_wal_checkpoint_thread.assert_called_once()
            handlers[-1].close_thread_connection.assert_called_once()
            engine.exit.assert_not_called()
            self.assertIs(tree.apps[''], previous)

            app_factory.side_effect = lambda *args: SimpleNamespace(api=SimpleNamespace(config_manager=object()))
            blocked = server_type(config=config)
            with self.assertRaisesRegex(RuntimeError, 'already in use'):
                blocked.start()
            engine.exit.assert_not_called()
            handlers[-1].stop_wal_checkpoint_thread.assert_called_once()

            engine.state = states.STOPPED
            engine.start.side_effect = OSError('address already in use')
            failed = server_type(config=config)
            with self.assertRaisesRegex(OSError, 'address already in use'):
                failed.start()
            failed.stop()  # repeated shutdown must not close someone else's bus
            engine.exit.assert_called_once()
            handlers[-1].stop_wal_checkpoint_thread.assert_called_once()
            handlers[-1].close_thread_connection.assert_called_once()
            api.start.assert_not_called()
            api.stop.assert_not_called()
            self.assertEqual(tree.apps, {'': previous})

            engine.start.reset_mock()
            engine.exit.reset_mock()
            engine.start.side_effect = lambda: setattr(engine, 'state', states.STARTED)
            ready = server_type(config=config)
            ready.start()
            ready.start()
            engine.start.assert_called_once()
            api.start.assert_called_once()
            with self.assertRaisesRegex(RuntimeError, 'already in use'):
                server_type(config=config).start()
            engine.exit.assert_not_called()
            ready.stop()
            ready.stop()
            engine.exit.assert_called_once()
            api.stop.assert_called_once()
            self.assertIsNone(server_type._engine_owner)
            self.assertEqual(tree.apps, {'': previous})

    def test_settings_validation_restart_and_concurrent_updates(self):
        import time
        from test_radio_bridge import backend_module, regions
        with tempfile.TemporaryDirectory() as directory:
            ui_path = Path(directory) / 'wm1303_ui.json'
            original = {'channels': [], 'region': {'code': 'EU868'},
                        'bridge': {'rules': [{'from': 'channel_e', 'to': 'repeater'}]}}
            ui_path.write_text(json.dumps(original))
            with patch.object(wm1303_api, '_UI_JSON', ui_path), \
                    patch.dict(sys.modules, {'openhop_core.hardware.wm1303_backend': backend_module,
                                             'openhop_core.hardware.region_config': regions}), \
                    patch.object(wm1303_api, 'sync_global_conf', return_value={'status': 'ok'}), \
                    patch.object(wm1303_api.subprocess, 'Popen') as restart:
                api = wm1303_api.WM1303API()
                for method, body in ((api._bridge_post, {}),
                                     (api._adv_config_post, dict(group='tx_queue', params={'inter_packet_delay': -0.5})),
                                     (api._adv_config_post, dict(group='tx_queue', params={'queue_size': 1.5})),
                                     (api._region_post, dict(code='CUSTOM', tx_freq_min=900, tx_freq_max=800))):
                    with patch.object(wm1303_api, '_body', return_value=body), \
                            self.assertRaises(wm1303_api.cherrypy.HTTPError):
                        method()
                    self.assertEqual(json.loads(ui_path.read_text()), original)
                with patch.object(wm1303_api, '_body', return_value={'code': 'US915', 'restart': True}):
                    self.assertEqual(json.loads(api._region_post())['status'], 'ok')
                restart.assert_called_once()
                with patch.object(wm1303_api, '_body', return_value={'rf0': {'freq_hz': 0}}):
                    api._rfchains_post()
                self.assertIsNone(json.loads(ui_path.read_text())['rf_center_freq_mhz'])
                load = wm1303_api._load_ui
                def slow_load():
                    data = load()
                    time.sleep(0.02)  # expose lost updates without the request-level lock
                    return data
                failures = []
                def save_channel(key):
                    try:
                        api._aux_channel_post(key)
                    except Exception as exc:
                        failures.append(exc)
                with patch.object(wm1303_api, '_load_ui', side_effect=slow_load), \
                        patch.object(wm1303_api, '_body', side_effect=lambda: {'name': threading.current_thread().name}):
                    workers = [threading.Thread(target=save_channel, args=(key,), name=key) for key in ('channel_e', 'channel_f')]
                    for worker in workers:
                        worker.start()
                    for worker in workers:
                        worker.join(timeout=2)
                        self.assertFalse(worker.is_alive())
                self.assertEqual(failures, [])
                saved = json.loads(ui_path.read_text())
                self.assertEqual(saved['channel_e']['name'], 'channel_e')
                self.assertEqual(saved['channel_f']['name'], 'channel_f')

    def test_metrics_lifecycle_and_spectrum_never_fabricates_measurements(self):
        from repeater.web import spectrum_collector
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spectrum_file = root / 'spectrum.json'
            daemon = SimpleNamespace(config={'storage': {'storage_dir': directory}})
            api = wm1303_api.WM1303API(daemon)
            with patch.object(wm1303_api, '_DB_PATH', str(root / 'repeater.db')), \
                    patch.object(wm1303_api, '_SPECTRAL_RES', spectrum_file), \
                    patch.object(spectrum_collector, 'JSON_PATH', str(spectrum_file)), \
                    patch.object(wm1303_api, 'get_collector', spectrum_collector.get_collector), \
                    patch.object(wm1303_api, '_load_ui', return_value={'region': {'code': 'EU868'}}), \
                    patch.object(wm1303_api, '_load_global_conf', return_value={}):
                try:
                    api.start()
                    recorder = wm1303_api._unified_rec_thread
                    collector = spectrum_collector.get_collector()
                    self.assertEqual(Path(collector.db_path), root / 'spectrum_history.db')
                    self.assertTrue(recorder.is_alive())
                finally:
                    api.stop()
                self.assertFalse(recorder.is_alive())
                self.assertFalse(collector._thread.is_alive())
                self.assertEqual(wm1303_api._shared_conn_instances, {})
                with patch.object(wm1303_api, '_COLLECTOR_AVAILABLE', False):
                    result = json.loads(api._do_spectrum_scan())
                    self.assertEqual(result['status'], 'unavailable')
                    self.assertEqual(result['scan_points'], [])
                    self.assertFalse(spectrum_file.exists())
                    native = json.dumps({'timestamp': 1234, 'channels': {'869000000': {'rssi_avg': -101.5}}})
                    spectrum_file.write_text(native)
                    measured = json.loads(api._do_spectrum_scan())
                    self.assertEqual(measured['scan_points'], [{'freq_mhz': 869.0, 'rssi_dbm': -101.5}])
                    self.assertEqual(measured['timestamp'], 1234)
                    self.assertEqual(spectrum_file.read_text(), native)

    def test_tiered_charts_preserve_widths_and_summary_only_channels(self):
        import sqlite3
        from repeater import metrics_retention
        metrics = {
            'packet_activity': {'total_rx_count': 7, 'total_tx_count': 2},
            'crc_error_rate': {'total_crc_errors': 2, 'total_crc_disabled': 0},
            'packet_metrics': {'direction': 'rx', 'total_bytes': 20, 'avg_hop_count': 3,
                               'avg_hop_count_count': 2, 'crc_error_count': 2},
            'noise_floor_history': {'avg_noise_floor_dbm': -105, 'min_noise_floor_dbm': -110,
                                    'max_noise_floor_dbm': -100, 'total_samples_collected': 2},
            'channel_stats_history': {'avg_rssi': -90, 'avg_snr': 5},
            'cad_events': {'total_cad_clear': 3, 'total_cad_detected': 1},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'repeater.db')
            conn = sqlite3.connect(path)
            try:
                # Keep the live table empty: discovery must include old tiers.
                conn.execute('CREATE TABLE noise_floor_history(timestamp REAL, channel_id TEXT, noise_floor_dbm REAL)')
                for cfg in metrics_retention.DOWNSAMPLE_TABLES:
                    if cfg['table'] not in metrics:
                        continue
                    for channel, suffix in (('channel_a', '15m'), ('channel_b', '10m')):
                        metrics_retention._create_summary_table(conn, cfg, suffix)
                        row = dict(bucket_ts=1_998_000, channel_id=channel, sample_count=2,
                                   **metrics[cfg['table']])
                        conn.execute(f"INSERT INTO {cfg['table']}_{suffix} ({','.join(row)}) "
                                     f"VALUES ({','.join('?' for _ in row)})", list(row.values()))
                conn.commit()
            finally:
                conn.close()
            ui = {'channels': [{'name': 'Chart A', 'active': False}, {'name': 'Chart B', 'active': True}]}
            with patch.object(wm1303_api, '_DB_PATH', path), \
                    patch.object(wm1303_api, '_load_ui', return_value=ui), \
                    patch.object(wm1303_api, '_get_backend', return_value=None), \
                    patch.object(wm1303_api.time, 'time', return_value=2_000_000):
                api = wm1303_api.WM1303API()
                try:
                    for method in (api._packet_activity, api._crc_error_rate, api._packet_metrics):
                        data = json.loads(method(hours='1'))
                        self.assertNotIn('error', data)
                        self.assertEqual(data['requested_bucket_seconds'], 60)
                        self.assertIsNone(data['bucket_seconds'])  # Different native widths per channel.
                        widths = {r['bucket_seconds'] for ch in data['channels'] for r in ch['data']}
                        self.assertEqual(widths, {600, 900})
                    self.assertEqual(data['channels'][0]['data'][0]['rx_crc_err_ratio'], 1.0)
                    signal = api.signal_quality(hours='1')['channels'][0]
                    self.assertEqual(signal['stats']['pkt_count'], 7)
                    self.assertEqual(signal['timeseries'][0]['pkt_count'], 7)
                    self.assertEqual(signal['timeseries'][0]['bucket_seconds'], 600)
                    history = json.loads(api.noise_floor_history(hours='1'))
                    self.assertEqual({r['channel_id'] for r in history['data']}, {'channel_a', 'channel_b'})
                    noise = json.loads(api._noise_floor_get(range='1h'))
                    self.assertEqual(noise['channels']['Chart B']['history'][0]['bucket_seconds'], 1800)
                    cad = json.loads(api._cad_stats_get(range='1h'))
                    self.assertEqual(set(cad['buckets']), {'channel_a', 'channel_b'})
                    self.assertEqual(cad['buckets']['channel_b'][0]['bucket_seconds'], 1800)
                finally:
                    api.stop()

    def test_e_f_save_share_validation_and_regenerate_without_a_d(self):
        from test_radio_bridge import backend_module
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ui_path = root / 'wm1303_ui.json'
            ui_path.write_text(json.dumps({'channels': [], 'region': {'code': 'EU868'}}))
            def write(target, content):
                # Keep the generated runtime /tmp copy isolated too.
                target = Path(target)
                wm1303_api.atomic_write_text(root / target.name, content)
            with patch.object(wm1303_api, '_UI_JSON', ui_path), \
                    patch.object(wm1303_api, '_PKTFWD_DIR', root), \
                    patch.object(wm1303_api, '_GLOBAL_CONF', root / 'global_conf.json'), \
                    patch.object(wm1303_api, '_safe_write', side_effect=write), \
                    patch.dict(sys.modules, {'openhop_core.hardware.wm1303_backend': backend_module}):
                api = wm1303_api.WM1303API()
                settings = dict(enabled=True, frequency=869618000, bandwidth=62500,
                                spreading_factor=8, coding_rate=8, lbt_rssi_target=-90)
                with patch.object(wm1303_api, '_body', return_value=settings):
                    result = json.loads(api._channel_e_post())
                self.assertEqual(result['status'], 'ok')
                conf = json.loads((root / 'global_conf.json').read_text())['SX130x_conf']
                self.assertEqual(conf['sx1261_conf']['lora_rx']['coding_rate'], 4)
                saved = ui_path.read_bytes()
                with patch.object(wm1303_api, '_body', return_value=dict(enabled=True, frequency=0)), \
                        self.assertRaises(wm1303_api.cherrypy.HTTPError):
                    api._channel_f_post()
                self.assertEqual(ui_path.read_bytes(), saved)

    def test_config_regeneration_does_not_start_a_competing_forwarder(self):
        generated = {"SX130x_conf": {"radio_0": {"freq": 869000000}}}
        backend = SimpleNamespace(_generate_bridge_conf=Mock(return_value=generated))
        with patch.dict(sys.modules, {"openhop_core.hardware.wm1303_backend": backend}), \
                patch.object(wm1303_api, "_load_ui", return_value={"channels": [{"name": "A", "active": True}]}), \
                patch.object(wm1303_api, "_safe_write") as write, \
                patch.object(wm1303_api.subprocess, "Popen") as popen:
            result = wm1303_api.sync_global_conf()
        self.assertEqual(result, {"status": "ok", "center_mhz": 869.0})
        self.assertEqual(write.call_count, 2)
        popen.assert_not_called()

    def test_failed_database_open_releases_lock_for_other_threads(self):
        shared = wm1303_api._SharedConn("unused.db")
        with patch.object(shared, "_ensure_conn", side_effect=OSError("unavailable")):
            with self.assertRaises(OSError):
                with shared:
                    self.fail("Database connection should not open")
        acquired = []

        def try_lock():
            locked = shared._lock.acquire(timeout=0.2)
            acquired.append(locked)
            if locked:
                shared._lock.release()

        worker = threading.Thread(target=try_lock)
        worker.start()
        worker.join(timeout=1)
        self.assertEqual(acquired, [True])

    def test_failed_database_setup_closes_partial_connection(self):
        connection = Mock()
        connection.execute.side_effect = OSError("read-only")
        shared = wm1303_api._SharedConn("unused.db")
        with patch("sqlite3.connect", return_value=connection):
            with self.assertRaises(OSError):
                shared._ensure_conn()
        connection.close.assert_called_once_with()
        self.assertIsNone(shared._conn)

    def test_incomplete_channel_requests_cannot_erase_configuration(self):
        api = wm1303_api.WM1303API()
        for body in (b'{', b'{}', b'null', b'{"channels":null}', b'{"channels":[null]}'):
            with self.subTest(body=body):
                with patch.object(wm1303_api.cherrypy.request, "body", io.BytesIO(body)), \
                        patch.object(wm1303_api, "_save_ui") as save:
                    with self.assertRaises(wm1303_api.cherrypy.HTTPError) as error:
                        api._channels_post()
                    self.assertEqual(error.exception.status, 400)
                    save.assert_not_called()

    def test_explicit_null_region_is_migrated(self):
        config, changed = wm1303_api._migrate_ui_config({"region": None})
        self.assertTrue(changed)
        self.assertIsInstance(config["region"], dict)

    def test_ui_save_reports_permission_failure(self):
        with patch.object(wm1303_api, "atomic_write_text", side_effect=PermissionError("read-only")):
            with patch.object(wm1303_api.subprocess, "run") as subprocess_run:
                with self.assertRaises(PermissionError):
                    wm1303_api._save_ui({"channels": []})
        subprocess_run.assert_not_called()

    def test_ui_save_can_be_read_back(self):
        with tempfile.TemporaryDirectory() as directory:
            filename = Path(directory) / "wm1303_ui.json"
            config = {"channels": [], "region": {"code": "EU868"}}
            with patch.object(wm1303_api, "_UI_JSON", filename):
                wm1303_api._save_ui(config)
                self.assertEqual(wm1303_api._load_ui(), config)

    def test_debug_collector_reads_configured_packet_forwarder_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            packet_forwarder = root / "custom-forwarder"
            packet_forwarder.mkdir()
            from repeater.atomic_file import atomic_write_text
            for name in ("bridge_conf.json", "global_conf.json"):
                atomic_write_text(packet_forwarder / name, json.dumps({"radio": "test", "password": "redact-me"}))
            collector = debug_collector.DebugCollector({"wm1303": {"pktfwd_dir": str(packet_forwarder)}})
            with patch.object(debug_collector, "resolve_config_path", side_effect=lambda name: root / name):
                collector._collect_wm1303_state(root / "bundle")
            for name in ("bridge_conf.json", "global_conf.json"):
                content = (root / "bundle" / "wm1303_state" / name).read_text()
                self.assertEqual(json.loads(content)["radio"], "test")
                self.assertNotIn("redact-me", content)


if __name__ == "__main__":
    unittest.main()
