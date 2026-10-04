"""Loopback HTTP contracts with real CherryPy and the overlaid dependencies.

Run explicitly with the installed core/repeater on PYTHONPATH. All policy files,
SQLite outcomes, authentication fixtures, and HTTP sockets are test-local.
"""

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from wsgiref.simple_server import WSGIRequestHandler, make_server

import cherrypy
import yaml

from repeater.data_acquisition.sqlite_handler import SQLiteHandler
from repeater.web.api_endpoints import APIEndpoints
from repeater.web.auth.cherrypy_tool import register_require_auth_tool


class QuietRequestHandler(WSGIRequestHandler):
    def log_message(self, format, *args):
        pass


class AnalyticsHTTPTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)
        with patch.object(SQLiteHandler, "_start_wal_checkpoint_thread"):
            self.db = SQLiteHandler(self.path)
        self.addCleanup(self.db.close_thread_connection)
        self.config = {"radio_type": "wm1303", "web": {"cors_enabled": True},
                       "policy": {"policy_file": "policy.yaml"}}
        daemon = SimpleNamespace(config=self.config, bridge_engine=object(),
                                 repeater_handler=SimpleNamespace(storage=self.db))
        self.api = APIEndpoints(config=self.config, daemon_instance=daemon,
                                config_path=str(self.path / "config.yaml"))
        self.saved_config = dict(cherrypy.config)
        self.saved_apps = dict(cherrypy.tree.apps)
        self.addCleanup(self.restore_cherrypy)
        cherrypy.config.update({
            "log.screen": False,
            "jwt_handler": SimpleNamespace(verify_jwt=lambda token: {
                "sub": "http-fixture", "client_id": "fixture",
            } if token == "fixture-token" else None),
            "token_manager": SimpleNamespace(verify_token=lambda token: None),
        })
        register_require_auth_tool()
        cherrypy.tree.mount(self.api, "/api", {"/": {"tools.require_auth.on": True}})

        def application(environ, start_response):
            try:
                yield from cherrypy.tree(environ, start_response)
            finally:
                self.db.close_thread_connection()

        self.server = make_server("127.0.0.1", 0, application,
                                  handler_class=QuietRequestHandler)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def restore_cherrypy(self):
        cherrypy.config.clear()
        cherrypy.config.update(self.saved_config)
        cherrypy.tree.apps.clear()
        cherrypy.tree.apps.update(self.saved_apps)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.assertFalse(self.thread.is_alive())

    def request(self, path, method="GET", body=None, authenticated=True):
        headers = {"Authorization": "Bearer fixture-token"} if authenticated else {}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode("utf-8")
        req = Request(self.base + path, data=data, headers=headers, method=method)
        try:
            response = urlopen(req, timeout=3)
        except HTTPError as error:
            response = error
        with response:
            status, headers, raw = response.status, response.headers, response.read()
        payload = json.loads(raw) if headers.get_content_type() == "application/json" else raw
        return status, headers, payload

    def test_lbt_http_summary_errors_preflight_and_auth(self):
        record = dict(timestamp=time.time() - 60, channel_id="channel_e", packet_type=5,
                      ack_received=True, ok=True, cad_enabled=True, cad_detected=False,
                      cad_retries=0, cad_reason="clear", lbt_enabled=False,
                      scheduler_attempt=1)
        self.db.store_tx_diagnostic(record)
        self.db.store_tx_diagnostic(dict(record, cad_retries=2, cad_reason="cleared_after_retries"))
        self.db.store_tx_diagnostic(dict(record, ack_received=False, cad_detected=None,
                                         cad_retries=None, cad_reason=None))
        status, headers, result = self.request(
            "/api/lbt_diagnostics?hours=1&bucket_seconds=60&severe_attempt_threshold=3")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get_content_type(), "application/json")
        self.assertTrue(result["success"])
        data = result["data"]
        self.assertEqual(data["data_source"], "wm1303_tx_diagnostics")
        summary = data["summary"]
        self.assertEqual(summary["recorded_transmissions"], 3)
        self.assertEqual(summary["total_transmissions"], 2)
        self.assertEqual(summary["total_attempts"], 4)
        self.assertEqual(summary["unknown_transmissions"], 1)
        self.assertEqual(summary["retry_rate_pct"], 50)
        self.assertEqual(summary["busy_channel_events"], 1)
        self.assertEqual(summary["max_attempts"], 3)
        self.assertEqual(data["packet_types"][0]["packet_type"], 5)
        self.assertIsNone(data["buckets"][0]["rf"]["avg_snr"])
        status, _, result = self.request("/api/lbt_diagnostics?hours=invalid")
        self.assertEqual(status, 200)  # Existing analytics error envelope contract.
        self.assertFalse(result["success"])
        self.assertIn("Invalid parameter format", result["error"])
        status, headers, result = self.request("/api/lbt_diagnostics", "OPTIONS", authenticated=False)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Access-Control-Allow-Origin"], "*")
        self.assertEqual(result, "")
        self.assertEqual(self.request("/api/lbt_diagnostics", authenticated=False)[0], 401)

    def test_policy_http_read_validate_save_and_group_json_bodies(self):
        status, _, result = self.request("/api/policy")
        self.assertEqual(status, 200)
        self.assertTrue(result["data"]["supported"])
        self.assertFalse(result["data"]["policy_engine"]["enabled"])
        draft = {"policy_engine": {"enabled": True, "default_action": "drop",
                                    "rules": [], "objects": {}}}
        status, _, result = self.request("/api/policy_validate", "POST", draft)
        self.assertEqual(status, 200)
        self.assertTrue(result["data"]["valid"])
        self.assertFalse((self.path / "policy.yaml").exists())
        status, _, result = self.request("/api/policy", "POST", draft)
        self.assertEqual(status, 200)
        self.assertTrue(result["success"])
        self.assertTrue(result["saved"])
        self.assertTrue(result["live_updated"])
        self.assertEqual(self.config["policy_engine"]["default_action"], "drop")
        with (self.path / "policy.yaml").open() as stream:
            self.assertTrue(yaml.safe_load(stream)["policy_engine"]["enabled"])
        group = dict(kind="channel_hashes", group_id="local_channels", friendly_name="Local Channels")
        status, _, result = self.request("/api/policy_groups", "POST", group)
        self.assertEqual(status, 200)
        self.assertEqual(result["data"]["group_id"], "local_channels")
        entry = dict(kind="channel_hashes", group_id="local_channels", entry_id="mesh",
                     friendly_name="Mesh", value="0xab")
        status, _, result = self.request("/api/policy_group_entries", "POST", entry)
        self.assertEqual(status, 200)
        self.assertEqual(result["data"]["entry"]["value"].lower(), "0xab")
        status, _, result = self.request("/api/policy_groups?kind=channel_hashes")
        self.assertEqual(status, 200)
        self.assertEqual(result["data"]["groups"][0]["id"], "local_channels")
        status, _, result = self.request(
            "/api/policy_group_entries?kind=channel_hashes&group_id=local_channels")
        self.assertEqual(status, 200)
        self.assertEqual(len(result["data"]["entries"]), 1)
        status, _, result = self.request("/api/policy_group_entries", "DELETE", {
            "kind": "channel_hashes", "group_id": "local_channels", "entry_id": "mesh",
        })
        self.assertEqual(status, 200)
        self.assertTrue(result["success"])
        status, _, result = self.request("/api/policy_groups", "DELETE", {
            "kind": "channel_hashes", "group_id": "local_channels",
        })
        self.assertEqual(status, 200)
        self.assertTrue(result["success"])

    def test_policy_http_error_and_query_delete_contracts(self):
        status, _, result = self.request("/api/policy", "POST", [])
        self.assertEqual(status, 400)
        self.assertFalse(result["success"])
        self.assertIn("JSON object", result["error"])
        status, _, result = self.request("/api/policy_groups?kind=invalid")
        self.assertEqual(status, 400)
        self.assertFalse(result["success"])
        status, headers, result = self.request("/api/policy", "PUT", {})
        self.assertEqual(status, 405)
        self.assertEqual(headers["Allow"], "GET, POST, OPTIONS")
        self.assertFalse(result["success"])
        self.assertEqual(self.request("/api/policy", authenticated=False)[0], 401)
        status, _, result = self.request("/api/policy_groups", "POST", {
            "kind": "pubkeys", "group_id": "neighbors", "friendly_name": "Neighbors",
        })
        self.assertEqual(status, 200)
        status, _, result = self.request(
            "/api/policy_groups?kind=pubkeys&group_id=neighbors", "DELETE")
        self.assertEqual(status, 200)
        self.assertTrue(result["success"])


if __name__ == "__main__":
    unittest.main()
