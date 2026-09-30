"""Monitoring discovery API contracts without installed upstream packages."""

import ast
import json
import math
from pathlib import Path
import re
import sys
import tempfile
import time
from types import SimpleNamespace
from typing import Optional
import unittest
from unittest.mock import Mock, patch

import cherrypy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "overlay/pymc_repeater"))


def load_api():
    source = ROOT / "overlay/pymc_repeater/repeater/web/api_endpoints.py"
    cls = next(n for n in ast.parse(source.read_text()).body
               if isinstance(n, ast.ClassDef) and n.name == "APIEndpoints")
    methods = {
        "_success", "_error", "_require_post", "_fmt_hash", "_encode_sse_event",
        "_normalize_discovery_node_name", "_enrich_discovery_result",
        "_get_local_pubkey_hex", "_is_local_discovery_pubkey",
        "discover_neighbors_start", "discover_neighbors_stream", "add_discovered_neighbor",
    }
    cls.body = [n for n in cls.body if
                isinstance(n, ast.FunctionDef) and n.name in methods or
                isinstance(n, ast.Assign) and
                "discover_neighbors_stream._cp_config" in ast.unparse(n)]
    namespace = dict(cherrypy=cherrypy, json=json, math=math, re=re, time=time,
                     Optional=Optional, logger=Mock(), require_auth=lambda fn: fn)
    exec(compile(ast.Module(body=[cls], type_ignores=[]), str(source), "exec"), namespace)
    return namespace["APIEndpoints"]()


class NeighborDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.api = load_api()
        self.api.config = {"mesh": {"path_hash_mode": 1}}
        self.api._set_cors_headers = Mock()
        self.api.event_loop = Mock(is_running=Mock(return_value=True))
        self.helper = Mock()
        self.helper.create_session.return_value = {"session_id": "scan", "tag": 123,
                                                   "status": "created", "timeout": 5}
        self.local_key = bytes(range(32))
        self.api.daemon_instance = SimpleNamespace(
            discovery_helper=self.helper,
            local_identity=SimpleNamespace(get_public_key=lambda: self.local_key))
        self.storage = SimpleNamespace(get_neighbors=Mock(return_value={}),
                                       record_advert=Mock(return_value=True))
        self.api._get_storage = lambda: self.storage
        self.request = SimpleNamespace(method="POST", json={}, headers={})
        self.response = SimpleNamespace(headers={})
        self.enterContext(patch.object(cherrypy, "request", self.request))
        self.enterContext(patch.object(cherrypy, "response", self.response))

    def test_start_matches_console_request_and_schedules_on_daemon_loop(self):
        # The Vue console calls this exact route with a five-second repeater scan.
        self.request.json = dict(timeout=5, filter_mask=4, since=0, prefix_only=False)
        result = self.api.discover_neighbors_start()
        self.assertTrue(result["success"], result)
        self.assertEqual(result["data"]["session_id"], "scan")
        self.helper.cleanup_sessions.assert_called_once_with()
        self.helper.create_session.assert_called_once_with(
            timeout=5, filter_mask=4, since=0, prefix_only=False,
            result_enricher=self.api._enrich_discovery_result)
        self.api.event_loop.call_soon_threadsafe.assert_called_once_with(
            self.helper.start_session_task, "scan")
        for name in ("discover_neighbors_start", "discover_neighbors_stream", "add_discovered_neighbor"):
            self.assertTrue(getattr(self.api, name).exposed)
        self.assertTrue(self.api.discover_neighbors_stream._cp_config["response.stream"])

    def test_start_rejects_invalid_parameters_and_unavailable_runtime(self):
        for values in ({"timeout": 0}, {"timeout": 61}, {"timeout": "nan"},
                       {"timeout": "inf"}, {"filter_mask": -1}, {"filter_mask": 256},
                       {"since": -1}, {"since": 1 << 32}, {"prefix_only": "false"}):
            with self.subTest(values=values):
                self.request.json = values
                self.assertFalse(self.api.discover_neighbors_start()["success"])
        self.helper.create_session.assert_not_called()
        self.request.json = {}
        self.api.event_loop.is_running.return_value = False
        self.assertIn("Event loop", self.api.discover_neighbors_start()["error"])
        self.api.event_loop.is_running.return_value = True
        self.api.daemon_instance.discovery_helper = None
        self.assertIn("Discovery helper", self.api.discover_neighbors_start()["error"])
        self.request.method = "GET"
        with self.assertRaises(cherrypy.HTTPError) as caught:
            self.api.discover_neighbors_start()
        self.assertEqual(caught.exception.status, 405)
        self.request.method = "OPTIONS"
        self.assertEqual(self.api.discover_neighbors_start(), "")
        self.assertEqual(self.api.discover_neighbors_stream(), "")

    def test_sse_delivers_results_completion_errors_and_reconnect_cursor(self):
        self.request.method = "GET"
        self.helper.get_session_snapshot.return_value = {"session_id": "scan", "status": "running"}
        events = [
            dict(id=1, event="started", data={"session_id": "scan"}),
            dict(id=2, event="discovery_result", data={"result": {"pub_key": "ab" * 32}, "count": 1}),
            dict(id=3, event="completed", data={"status": "completed", "count": 1}),
        ]
        self.helper.get_events_since.side_effect = lambda session, cursor: {
            "events": [event for event in events if event["id"] > cursor], "completed": True}
        chunks = list(self.api.discover_neighbors_stream("scan"))
        self.assertEqual([chunk.splitlines()[0] for chunk in chunks],
                         ["event: connected", "event: started", "event: discovery_result", "event: completed"])
        payload = json.loads(chunks[2].split("data: ")[1])
        self.assertEqual(payload["result"]["pub_key"], "ab" * 32)
        self.assertEqual(self.response.headers["Content-Type"], "text/event-stream")
        self.request.headers["Last-Event-ID"] = "2"
        chunks = list(self.api.discover_neighbors_stream("scan"))
        self.assertEqual(len(chunks), 2)
        self.assertIn("id: 3", chunks[1])
        chunks = list(self.api.discover_neighbors_stream("scan", last_event_id="1"))
        self.assertIn("id: 2", chunks[1])
        self.helper.get_session_snapshot.return_value = None
        self.assertIn("event: error", next(self.api.discover_neighbors_stream("missing")))
        self.assertIn("Missing session_id", next(self.api.discover_neighbors_stream()))
        self.helper.get_session_snapshot.return_value = {"status": "error"}
        self.helper.get_events_since.side_effect = None
        self.helper.get_events_since.return_value = {
            "events": [dict(id=4, event="error", data={"error": "Failed to send discovery request"})],
            "completed": True}
        self.assertIn("Failed to send discovery request", list(self.api.discover_neighbors_stream("scan"))[-1])

    def test_add_persists_result_without_erasing_known_metadata_or_adding_self(self):
        from repeater.data_acquisition.sqlite_handler import SQLiteHandler

        directory = self.enterContext(tempfile.TemporaryDirectory())
        with patch.object(SQLiteHandler, "_start_wal_checkpoint_thread"):
            db = SQLiteHandler(Path(directory))
        self.addCleanup(db.close_thread_connection)
        self.storage.get_neighbors = db.get_neighbors
        self.storage.record_advert.side_effect = db.store_advert
        key = "ab" * 32
        self.request.json = dict(pub_key=key, node_name="Unknown", node_type=2,
                                 rssi=-83, response_snr=7.25)
        result = self.api.add_discovered_neighbor()
        self.assertTrue(result["success"], result)
        self.assertTrue(result["data"]["known_neighbor"])
        row = db.get_neighbors()[key]
        self.assertEqual((row["contact_type"], row["rssi"], row["snr"], row["zero_hop"]),
                         ("Repeater", -83, 7.25, True))
        self.assertIsNone(row["node_name"])
        db.store_advert(dict(pubkey=key, node_name="Hilltop", latitude=48.2, longitude=16.3,
                             contact_type="Repeater", zero_hop=True, rssi=-80, snr=8))
        self.assertTrue(self.api.add_discovered_neighbor()["success"])
        self.assertEqual(db.get_neighbors()[key]["latitude"], 48.2)
        self.assertEqual(db.get_neighbors()[key]["node_name"], "Hilltop")
        for local in (self.local_key.hex(), self.local_key[:8].hex()):
            self.request.json["pub_key"] = local
            self.assertTrue(self.api.add_discovered_neighbor()["data"]["is_self"])
        self.storage.record_advert.assert_called_once()
        self.request.json["pub_key"] = "not a key"
        self.assertFalse(self.api.add_discovered_neighbor()["success"])
        self.request.json["pub_key"] = "cd" * 32
        self.storage.record_advert.side_effect = None
        self.storage.record_advert.return_value = False
        self.assertIn("write queue full", self.api.add_discovered_neighbor()["error"])

    def test_enrichment_is_json_serializable_and_uses_public_identity(self):
        key = "ab" * 32
        self.storage.get_neighbors.return_value = {key: {"node_name": "Hilltop", "zero_hop": True}}
        result = self.api._enrich_discovery_result({"pub_key": key, "pub_key_bytes": bytes.fromhex(key)})
        self.assertEqual(result["node_hash"], "0xABAB")
        self.assertEqual(result["node_name"], "Hilltop")
        self.assertTrue(result["known_neighbor"])
        json.dumps(result)
        self.api.config["repeater"] = {"identity_key": "cd" * 64}
        self.api.daemon_instance.local_identity = None
        self.assertFalse(self.api._is_local_discovery_pubkey("cd" * 8))


if __name__ == "__main__":
    unittest.main()
