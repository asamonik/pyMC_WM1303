"""Console compatibility without a radio or an upstream checkout."""

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
            self.assertIn("disabled:s.value,onClick:$},` Edit Settings `", policy.read_text())
            self.assertNotIn("disabled:!0", policy.read_text())
            self.assertNotIn("unavailable", policy.read_text())

    def test_pristine_policy_editor_is_accepted_without_disabling_controls(self):
        source = ";".join(after for _, after in console_assets.POLICY_REPLACEMENTS)
        self.assertEqual(console_assets.adapt_module(source, console_assets.POLICY_REPLACEMENTS), source)
        with tempfile.TemporaryDirectory() as directory:
            assets = Path(directory) / "assets"
            assets.mkdir()
            policy = assets / "Configuration-pristine.js"
            policy.write_text(source)
            self.assertEqual(console_assets.patch_console_assets(directory), 0)
            self.assertEqual(policy.read_text(), source)

    def test_missing_retry_measurements_display_as_unavailable(self):
        maximum_before, _ = console_assets.LBT_REPLACEMENTS[0]
        empty_variants, _ = console_assets.LBT_REPLACEMENTS[1]
        for empty_text in empty_variants:
            with self.subTest(empty_text=empty_text):
                source = maximum_before + ";" + empty_text
                result = console_assets.adapt_module(source, console_assets.LBT_REPLACEMENTS)
                self.assertIn("has_lbt_data?Y.value.max_attempts:`N/A`", result)
                self.assertIn("No measured CAD/LBT TX checks are available for this window.", result)
                self.assertNotIn("bridge records do not store retry attempts", result)
                self.assertEqual(result, console_assets.adapt_module(result, console_assets.LBT_REPLACEMENTS))

    def test_policy_page_loads_and_saves_a_supported_live_policy(self):
        from test_packet_policies import PolicyService, document, load_api, rule

        api = load_api()
        api._set_cors_headers = Mock()
        request = SimpleNamespace(method="GET", params={}, json=None)
        response = SimpleNamespace(status=200, headers={})
        with tempfile.TemporaryDirectory() as directory, patch.object(cherrypy, "request", request), patch.object(cherrypy, "response", response):
            service = PolicyService({}, str(Path(directory) / "config.yaml"), SimpleNamespace())
            api._get_packet_policy_service = lambda: service
            result = api.policy()
            self.assertTrue(result["success"])
            self.assertTrue(result["data"]["supported"])
            self.assertFalse(result["data"]["policy_engine"]["enabled"])
            self.assertEqual(result["data"]["groups"], {"channel_hashes": [], "pubkeys": []})
            request.method = "POST"
            request.json = document([rule()])
            self.assertTrue(api.policy_validate()["data"]["valid"])
            self.assertFalse(service.path.exists())
            saved = api.policy()
            self.assertTrue(saved["success"])
            self.assertTrue(saved["live_updated"])
            self.assertFalse(saved["restart_required"])
            self.assertEqual(service.evaluate(SimpleNamespace(), {"hop_count": 5}).action, "drop")
            self.assertTrue(service.path.exists())
            request.method = "OPTIONS"
            self.assertEqual(api.policy(), "")
            request.method = "DELETE"
            self.assertFalse(api.policy()["success"])
            self.assertEqual(response.status, 405)
