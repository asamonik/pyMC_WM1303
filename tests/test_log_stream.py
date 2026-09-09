"""Live logs match the console's named SSE events and reconnect cursor."""

import ast
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import logging
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import cherrypy

ROOT = Path(__file__).resolve().parents[1]


class LogStreamTests(unittest.TestCase):
    def test_concurrent_log_writes_snapshot_and_stream_reconnection(self):
        source = ROOT / 'overlay/pymc_repeater/repeater/web/http_server.py'
        cls = next(n for n in ast.parse(source.read_text()).body
                   if isinstance(n, ast.ClassDef) and n.name == 'LogBuffer')
        namespace = dict(logging=logging, deque=deque, datetime=datetime, time=time)
        exec(compile(ast.Module(body=[cls], type_ignores=[]), str(source), 'exec'), namespace)
        buffer = namespace['LogBuffer'](max_lines=50)
        def emit(number):
            buffer.handle(logging.LogRecord('fixture', logging.INFO, __file__, 1, 'entry %d', (number,), None))
            buffer.snapshot()
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(emit, range(200)))
        rows = buffer.snapshot()
        self.assertEqual(len(rows), 50)
        ids = [r['id'] for r in rows]
        self.assertEqual(ids, sorted(set(ids)))
        rows[-1]['message'] = 'must not modify buffer'
        self.assertNotEqual(buffer.snapshot()[-1]['message'], rows[-1]['message'])

        source = ROOT / 'overlay/pymc_repeater/repeater/web/api_endpoints.py'
        cls = next(n for n in ast.parse(source.read_text()).body
                   if isinstance(n, ast.ClassDef) and n.name == 'APIEndpoints')
        methods = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in ('logs', 'logs_stream')]
        for fn in methods:
            fn.decorator_list = []
        namespace = dict(__package__='repeater.web', cherrypy=cherrypy, json=json,
                         time=SimpleNamespace(sleep=Mock()), logger=Mock(), datetime=datetime)
        exec(compile(ast.Module(body=methods, type_ignores=[]), str(source), 'exec'), namespace)
        imports = {'repeater.web.http_server': SimpleNamespace(_log_buffer=buffer)}
        with patch.dict(sys.modules, imports), patch.object(cherrypy, 'response', SimpleNamespace(headers={})):
            self.assertEqual(len(namespace['logs'](None)['logs']), 50)
            stream = namespace['logs_stream'](None, since_id=ids[-2])
            connected = next(stream)
            self.assertIn('event: connected', connected)
            self.assertIn(str(ids[-1]), connected)
            entry = next(stream)
            self.assertIn('event: log', entry)
            self.assertEqual(json.loads(entry.split('data: ')[1])['entry']['id'], ids[-1])
            self.assertIn('event: keepalive', next(stream))
            emit(201)
            self.assertIn('entry 201', next(stream))
            self.assertIn('event: keepalive', next(stream))
            stream.close()
            with self.assertRaises(StopIteration):
                next(stream)
            self.assertEqual(cherrypy.response.headers['Content-Type'], 'text/event-stream')
            with self.assertRaises(cherrypy.HTTPError):
                namespace['logs_stream'](None, since_id='invalid')


if __name__ == '__main__':
    unittest.main()
