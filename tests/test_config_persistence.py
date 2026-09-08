"""Configuration regression tests using temporary files, without a daemon."""

from copy import deepcopy
from pathlib import Path
import stat
import os
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "overlay/pymc_repeater"))
sys.path.insert(0, str(ROOT / "overlay/pymc_core/src"))

from repeater.atomic_file import atomic_write_text
from repeater.config import load_config, save_config
from repeater.config_manager import ConfigManager
from repeater.identity_manager import IdentityManager
from repeater.handler_helpers.mesh_cli import MeshCLI


class AtomicWriteTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.filename = self.directory / "config.yaml"

    def test_creates_parent_directories_and_private_utf8_file(self):
        filename = self.directory / "nested" / "config.yaml"
        atomic_write_text(filename, "name: Österreich 📡\n")
        self.assertEqual(filename.read_text(), "name: Österreich 📡\n")
        self.assertEqual(stat.S_IMODE(filename.stat().st_mode), 0o600)

    def test_preserves_mode_and_symlink(self):
        atomic_write_text(self.filename, "old")
        self.filename.chmod(0o640)
        link = self.directory / "link.yaml"
        link.symlink_to(self.filename)
        atomic_write_text(link, "new")
        self.assertTrue(link.is_symlink())
        self.assertEqual(self.filename.read_text(), "new")
        self.assertEqual(stat.S_IMODE(self.filename.stat().st_mode), 0o640)

    def test_failed_replace_preserves_old_file_and_removes_temporary(self):
        atomic_write_text(self.filename, "original")
        with patch("repeater.atomic_file.os.replace", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                atomic_write_text(self.filename, "replacement")
        self.assertEqual(self.filename.read_text(), "original")
        self.assertEqual(list(self.directory.iterdir()), [self.filename])

    def test_failed_flush_preserves_old_file_and_removes_temporary(self):
        atomic_write_text(self.filename, "original")
        with patch("repeater.atomic_file.os.fsync", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                atomic_write_text(self.filename, "replacement")
        self.assertEqual(self.filename.read_text(), "original")
        self.assertEqual(list(self.directory.iterdir()), [self.filename])

    def test_save_config_keeps_readable_backup(self):
        atomic_write_text(self.filename, "name: original\n")
        self.assertTrue(save_config({"name": "updated"}, str(self.filename)))
        self.assertEqual(yaml.safe_load(self.filename.read_text()), {"name": "updated"})
        self.assertEqual(self.filename.with_suffix(".yaml.backup").read_text(), "name: original\n")

    def test_invalid_yaml_value_cannot_remove_original(self):
        atomic_write_text(self.filename, "name: original\n")
        with self.assertLogs("Config", level="ERROR"):
            self.assertFalse(save_config({"unsupported": object()}, str(self.filename)))
        self.assertEqual(self.filename.read_text(), "name: original\n")
        self.assertFalse(self.filename.with_suffix(".yaml.backup").exists())

    def test_openhop_environment_names_take_precedence(self):
        legacy = self.directory / "legacy.yaml"
        atomic_write_text(legacy, "repeater:\n  identity_key: legacy\n")
        atomic_write_text(self.filename, "repeater:\n  identity_key: current\n")
        with patch.dict(os.environ, {"OPENHOP_REPEATER_CONFIG": str(self.filename),
                                     "PYMC_REPEATER_CONFIG": str(legacy),
                                     "OPENHOP_REPEATER_LOG_LEVEL": "DEBUG",
                                     "PYMC_REPEATER_LOG_LEVEL": "WARNING"}):
            with self.assertLogs("Config", level="WARNING"):
                config = load_config()
            self.assertEqual(config["repeater"]["identity_key"], "current")
            self.assertEqual(config["logging"]["level"], "DEBUG")
            self.assertTrue(save_config({"saved": True}))
        self.assertEqual(yaml.safe_load(self.filename.read_text()), {"saved": True})
        self.assertEqual(yaml.safe_load(legacy.read_text())["repeater"]["identity_key"], "legacy")


class ConfigManagerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.filename = Path(temporary.name) / "config.yaml"
        self.config = {
            "repeater": {"node_name": "original", "security": {"max_clients": 1, "allow_read_only": False}},
            "radio": {"frequency": 868000000, "tx_power": 14},
        }
        self.manager = ConfigManager(str(self.filename), self.config)

    def test_relative_filename_can_be_saved(self):
        # A basename's dirname is empty; it must not be passed to makedirs.
        with patch("repeater.atomic_file.Path.resolve", return_value=self.filename):
            manager = ConfigManager("config.yaml", self.config)
            self.assertTrue(manager.save_to_file())
        self.assertEqual(yaml.safe_load(self.filename.read_text()), self.config)

    def test_nested_updates_preserve_sibling_settings(self):
        result = self.manager.update_nested("repeater.security.max_clients", 3, live_update=False)
        self.assertTrue(result["success"])
        self.assertEqual(self.config["repeater"]["security"], {"max_clients": 3, "allow_read_only": False})
        self.assertEqual(yaml.safe_load(self.filename.read_text()), self.config)

    def test_failed_save_does_not_mutate_shared_runtime_config(self):
        original = deepcopy(self.config)
        self.manager.daemon = SimpleNamespace(config=self.config)
        with patch.object(self.manager, "save_to_file", return_value=False):
            with patch.object(self.manager, "live_update_daemon") as live_update:
                result = self.manager.update_nested("repeater.security.max_clients", 3)
        self.assertFalse(result["success"])
        self.assertEqual(self.config, original)
        live_update.assert_not_called()

    def test_mesh_cli_uses_transactional_runtime_keys_and_rejects_ambiguous_radio(self):
        acl = SimpleNamespace(refresh_repeater_security=Mock())
        handler = SimpleNamespace(reload_runtime_config=Mock())
        self.manager.daemon = SimpleNamespace(config=self.config, login_helper=acl, repeater_handler=handler)
        cli = MeshCLI(str(self.filename), self.config, self.manager)
        for command, expected in (("name renamed", "> renamed"),
                                  ("txdelay 1.5", "> 1.5"),
                                  ("direct.txdelay 0.25", "> 0.25"),
                                  ("flood.advert.interval 12", "> 12"),
                                  ("guest.password fresh", "> fresh"),
                                  ("allow.read.only on", "> on"),
                                  ("path.hash.mode 2", "> 2")):
            self.assertEqual(cli._cmd_set(command), "OK")
            self.assertEqual(cli._cmd_get(command.split()[0]), expected)
        self.assertEqual(cli._cmd_password("password new-admin"), "password now: new-admin")
        acl.refresh_repeater_security.assert_called_with(self.config)
        self.assertEqual(self.config["repeater"]["security"]["admin_password"], "new-admin")
        handler.reload_runtime_config.reset_mock()
        self.assertEqual(cli._cmd_set("loop.detect strict"), "OK")
        self.assertEqual(cli._cmd_get("loop.detect"), "> strict")
        handler.reload_runtime_config.assert_called_once()
        self.assertEqual(yaml.safe_load(self.filename.read_text()), self.config)

        original = deepcopy(self.config)
        with patch.object(self.manager, "save_to_file", return_value=False):
            self.assertEqual(cli._cmd_set("name unsaved"), "Error: Failed to save config")
        self.assertEqual(self.config, original)
        with patch.object(self.manager, "live_update_daemon") as live_update:
            self.assertTrue(cli._cmd_set("radio 869.618 250 9 6").startswith("OK - restart"))
            live_update.assert_not_called()
        self.assertEqual(self.config["radio"]["frequency"], 869618000)
        self.assertEqual(self.config["radio"]["bandwidth"], 250000)
        self.config["radio_type"] = "wm1303"
        original = deepcopy(self.config)
        for key in ("radio", "freq", "tx"):
            self.assertIn("WM1303 Manager", cli._cmd_get(key))
            self.assertIn("WM1303 Manager", cli._cmd_set(f"{key} 900"))
        self.assertEqual(self.config, original)

    def test_post_replace_flush_failure_keeps_memory_in_sync_with_saved_file(self):
        with patch("repeater.atomic_file.os.fsync", side_effect=[None, OSError("directory flush failed")]):
            with self.assertLogs("repeater.atomic_file", level="WARNING"):
                result = self.manager.update_nested("radio.frequency", 869000000, live_update=False)
        self.assertTrue(result["saved"])
        self.assertEqual(self.config["radio"]["frequency"], 869000000)
        self.assertEqual(yaml.safe_load(self.filename.read_text()), self.config)

    def test_updates_replace_null_sections_and_do_not_share_input(self):
        self.config["mesh"] = None
        updates = {"mesh": {"nested": {"value": 1}}}
        self.assertTrue(self.manager.update_and_save(updates, live_update=False)["success"])
        updates["mesh"]["nested"]["value"] = 99
        self.assertEqual(self.config["mesh"]["nested"]["value"], 1)

    def test_live_update_copies_nested_values(self):
        self.manager.daemon = SimpleNamespace(config={"repeater": None})
        self.assertTrue(self.manager.live_update_daemon(["repeater"]))
        self.config["repeater"]["security"]["max_clients"] = 99
        self.assertEqual(self.manager.daemon.config["repeater"]["security"]["max_clients"], 1)

    def test_live_radio_snapshot_accepts_zero_dbm(self):
        handler = SimpleNamespace(radio_config={"tx_power": 14})
        self.manager.daemon = SimpleNamespace(repeater_handler=handler)
        self.manager._sync_repeater_handler_radio_config({"tx_power": 0})
        self.assertEqual(handler.radio_config["tx_power"], 0)

    def test_successful_radio_update_refreshes_airtime_parameters(self):
        self.config["radio"].update(bandwidth=62500, spreading_factor=10, coding_rate=8, preamble_length=17)
        airtime_manager = Mock()
        radio = SimpleNamespace(configure_radio=Mock(return_value=True))
        handler = SimpleNamespace(radio_config={}, airtime_mgr=airtime_manager)
        self.manager.daemon = SimpleNamespace(radio=radio, repeater_handler=handler)
        self.assertTrue(self.manager._apply_live_radio_config())
        airtime_manager.refresh_radio_params.assert_called_once_with(self.config["radio"])

    def test_failed_radio_update_does_not_refresh_airtime_parameters(self):
        airtime_manager = Mock()
        radio = SimpleNamespace(configure_radio=Mock(return_value=False))
        handler = SimpleNamespace(radio_config={}, airtime_mgr=airtime_manager)
        self.manager.daemon = SimpleNamespace(radio=radio, repeater_handler=handler)
        with self.assertLogs("ConfigManager", level="WARNING"):
            self.assertFalse(self.manager._apply_live_radio_config())
        airtime_manager.refresh_radio_params.assert_not_called()


class IdentityManagerTests(unittest.TestCase):
    def test_duplicate_name_cannot_diverge_identity_indexes(self):
        manager = IdentityManager({})
        first = SimpleNamespace(get_public_key=lambda: bytes([1]) * 32)
        second = SimpleNamespace(get_public_key=lambda: bytes([2]) * 32)
        self.assertTrue(manager.register_identity("node", first, {}, "repeater"))
        with self.assertLogs("IdentityManager", level="ERROR"):
            self.assertFalse(manager.register_identity("node", second, {}, "companion"))
        self.assertIs(manager.get_identity_by_name("node")[0], first)
        self.assertIsNone(manager.get_identity_by_hash(2))


if __name__ == "__main__":
    unittest.main()
