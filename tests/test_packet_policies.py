"""Console policy compilation, persistence and live decisions without radios."""

import ast
import hashlib
import importlib.util
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import cherrypy
import yaml


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "overlay/pymc_repeater/repeater"
PACKAGE = "wm1303_packet_policy_tests"
package = ModuleType(PACKAGE)
package.__path__ = [str(SOURCE)]
sys.modules[PACKAGE] = package
constants = ModuleType("openhop_core.protocol.constants")
constants.PAYLOAD_TYPE_GRP_TXT, constants.PAYLOAD_TYPE_GRP_DATA = 5, 6
crypto = ModuleType("openhop_core.protocol.crypto")
crypto.CryptoUtils = SimpleNamespace(_hmac_sha256=Mock(), _aes_decrypt=Mock())


def load_module(name):
    spec = importlib.util.spec_from_file_location(f"{PACKAGE}.{name}", SOURCE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    with patch.dict(sys.modules, {"openhop_core.protocol.constants": constants,
                                  "openhop_core.protocol.crypto": crypto}):
        spec.loader.exec_module(module)
    return module


engine_module = load_module("policy_engine")
service_module = load_module("policy_service")
PolicyService = service_module.PolicyService
PolicyDocumentError = service_module.PolicyDocumentError


def rule(field="hop_count", value=4, op="greater_than", action="drop", rule_id=1):
    return {"id": rule_id, "name": "fixture", "enabled": True,
            "if": {"all": [{"field": field, "op": op, "value": value}]}, "then": {"action": action}}


def document(rules=None, enabled=True, default_action="allow", **extra):
    return {"policy_engine": {"enabled": enabled, "default_action": default_action,
                              "rules": rules or [], "objects": {}, **extra}}


class PacketPolicyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config_path = Path(self.directory.name) / "config.yaml"
        self.config_path.write_text("repeater: {node_name: fixture}\n")
        self.config = {"policy": {"policy_file": "policies/packet.yaml"}}
        self.service = PolicyService(self.config, str(self.config_path))
        self.path = Path(self.directory.name) / "policies/packet.yaml"
        self.packet = SimpleNamespace(payload=b"\x12body", get_payload_type=lambda: 5,
                                      get_path_hashes_hex=lambda: ["AB", "CD"])

    def test_missing_file_disabled_defaults_do_not_write_and_preserve_behavior(self):
        snapshot = self.service.snapshot()
        self.assertFalse(snapshot["exists"])
        self.assertFalse(snapshot["policy_engine"]["enabled"])
        self.assertEqual(snapshot["policy_file"], str(self.path))
        self.assertFalse(self.path.exists())
        self.assertEqual(self.service.evaluate(self.packet, {}).action, "allow")

    def test_first_match_log_only_and_live_update_replace_the_running_engine(self):
        first = rule(action="log_only")
        second = rule(action="drop", rule_id=2)
        initial = self.service.engine
        self.service.update(document([first, second]))
        self.assertIsNot(self.service.engine, initial)
        decision = self.service.evaluate(self.packet, {"hop_count": 5})
        self.assertEqual((decision.action, decision.rule_id, decision.matched), ("log_only", 1, True))
        self.service.update(document([second]))
        self.assertEqual(self.service.evaluate(self.packet, {"hop_count": 5}).action, "drop")
        self.service.update(document([second], enabled=False, default_action="drop"))
        self.assertEqual(self.service.evaluate(self.packet, {"hop_count": 5}).action, "allow")

    def test_default_action_and_nested_any_all(self):
        nested = rule()
        nested["if"] = {"all": [{"field": "local_transmission", "value": False},
                                {"any": [{"field": "rssi", "op": "less_than", "value": -100},
                                         {"field": "snr", "op": "less_than", "value": -5}]}]}
        self.service.update(document([nested], default_action="log_only"))
        self.assertEqual(self.service.evaluate(self.packet, {"local_transmission": False, "rssi": -90, "snr": -6}).action, "drop")
        self.assertEqual(self.service.evaluate(self.packet, {"local_transmission": True, "rssi": -110, "snr": -6}).action, "log_only")

    def test_receipt_can_record_the_exact_engine_before_and_after_live_update(self):
        decision, first_engine = self.service.evaluate_with_engine(self.packet, {})
        self.assertEqual(decision.action, "allow")
        self.service.update(document(default_action="drop"))
        decision, second_engine = self.service.evaluate_with_engine(self.packet, {})
        self.assertEqual(decision.action, "drop")
        self.assertIsNot(first_engine, second_engine)
        self.assertIs(second_engine, self.service.engine)

    def test_path_hashes_and_channel_secrets_use_wire_values(self):
        self.service.update(document([rule("path_hashes", ["0xab"], "intersects")]))
        self.assertEqual(self.service.evaluate(self.packet, {}).action, "drop")
        secret = "9CD8FCF22A47333B591D96A2B848B73F"
        self.packet.payload = bytes([hashlib.sha256(bytes.fromhex(secret)).digest()[0]]) + b"body"
        self.service.update(document([rule("channel_hash", "0x" + secret, "equals")]))
        self.assertEqual(self.service.evaluate(self.packet, {}).action, "drop")
        self.service.update(document([rule("channel_hash", "0x12", "equals")]))
        self.assertEqual(self.service.evaluate(self.packet, {}).action, "allow")

    def test_decrypted_conditions_cache_only_current_packet(self):
        self.service.update(document([rule("channel_message_body", "blocked", "contains")]))
        for index in range(20):
            packet = SimpleNamespace(decrypted={"group_text_data": {"text": f"Alice: blocked {index}"}})
            self.assertEqual(self.service.evaluate(packet, {}).action, "drop")
            self.assertLessEqual(len(self.service.engine._channel_decrypt_cache), 1)
            packet.decrypted["group_text_data"]["text"] = "Alice: allowed"
            self.assertEqual(self.service.evaluate(packet, {}).action, "allow")

    def test_debug_logs_do_not_expose_channel_secret_literals_or_parse_errors(self):
        malformed_secret = "private-channel-secret"
        engine = engine_module.PolicyEngine(document([rule("channel_hash", malformed_secret, "equals")])["policy_engine"])
        with self.assertLogs("PolicyEngine", level="DEBUG") as logs:
            self.assertEqual(engine.evaluate(self.packet, {}).action, "allow")
        self.assertNotIn(malformed_secret, "\n".join(logs.output))
        self.assertNotIn("expected=", "\n".join(logs.output))

    def test_failed_atomic_replace_preserves_disk_and_runtime(self):
        self.service.update(document([rule()]))
        previous = self.path.read_bytes()
        runtime = self.service.engine
        saved_config = yaml.safe_dump(self.config)
        with patch.object(service_module.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.service.update(document([], default_action="drop"))
        self.assertEqual(self.path.read_bytes(), previous)
        self.assertIs(self.service.engine, runtime)
        self.assertEqual(yaml.safe_dump(self.config), saved_config)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_existing_malformed_file_blocks_load_and_cannot_be_overwritten(self):
        self.path.parent.mkdir()
        for invalid in ("", "policy_engine: [bad]\n", "policy_engine: {enabled: yes, rules: broken}\n", "a: ["):
            with self.subTest(invalid=invalid):
                self.path.write_text(invalid)
                with self.assertRaises(PolicyDocumentError):
                    PolicyService(self.config, str(self.config_path))
                with self.assertRaises(PolicyDocumentError):
                    self.service.update(document())
                self.assertEqual(self.path.read_text(), invalid)

    def test_manual_corruption_preserves_last_valid_engine(self):
        self.service.update(document([rule()]))
        engine = self.service.engine
        self.path.write_text("policy_engine: []\n")
        with self.assertRaises(PolicyDocumentError):
            self.service.snapshot()
        self.assertIs(self.service.engine, engine)
        self.assertEqual(self.service.evaluate(self.packet, {"hop_count": 7}).action, "drop")

    def test_invalid_shapes_values_operators_and_references_are_rejected(self):
        invalid = [
            {"enabled": "false"}, {"enabled": 1}, {"rules": {}}, {"objects": []},
            {"default_action": "ban"}, {"unknown": True},
            {"rules": [rule("unknown_field", 3)]},
            {"rules": [rule("hop_count", "3")]},
            {"rules": [rule("hop_count", float("nan"))]},
            {"rules": [rule("hop_count", True)]},
            {"rules": [rule("hop_count", 3, "regex")]},
            {"rules": [rule("path_hashes", ["0x42", "0x0042"], "intersects")]},
            {"rules": [rule("channel_hash", "0x100", "equals")]},
            {"rules": [rule("channel_hash", "@channel_hash_groups.missing", "in")]},
            {"rules": [rule("local_transmission", "true", "equals")]},
            {"rules": [rule(), rule()]},
            {"rules": [{"if": {"all": []}, "then": {"action": "drop"}}]},
        ]
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(PolicyDocumentError):
                self.service.update(payload)
        self.assertFalse(self.path.exists())

    def test_group_and_entry_crud_project_objects_and_publish_decisions(self):
        self.service.mutate_group({"kind": "channel_hashes", "group_id": "ops", "friendly_name": "Ops"})
        added = self.service.mutate_entry({"kind": "channel_hashes", "group_id": "ops", "value": "0x12", "friendly_name": "Primary"})
        self.assertEqual(added["entry"]["value"], "0x12")
        self.service.update(document([rule("channel_hash", "@channel_hash_groups.ops", "in")]))
        self.assertEqual(self.service.evaluate(self.packet, {}).action, "drop")
        self.assertEqual(self.service.groups("channel_hashes")["groups"][0]["friendly_name"], "Ops")
        self.assertEqual(self.service.group_entries("channel_hashes", "ops")["entries"][0]["id"], "primary")
        previous = self.path.read_bytes()
        with self.assertRaises(PolicyDocumentError):
            self.service.mutate_group({"kind": "channel_hashes", "group_id": "ops"}, delete=True)
        self.assertEqual(self.path.read_bytes(), previous)
        self.service.mutate_entry({"kind": "channel_hashes", "group_id": "ops", "entry_id": "primary"}, delete=True)
        self.assertEqual(self.service.evaluate(self.packet, {}).action, "allow")
        self.service.update(document())
        self.service.mutate_group({"kind": "channel_hashes", "group_id": "ops"}, delete=True)
        self.assertEqual(self.service.snapshot()["policy_engine"]["objects"]["channel_hash_groups"], {})

    def test_pubkey_entries_are_normalized_and_invalid_entries_preserve_file(self):
        self.service.mutate_group({"kind": "pubkeys", "group_id": "trusted"})
        added = self.service.mutate_entry({"kind": "pubkeys", "group_id": "trusted", "value": "0xAABBCCDD"})
        self.assertEqual(added["entry"]["value"], "0xaabbccdd")
        previous = self.path.read_bytes()
        for value in ("xyz", "a", "ab" * 33, True, 34):
            with self.subTest(value=value), self.assertRaises(PolicyDocumentError):
                self.service.mutate_entry({"kind": "pubkeys", "group_id": "trusted", "value": value})
        self.assertEqual(self.path.read_bytes(), previous)

    def test_console_preview_accepts_draft_group_refs_without_installing_them(self):
        payload = document([rule("channel_hash", "@channel_hash_groups.draft", "in")],
                           objects={"channel_hash_groups": {"draft": ["0x12"]}})
        original = self.service.engine
        validated = self.service.validate(payload, preview=True)
        self.assertEqual(validated["policy_engine"]["objects"]["channel_hash_groups"]["draft"], ["0x12"])
        self.assertIs(self.service.engine, original)
        self.assertFalse(self.path.exists())
        payload["groups"] = {"channel_hashes": [{"id": "draft", "entries": [{"value": "0x12"}]}]}
        self.service.update(payload)
        self.assertEqual(self.service.evaluate(self.packet, {}).action, "drop")

    def test_duplicate_group_ids_and_invalid_entries_fail_entire_document(self):
        payload = document()
        payload["groups"] = {"channel_hashes": [{"id": "ops"}, {"id": "ops"}]}
        with self.assertRaises(PolicyDocumentError):
            self.service.update(payload)
        payload["groups"] = {"channel_hashes": [{"id": "ops", "entries": [{"value": "0x12"}, {"value": "bad"}]}]}
        with self.assertRaises(PolicyDocumentError):
            self.service.update(payload)
        self.assertFalse(self.path.exists())

    def test_service_helper_reuses_daemon_service_and_keeps_config_file_separate(self):
        daemon = SimpleNamespace()
        service = service_module.get_policy_service(self.config, str(self.config_path), daemon)
        self.assertIs(service_module.get_policy_service(self.config, str(self.config_path), daemon), service)
        self.assertIs(daemon.policy_service, service)
        with self.assertRaises(PolicyDocumentError):
            PolicyService({"policy": {"policy_file": "config.yaml"}}, str(self.config_path))


def load_api():
    path = SOURCE / "web/api_endpoints.py"
    cls = next(node for node in ast.parse(path.read_text()).body if isinstance(node, ast.ClassDef) and node.name == "APIEndpoints")
    methods = {"_success", "_error", "policy", "policy_validate", "policy_groups", "policy_group_entries",
               "_policy_request_data", "_policy_method_error"}
    cls.body = [node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name in methods]
    for method in cls.body:
        method.decorator_list = []
    namespace = {"cherrypy": cherrypy}
    exec(compile(ast.Module(body=[cls], type_ignores=[]), str(path), "exec"), namespace)
    return namespace["APIEndpoints"]()


class PacketPolicyAPITests(unittest.TestCase):
    def setUp(self):
        PacketPolicyTests.setUp(self)
        self.api = load_api()
        self.api._set_cors_headers = Mock()
        self.api._get_packet_policy_service = lambda: self.service
        self.request = SimpleNamespace(method="GET", params={}, json=None)
        self.response = SimpleNamespace(status=200, headers={})
        request_patch = patch.object(cherrypy, "request", self.request)
        response_patch = patch.object(cherrypy, "response", self.response)
        request_patch.start()
        response_patch.start()
        self.addCleanup(request_patch.stop)
        self.addCleanup(response_patch.stop)

    def test_policy_get_update_validate_and_method_errors(self):
        self.assertTrue(self.api.policy()["data"]["supported"])
        self.request.method, self.request.json = "POST", document([rule()])
        validated = self.api.policy_validate()
        self.assertTrue(validated["data"]["valid"])
        self.assertEqual(validated["data"]["effective"]["rule_count"], 1)
        self.assertFalse(self.path.exists())
        result = self.api.policy()
        self.assertTrue(result["saved"])
        self.assertFalse(result["restart_required"])
        self.request.method = "DELETE"
        self.assertFalse(self.api.policy()["success"])
        self.assertEqual(self.response.status, 405)
        self.request.method = "OPTIONS"
        for endpoint in (self.api.policy, self.api.policy_validate, self.api.policy_groups, self.api.policy_group_entries):
            self.assertEqual(endpoint(), "")

    def test_group_api_supports_query_delete_and_json_errors(self):
        self.request.method = "POST"
        self.request.json = {"kind": "channel_hashes", "group_id": "ops"}
        self.assertTrue(self.api.policy_groups()["success"])
        self.request.json = {"kind": "channel_hashes", "group_id": "ops", "value": "0x12"}
        added = self.api.policy_group_entries()
        self.assertTrue(added["success"])
        self.request.method, self.request.json = "GET", None
        self.request.params = {"kind": "channel_hashes", "group_id": "ops"}
        self.assertEqual(self.api.policy_group_entries()["data"]["entries"][0]["value"], "0x12")
        self.request.method = "DELETE"
        self.request.params["entry_id"] = added["data"]["entry_id"]
        self.assertTrue(self.api.policy_group_entries()["success"])
        self.request.params.pop("entry_id")
        self.assertTrue(self.api.policy_groups()["success"])
        self.request.method, self.request.json = "POST", []
        self.assertFalse(self.api.policy()["success"])
        self.assertEqual(self.response.status, 400)


if __name__ == "__main__":
    unittest.main()
