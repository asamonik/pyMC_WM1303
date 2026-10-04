"""Console compatibility without a radio or an upstream checkout."""

import ast
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

import cherrypy


ROOT = Path(__file__).resolve().parents[1]
PATCHER = ROOT / "overlay/pymc_repeater/repeater/web/console_assets.py"
spec = importlib.util.spec_from_file_location("console_assets", PATCHER)
console_assets = importlib.util.module_from_spec(spec)
spec.loader.exec_module(console_assets)


class ConsoleViewTests(unittest.TestCase):
    def test_named_table_chart_search_and_sort_preserve_peer_identity(self):
        source = ";".join(before for before, _ in console_assets.NEIGHBOR_REPLACEMENTS)
        source += ";Y(e.peer_hash);getHistory({peer_hash:e.peer_hash})"
        result = console_assets.adapt_module(source, console_assets.NEIGHBOR_REPLACEMENTS)
        for _, after in console_assets.NEIGHBOR_REPLACEMENTS:
            self.assertIn(after, result)
        self.assertIn("Y(e.peer_hash);getHistory({peer_hash:e.peer_hash})", result)
        self.assertEqual(result, console_assets.adapt_module(result, console_assets.NEIGHBOR_REPLACEMENTS))

    def test_deployment_is_idempotent_and_detects_upstream_changes_before_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            assets = Path(directory) / "assets"
            assets.mkdir()
            neighbor = assets / "NeighbourLinks-example.js"
            neighbor_source = ";".join(before for before, _ in console_assets.NEIGHBOR_REPLACEMENTS)
            neighbor.write_text(neighbor_source)
            neighbor.chmod(0o644)
            policy = assets / "Configuration-example.js"
            policy.write_text("unexpected upstream build")
            with self.assertRaises(ValueError):
                console_assets.patch_console_assets(directory)
            self.assertEqual(neighbor.read_text(), neighbor_source)
            policy.write_text(";".join(before for before, _ in console_assets.POLICY_REPLACEMENTS))
            self.assertEqual(console_assets.patch_console_assets(directory), 2)
            self.assertEqual(neighbor.stat().st_mode & 0o777, 0o644)
            self.assertEqual(console_assets.patch_console_assets(directory), 0)
            self.assertIn("disabled:!0", policy.read_text())
            self.assertIn("unavailable", policy.read_text())

    def test_missing_retry_measurements_display_as_unavailable(self):
        source = ";".join(before for before, _ in console_assets.LBT_REPLACEMENTS)
        result = console_assets.adapt_module(source, console_assets.LBT_REPLACEMENTS)
        self.assertIn("has_lbt_data?Y.value.max_attempts:`N/A`", result)
        self.assertIn("WM1303 bridge records do not store retry attempts", result)
        self.assertEqual(result, console_assets.adapt_module(result, console_assets.LBT_REPLACEMENTS))

    def test_policy_page_loads_an_explicit_disabled_capability(self):
        source = ROOT / "overlay/pymc_repeater/repeater/web/api_endpoints.py"
        cls = next(node for node in ast.parse(source.read_text()).body
                   if isinstance(node, ast.ClassDef) and node.name == "APIEndpoints")
        cls.body = [node for node in cls.body if isinstance(node, ast.FunctionDef)
                    and node.name in ("_success", "_error", "policy")]
        for method in cls.body:
            method.decorator_list = []
        namespace = {"cherrypy": cherrypy}
        exec(compile(ast.Module(body=[cls], type_ignores=[]), str(source), "exec"), namespace)
        api = namespace["APIEndpoints"]()
        api._set_cors_headers = Mock()
        request = SimpleNamespace(method="GET")
        response = SimpleNamespace(status=200, headers={})
        with patch.object(cherrypy, "request", request), patch.object(cherrypy, "response", response):
            result = api.policy()
            self.assertTrue(result["success"])
            self.assertFalse(result["data"]["supported"])
            self.assertFalse(result["data"]["policy_engine"]["enabled"])
            self.assertEqual(result["data"]["groups"], {"channel_hashes": [], "pubkeys": []})
            request.method = "POST"
            self.assertFalse(api.policy()["success"])
            self.assertEqual(response.status, 501)
            request.method = "OPTIONS"
            self.assertEqual(api.policy(), "")
            request.method = "DELETE"
            self.assertFalse(api.policy()["success"])
            self.assertEqual(response.status, 405)
