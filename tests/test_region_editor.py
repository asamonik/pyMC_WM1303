"""Region editing preserves a valid tree, wire keys and accurate last-heard data."""

import ast
import base64
from datetime import datetime
import hashlib
import math
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import cherrypy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'overlay/pymc_repeater'))
from repeater.data_acquisition.sqlite_handler import SQLiteHandler


class RegionEditorTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with patch('threading.Thread.start'):
            self.db = SQLiteHandler(Path(tmp.name))
        self.addCleanup(self.db.close_thread_connection)
        self.custom_key = base64.b64encode(b'k' * 16).decode()
        source = ROOT / 'overlay/pymc_repeater/repeater/web/api_endpoints.py'
        cls = next(n for n in ast.parse(source.read_text()).body
                   if isinstance(n, ast.ClassDef) and n.name == 'APIEndpoints')
        names = {'_success', '_error', '_region_error', '_invalidate_region_cache',
                 '_region_last_used', 'transport_keys', 'transport_key'}
        cls.body = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in names]
        for method in cls.body:
            # Keep @staticmethod, remove CherryPy request wrappers.
            method.decorator_list = [d for d in method.decorator_list
                                     if isinstance(d, ast.Name) and d.id == 'staticmethod']
        namespace = dict(cherrypy=cherrypy, datetime=datetime, math=math, logger=Mock())
        exec(compile(ast.Module(body=[cls], type_ignores=[]), str(source), 'exec'), namespace)
        self.api = namespace['APIEndpoints']()
        self.api._get_storage = lambda: self.db
        self.api.config = {'mesh': {}}
        self.engine = SimpleNamespace(_transport_keys_cache=['old snapshot'], _transport_keys_cache_time=123)
        self.api.daemon_instance = SimpleNamespace(repeater_handler=self.engine)
        self.request = SimpleNamespace(method='POST', json={})
        request_patch = patch.object(cherrypy, 'request', self.request)
        request_patch.start()
        self.addCleanup(request_patch.stop)
        response_patch = patch.object(cherrypy, 'response', SimpleNamespace(status=200))
        response_patch.start()
        self.addCleanup(response_patch.stop)

    def create(self, name, parent=None):
        result = self.db.create_transport_key(name, 'allow', self.custom_key, parent)
        self.assertIsNotNone(result)
        return result

    def test_parent_can_be_cleared_and_deleted_without_orphans(self):
        parent = self.create('parent')
        child = self.create('child', parent)
        self.request.method, self.request.json = 'PUT', {'parent_id': None}
        self.assertTrue(self.api.transport_key(child)['success'])
        self.assertIsNone(self.db.get_transport_key_by_id(child)['parent_id'])
        self.assertEqual(self.engine._transport_keys_cache_time, 0)
        self.assertEqual(self.engine._transport_keys_cache, ['old snapshot'])
        self.assertTrue(self.db.update_transport_key(child, parent_id=parent))
        self.assertTrue(self.db.delete_transport_key(parent))
        self.assertIsNone(self.db.get_transport_key_by_id(child)['parent_id'])

    def test_cycles_and_missing_parents_cannot_be_saved(self):
        parent = self.create('parent')
        child = self.create('child', parent)
        with self.assertLogs('SQLiteHandler', level='ERROR'):
            self.assertFalse(self.db.update_transport_key(parent, parent_id=child))
            self.assertIsNone(self.db.create_transport_key('missing', 'allow', self.custom_key, 999))
        self.assertIsNone(self.db.get_transport_key_by_id(parent)['parent_id'])

    def test_last_heard_is_unknown_on_creation_and_preserves_numeric_updates(self):
        self.request.json = {'name': 'new region', 'flood_policy': 'allow', 'transport_key': self.custom_key}
        result = self.api.transport_keys()
        self.assertTrue(result['success'])
        key_id = result['data']['id']
        self.assertIsNone(self.db.get_transport_key_by_id(key_id)['last_used'])
        self.request.method = 'PUT'
        for value, expected in ((123.5, 123.5), ('2026-01-01T00:00:00Z', 1767225600), (0, 0)):
            self.request.json = {'last_used': value}
            self.assertTrue(self.api.transport_key(key_id)['success'])
            self.assertEqual(self.db.get_transport_key_by_id(key_id)['last_used'], expected)

    def test_invalid_save_is_an_http_error_and_default_region_stays_usable(self):
        key_id = self.create('at')
        self.api.config['mesh']['default_region'] = 'at'
        self.request.method = 'PUT'
        for data in ({'name': 'renamed'}, {'flood_policy': 'deny'}, {'flood_policy': ''}):
            self.request.json = data
            self.assertFalse(self.api.transport_key(key_id)['success'])
            self.assertEqual(cherrypy.response.status, 400)
        self.request.method = 'DELETE'
        self.assertFalse(self.api.transport_key(key_id)['success'])
        self.assertEqual(self.db.get_transport_key_by_id(key_id)['name'], 'at')
        self.assertEqual(self.db.get_transport_key_by_id(key_id)['flood_policy'], 'allow')

    def test_auto_region_rename_updates_key_and_custom_key_is_retained(self):
        def derive(name):
            if not name.isascii():
                raise ValueError('Public region names must be ASCII')
            return hashlib.sha256(('#' + name).encode()).digest()[:16]
        with patch.dict(sys.modules, {'openhop_core.protocol.transport_keys': SimpleNamespace(get_auto_key_for=derive)}):
            auto = self.db.create_transport_key('old', 'allow')
            old_key = self.db.get_transport_key_by_id(auto)['transport_key']
            # The editor sends the previous key while changing the display name.
            self.assertTrue(self.db.update_transport_key(auto, name='new', transport_key=old_key))
            self.assertEqual(self.db.get_transport_key_by_id(auto)['transport_key'],
                             base64.b64encode(derive('new')).decode())
            custom = self.create('custom')
            self.assertTrue(self.db.update_transport_key(custom, name='custom renamed'))
            self.assertEqual(self.db.get_transport_key_by_id(custom)['transport_key'], self.custom_key)
            with self.assertLogs('SQLiteHandler', level='ERROR'):
                self.assertIsNone(self.db.create_transport_key('ö', 'allow'))
                self.assertIsNone(self.db.create_transport_key('bad key', 'allow', 'c2hvcnQ='))


if __name__ == '__main__':
    unittest.main()
