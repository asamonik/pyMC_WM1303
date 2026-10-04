"""Repeater MeshCore guest passwords save transactionally without exposing secrets."""

import ast
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import cherrypy
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "overlay/pymc_repeater"))
sys.path.insert(0, str(ROOT / "overlay/pymc_core/src"))

from repeater.config_manager import ConfigManager


class MeshPasswordTests(unittest.TestCase):
    def setUp(self):
        source = ROOT / "overlay/pymc_repeater/repeater/web/api_endpoints.py"
        definition = next(node for node in ast.parse(source.read_text()).body
                          if isinstance(node, ast.ClassDef) and node.name == "APIEndpoints")
        definition.body = [node for node in definition.body
                           if isinstance(node, ast.FunctionDef)
                           and node.name in {"_success", "_error", "_require_post", "repeater_security"}]
        for method in definition.body:
            method.decorator_list = []
        namespace = {"cherrypy": cherrypy, "logger": Mock()}
        exec(compile(ast.Module(body=[definition], type_ignores=[]), str(source), "exec"), namespace)
        self.api = namespace["APIEndpoints"]()
        self.api._set_cors_headers = lambda: None
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "config.yaml"
        self.config = {
            "repeater": {"node_name": "fixture", "security": {
                "admin_password": "admin-current", "guest_password": "guest-old",
                "jwt_secret": "private-jwt", "allow_read_only": False, "max_clients": 3}},
            "radio": {"frequency": 869525000},
        }
        self.acl = SimpleNamespace(admin_password="admin-current", guest_password="guest-old", clients={"existing": 2})

        def refresh(config):
            security = config["repeater"]["security"]
            self.acl.admin_password, self.acl.guest_password = security["admin_password"], security["guest_password"]
            return True

        self.refresh = Mock(side_effect=refresh)
        self.daemon = SimpleNamespace(config=self.config, login_helper=SimpleNamespace(refresh_repeater_security=self.refresh))
        self.manager = ConfigManager(str(self.path), self.config, self.daemon)
        self.assertTrue(self.manager.save_to_file())
        self.api.config, self.api.config_manager = self.config, self.manager
        self.request = SimpleNamespace(method="POST", json={"current_password": "admin-current", "guest_password": "guest-new"})
        self.response = SimpleNamespace(status=200, headers={})
        request_patch = patch.object(cherrypy, "request", self.request)
        response_patch = patch.object(cherrypy, "response", self.response)
        request_patch.start()
        response_patch.start()
        self.addCleanup(request_patch.stop)
        self.addCleanup(response_patch.stop)

    def test_status_returns_no_passwords_and_read_does_not_mutate(self):
        self.request.method = "GET"
        before = self.path.read_bytes()
        result = self.api.repeater_security()
        self.assertEqual(result, {"success": True, "data": {
            "has_admin_password": True, "has_guest_password": True,
            "admin_password_shared_with_console": True, "max_password_bytes": 15}})
        self.assertEqual(self.path.read_bytes(), before)
        self.refresh.assert_not_called()

    def test_save_preserves_saved_manual_edits_and_refreshes_live_acl(self):
        saved = yaml.safe_load(self.path.read_text())
        saved["repeater"]["security"]["jwt_secret"] = "staged-jwt"
        saved["radio"]["frequency"] = 868000000
        self.path.write_text(yaml.safe_dump(saved))
        result = self.api.repeater_security()
        self.assertTrue(result["success"], result)
        self.assertTrue(result["saved"])
        self.assertTrue(result["live_updated"])
        self.assertFalse(result["restart_required"])
        self.assertEqual(self.acl.guest_password, "guest-new")
        self.assertEqual(self.acl.clients, {"existing": 2})
        self.assertEqual(self.acl.admin_password, "admin-current")
        self.assertEqual(self.config["repeater"]["security"]["jwt_secret"], "private-jwt")
        self.assertFalse(self.config["repeater"]["security"]["allow_read_only"])
        self.assertEqual(self.config["radio"]["frequency"], 869525000)
        saved["repeater"]["security"]["guest_password"] = "guest-new"
        self.assertEqual(yaml.safe_load(self.path.read_text()), saved)
        self.assertNotIn("guest-new", str(result))

    def test_failed_persistence_leaves_config_and_acl_unchanged(self):
        before, disk = deepcopy(self.config), self.path.read_bytes()
        with patch.object(self.manager, "save_to_file", return_value=False):
            result = self.api.repeater_security()
        self.assertFalse(result["success"])
        self.assertEqual(self.config, before)
        self.assertEqual(self.path.read_bytes(), disk)
        self.assertEqual(self.acl.guest_password, "guest-old")
        self.refresh.assert_not_called()

    def test_live_failure_keeps_durable_save_and_reports_restart(self):
        self.refresh.side_effect = None
        self.refresh.return_value = False
        result = self.api.repeater_security()
        self.assertTrue(result["success"], result)
        self.assertTrue(result["saved"])
        self.assertFalse(result["live_updated"])
        self.assertTrue(result["restart_required"])
        self.assertEqual(yaml.safe_load(self.path.read_text())["repeater"]["security"]["guest_password"], "guest-new")

    def test_empty_password_explicitly_disables_guest_password_login(self):
        self.request.json["guest_password"] = ""
        result = self.api.repeater_security()
        self.assertTrue(result["success"], result)
        self.assertFalse(result["data"]["has_guest_password"])
        self.assertEqual(self.acl.guest_password, "")
        self.assertFalse(self.config["repeater"]["security"]["allow_read_only"])

    def test_invalid_inputs_and_current_password_cannot_change_credentials(self):
        original, disk = deepcopy(self.config), self.path.read_bytes()
        cases = [None, [], {"current_password": "admin-current"},
                 {"current_password": "admin-current", "guest_password": None},
                 {"current_password": "admin-current", "guest_password": 123},
                 {"current_password": "admin-current", "guest_password": "x" * 16},
                 {"current_password": "admin-current", "guest_password": "é" * 8},
                 {"current_password": "admin-current", "guest_password": "guest\x00pw"},
                 {"current_password": "admin-current", "guest_password": "\ud800"},
                 {"current_password": "wrong", "guest_password": "guest-new"},
                 {"current_password": None, "guest_password": "guest-new"},
                 {"current_password": "", "guest_password": "guest-new"},
                 {"current_password": "admin-current", "guest_password": "admin-current"}]
        for data in cases:
            with self.subTest(data=data):
                self.request.json = data
                self.assertFalse(self.api.repeater_security()["success"])
                self.assertEqual(self.config, original)
                self.assertEqual(self.path.read_bytes(), disk)
        self.refresh.assert_not_called()
        self.request.json = {"current_password": "admin-current", "guest_password": "é" * 7 + "a"}
        self.assertTrue(self.api.repeater_security()["success"])

    def test_guest_cannot_match_staged_admin_password(self):
        saved = yaml.safe_load(self.path.read_text())
        saved["repeater"]["security"]["admin_password"] = "future-admin"
        self.path.write_text(yaml.safe_dump(saved))
        before = self.path.read_bytes()
        self.request.json["guest_password"] = "future-admin"
        self.assertFalse(self.api.repeater_security()["success"])
        self.assertEqual(self.path.read_bytes(), before)
        self.refresh.assert_not_called()

    def test_only_get_post_and_options_are_allowed(self):
        self.request.method = "PUT"
        with self.assertRaises(cherrypy.HTTPError) as failure:
            self.api.repeater_security()
        self.assertEqual(failure.exception.status, 405)
        self.request.method = "OPTIONS"
        self.assertEqual(self.api.repeater_security(), "")
        self.refresh.assert_not_called()


if __name__ == "__main__":
    unittest.main()
