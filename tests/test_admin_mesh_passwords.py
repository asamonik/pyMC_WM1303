"""The shared Console/MeshCore admin password keeps separate roles usable."""

import ast
from copy import deepcopy
import io
import json
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

from repeater.config_manager import ConfigManager


class AdminMeshPasswordTests(unittest.TestCase):
    def setUp(self):
        source = ROOT / "overlay/pymc_repeater/repeater/web/auth_endpoints.py"
        cls = next(node for node in ast.parse(source.read_text()).body
                   if isinstance(node, ast.ClassDef) and node.name == "AuthEndpoints")
        cls.body = [node for node in cls.body if isinstance(node, ast.FunctionDef)
                    and node.name == "change_password"]
        cls.body[0].decorator_list = []
        namespace = {"cherrypy": cherrypy, "logger": Mock()}
        exec(compile(ast.Module(body=[cls], type_ignores=[]), str(source), "exec"), namespace)
        self.auth = namespace["AuthEndpoints"]()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "config.yaml"
        self.config = {"radio_type": "wm1303", "repeater": {"security": {
            "admin_password": "admin-current", "guest_password": "guest-existing",
            "jwt_secret": "preserved"}}}
        self.refresh = Mock(return_value=True)
        daemon = SimpleNamespace(config=self.config,
                                 login_helper=SimpleNamespace(refresh_repeater_security=self.refresh))
        self.manager = ConfigManager(str(self.path), self.config, daemon)
        self.assertTrue(self.manager.save_to_file())
        self.auth.config, self.auth.config_manager = self.config, self.manager
        self.request = SimpleNamespace(method="POST", headers={"Authorization": "Bearer fixture"})
        self.response = SimpleNamespace(status=200, headers={})
        self.jwt = Mock()
        self.jwt.verify_jwt.return_value = {"sub": "admin", "client_id": "fixture"}
        self.tokens = Mock()
        self.tokens.verify_token.return_value = None
        for target, value in (("request", self.request), ("response", self.response),
                              ("config", {"jwt_handler": self.jwt, "token_manager": self.tokens})):
            context = patch.object(cherrypy, target, value)
            context.start()
            self.addCleanup(context.stop)

    def change(self, new_password, current_password="admin-current"):
        self.request.body = io.BytesIO(json.dumps({
            "current_password": current_password, "new_password": new_password,
        }).encode("utf-8"))
        return json.loads(self.auth.change_password())

    def test_admin_cannot_equal_active_or_saved_guest_password(self):
        saved = yaml.safe_load(self.path.read_text())
        saved["repeater"]["security"]["guest_password"] = "guest-staged"
        self.path.write_text(yaml.safe_dump(saved))
        original, disk = deepcopy(self.config), self.path.read_bytes()
        for password in ("guest-existing", "guest-staged"):
            with self.subTest(password=password):
                result = self.change(password)
                self.assertFalse(result["success"])
                self.assertEqual(self.response.status, 400)
                self.assertIn("must be different", result["error"])
                self.assertEqual(self.config, original)
                self.assertEqual(self.path.read_bytes(), disk)
        self.refresh.assert_not_called()

    def test_invalid_wire_passwords_cannot_change_credentials(self):
        original, disk = deepcopy(self.config), self.path.read_bytes()
        for password in ("x" * 16, "é" * 8, "new\x00pass", "\ud800" * 8,
                         "short", "", None, 12345678, ["password"]):
            with self.subTest(password=password):
                result = self.change(password)
                self.assertFalse(result["success"])
                self.assertEqual(self.response.status, 400)
                self.assertEqual(self.config, original)
                self.assertEqual(self.path.read_bytes(), disk)
        self.refresh.assert_not_called()

    def test_exact_utf8_limit_saves_and_refreshes_live_acl(self):
        password = "é" * 7 + "a"
        result = self.change(password)
        self.assertTrue(result["success"], result)
        self.assertTrue(result["saved"] and result["live_updated"])
        self.assertEqual(self.config["repeater"]["security"]["admin_password"], password)
        self.assertEqual(yaml.safe_load(self.path.read_text()), self.config)
        self.assertEqual(self.config["repeater"]["security"]["guest_password"], "guest-existing")
        self.refresh.assert_called_once()
        self.assertNotIn(password, str(result))

    def test_session_and_current_administrator_password_are_required(self):
        original, disk = deepcopy(self.config), self.path.read_bytes()
        self.jwt.verify_jwt.return_value = None
        self.assertFalse(self.change("admin-new")["success"])
        self.assertEqual(self.response.status, 401)
        self.jwt.verify_jwt.return_value = {"sub": "admin", "client_id": "fixture"}
        for password in ("wrong-password", "guest-existing", None, 123):
            with self.subTest(password=password):
                self.assertFalse(self.change("admin-new", password)["success"])
        self.assertEqual(self.config, original)
        self.assertEqual(self.path.read_bytes(), disk)
        self.refresh.assert_not_called()

    def test_failed_save_cannot_change_active_password(self):
        original, disk = deepcopy(self.config), self.path.read_bytes()
        with patch.object(self.manager, "save_to_file", return_value=False):
            result = self.change("admin-new")
        self.assertFalse(result["success"])
        self.assertEqual(self.config, original)
        self.assertEqual(self.path.read_bytes(), disk)
        self.refresh.assert_not_called()


if __name__ == "__main__":
    unittest.main()
