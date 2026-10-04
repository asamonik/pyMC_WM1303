"""Validated packet policies shared by the Console and live packet paths."""

from copy import deepcopy
import logging
import math
import os
from pathlib import Path
import re
import stat
import tempfile
import threading

import yaml

from .policy_engine import PolicyEngine, SUPPORTED_ACTIONS, default_policy_engine_config


logger = logging.getLogger("PolicyService")
GROUP_KINDS = ("channel_hashes", "pubkeys")
NUMERIC_FIELDS = {
    "route_type", "payload_type", "payload_length", "path_hash_size", "hop_count",
    "rssi", "snr", "transport_code_0", "transport_code_1",
}
BOOLEAN_FIELDS = {"local_transmission", "channel_decryptable"}
STRING_FIELDS = {"mode", "channel_sender", "channel_message_body", "payload_hex", "ingress", "channel"}
SUPPORTED_FIELDS = NUMERIC_FIELDS | BOOLEAN_FIELDS | STRING_FIELDS | {"path_hashes", "channel_hash"}
OP_ALIASES = {
    "eq": "equals", "==": "equals", "ne": "not_equals", "!=": "not_equals",
    "gt": "greater_than", ">": "greater_than", "gte": "greater_or_equal", ">=": "greater_or_equal",
    "lt": "less_than", "<": "less_than", "lte": "less_or_equal", "<=": "less_or_equal",
    "is_in": "in", "overlaps": "intersects",
}
SUPPORTED_OPS = {
    "equals", "not_equals", "greater_than", "greater_or_equal", "less_than", "less_or_equal",
    "contains", "in", "intersects", "starts_with", "ends_with",
}


class PolicyDocumentError(ValueError):
    """A policy cannot be compiled safely; its saved contents remain intact."""


def _object(value, label):
    if not isinstance(value, dict):
        raise PolicyDocumentError(f"{label} must be an object")
    return value


def _check_keys(value, allowed, label):
    unknown = set(value) - allowed
    if unknown:
        raise PolicyDocumentError(f"Unsupported {label} field: {sorted(map(str, unknown))[0]}")


def _identifier(value, fallback=None):
    if value is None or value == "":
        value = fallback
    if not isinstance(value, str) or not value.strip():
        raise PolicyDocumentError("A nonempty group or entry id/name is required")
    result = re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")
    if not result or len(result) > 128:
        raise PolicyDocumentError("Group and entry ids must contain letters or numbers (maximum 128 characters)")
    return result


def _text(value, label, default=""):
    if value is None:
        return default
    if not isinstance(value, str):
        raise PolicyDocumentError(f"{label} must be a string")
    return value


def _validate_object_value(value, depth=0):
    if depth > 16:
        raise PolicyDocumentError("Policy objects are nested too deeply")
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise PolicyDocumentError("Policy object keys must be strings")
        for item in value.values():
            _validate_object_value(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _validate_object_value(item, depth + 1)
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise PolicyDocumentError("Policy objects must contain finite numeric values")
    elif value is not None and not isinstance(value, (str, int, bool)):
        raise PolicyDocumentError("Policy objects must contain JSON values")


def _finite_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def normalize_entry_value(kind, value):
    if kind not in GROUP_KINDS:
        raise PolicyDocumentError("Invalid kind. Use 'channel_hashes' or 'pubkeys'")
    if isinstance(value, bool) or value is None or not isinstance(value, (str, int)):
        raise PolicyDocumentError("Entry value must be a hexadecimal string or channel hash number")
    if kind == "channel_hashes":
        try:
            raw = str(value).strip()
            secret = PolicyEngine._extract_channel_secret_literal(raw)
            if secret:
                return "0x" + secret.upper()
            normalized = PolicyEngine._normalize_channel_hash_value(value)
            if normalized is None:
                raise ValueError("channel hash value is required")
            return normalized
        except ValueError as exc:
            raise PolicyDocumentError(str(exc)) from exc
    if not isinstance(value, str):
        raise PolicyDocumentError("Pubkey must be a hexadecimal string")
    raw = value.strip().lower()
    if raw.startswith("0x"):
        raw = raw[2:]
    if not re.fullmatch(r"(?:[0-9a-f]{2}){1,32}", raw):
        raise PolicyDocumentError("Pubkey must contain 1 to 32 bytes of hexadecimal text")
    return "0x" + raw


def normalize_groups(groups):
    _object(groups, "groups")
    _check_keys(groups, set(GROUP_KINDS), "group kind")
    result = {kind: [] for kind in GROUP_KINDS}
    for kind in GROUP_KINDS:
        source = groups.get(kind, [])
        if not isinstance(source, list):
            raise PolicyDocumentError(f"groups.{kind} must be a list")
        ids = set()
        for index, group in enumerate(source):
            _object(group, f"groups.{kind}[{index}]")
            _check_keys(group, {"id", "name", "friendly_name", "description", "entries"}, "group")
            group_id = _identifier(group.get("id"), group.get("friendly_name") or group.get("name"))
            if group_id in ids:
                raise PolicyDocumentError(f"Duplicate group id: {group_id}")
            ids.add(group_id)
            entries = group.get("entries", [])
            if not isinstance(entries, list):
                raise PolicyDocumentError(f"Entries for {group_id} must be a list")
            normalized_entries, entry_ids = [], set()
            for entry in entries:
                _object(entry, "entry")
                _check_keys(entry, {"id", "name", "friendly_name", "value"}, "entry")
                value = normalize_entry_value(kind, entry.get("value"))
                entry_id = _identifier(entry.get("id"), entry.get("friendly_name") or entry.get("name") or value)
                if entry_id in entry_ids:
                    raise PolicyDocumentError(f"Duplicate entry id in {group_id}: {entry_id}")
                entry_ids.add(entry_id)
                normalized_entries.append({
                    "id": entry_id, "friendly_name": _text(entry.get("friendly_name", entry.get("name")), "friendly_name", entry_id),
                    "value": value,
                })
            result[kind].append({
                "id": group_id, "friendly_name": _text(group.get("friendly_name", group.get("name")), "friendly_name", group_id),
                "description": _text(group.get("description"), "description"), "entries": normalized_entries,
            })
    return result


def _validate_condition(condition, objects, depth=0):
    _object(condition, "condition")
    if depth > 16:
        raise PolicyDocumentError("Policy conditions are nested too deeply")
    branches = set(condition) & {"all", "any"}
    if branches:
        if len(branches) != 1 or len(condition) != 1:
            raise PolicyDocumentError("A condition must contain exactly one all/any branch")
        branch = next(iter(branches))
        items = condition[branch]
        if not isinstance(items, list) or not items:
            raise PolicyDocumentError(f"{branch} must be a nonempty list of conditions")
        return {branch: [_validate_condition(item, objects, depth + 1) for item in items]}
    _check_keys(condition, {"field", "op", "value"}, "condition")
    field = condition.get("field")
    if not isinstance(field, str) or field not in SUPPORTED_FIELDS:
        raise PolicyDocumentError(f"Unsupported policy field: {field}")
    op = condition.get("op", "equals")
    if not isinstance(op, str):
        raise PolicyDocumentError("Condition op must be a string")
    op = OP_ALIASES.get(op, op)
    if op not in SUPPORTED_OPS:
        raise PolicyDocumentError(f"Unsupported policy operator: {op}")
    if "value" not in condition:
        raise PolicyDocumentError("Condition value is required")
    value = condition["value"]
    resolved = value
    if isinstance(value, str) and value.startswith("@"):
        parts = value[1:].split(".", 1)
        if len(parts) != 2 or not isinstance(objects.get(parts[0]), dict) or parts[1] not in objects[parts[0]]:
            raise PolicyDocumentError(f"Unknown policy object reference: {value}")
        resolved = objects[parts[0]][parts[1]]
    values = resolved if isinstance(resolved, list) else [resolved]
    if op in {"in", "intersects"} and not isinstance(resolved, list):
        raise PolicyDocumentError(f"{op} requires a list or a reference to a group")
    if op not in {"in", "intersects"} and isinstance(resolved, list):
        raise PolicyDocumentError(f"{op} requires a single value")
    if field in NUMERIC_FIELDS:
        if op not in {"equals", "not_equals", "greater_than", "greater_or_equal", "less_than", "less_or_equal", "in"}:
            raise PolicyDocumentError(f"{op} is not supported for {field}")
        if any(not _finite_number(item) for item in values):
            raise PolicyDocumentError(f"{field} requires finite numeric values")
    elif field in BOOLEAN_FIELDS:
        if op not in {"equals", "not_equals"} or type(resolved) is not bool:
            raise PolicyDocumentError(f"{field} requires a boolean and equals/not_equals")
    elif field == "path_hashes":
        if op not in {"contains", "intersects"}:
            raise PolicyDocumentError("path_hashes requires contains/intersects")
        try:
            if any(item is None or isinstance(item, bool) for item in values):
                raise ValueError("path hashes must be hex")
            normalized = PolicyEngine._normalize_path_hash_values(resolved)
            if normalized is None:
                raise ValueError("path hash value is required")
        except ValueError as exc:
            raise PolicyDocumentError(str(exc)) from exc
    elif field == "channel_hash":
        if op not in {"equals", "not_equals", "in"}:
            raise PolicyDocumentError("channel_hash requires equals/not_equals/in")
        for item in values:
            normalize_entry_value("channel_hashes", item)
    elif field in STRING_FIELDS:
        if any(not isinstance(item, str) for item in values):
            raise PolicyDocumentError(f"{field} requires string values")
        if op not in {"equals", "not_equals", "contains", "starts_with", "ends_with", "in"}:
            raise PolicyDocumentError(f"{op} is not supported for {field}")
    return {"field": field, "op": op, "value": deepcopy(value)}


def normalize_document(payload, existing_groups=None, *, project_groups=True):
    _object(payload, "Policy document")
    if "policy_engine" in payload:
        _check_keys(payload, {"policy_engine", "groups"}, "document")
        engine = deepcopy(_object(payload["policy_engine"], "policy_engine"))
    else:
        engine = deepcopy({key: value for key, value in payload.items() if key != "groups"})
    _check_keys(engine, {"enabled", "default_action", "rules", "objects"}, "policy_engine")
    cfg = default_policy_engine_config()
    cfg.update(engine)
    if type(cfg["enabled"]) is not bool:
        raise PolicyDocumentError("enabled must be a boolean")
    if not isinstance(cfg["default_action"], str) or cfg["default_action"] not in SUPPORTED_ACTIONS:
        raise PolicyDocumentError(f"Unsupported default_action: {cfg['default_action']}")
    groups = normalize_groups(payload.get("groups", existing_groups if existing_groups is not None else {}))
    objects = _object(cfg["objects"], "objects")
    _validate_object_value(objects)
    # Group projections are authoritative; remove deleted groups as well.
    if project_groups and ("groups" in payload or existing_groups is not None):
        objects.update({
            "channel_hash_groups": {group["id"]: [entry["value"] for entry in group["entries"]] for group in groups["channel_hashes"]},
            "pubkey_groups": {group["id"]: [entry["value"] for entry in group["entries"]] for group in groups["pubkeys"]},
        })
    rules = cfg["rules"]
    if not isinstance(rules, list):
        raise PolicyDocumentError("rules must be a list")
    seen_ids = set()
    normalized_rules = []
    for index, rule in enumerate(rules):
        _object(rule, f"rules[{index}]")
        _check_keys(rule, {"id", "name", "enabled", "if", "then", "action"}, "rule")
        rule = deepcopy(rule)
        rule_id = rule.get("id", index + 1)
        if isinstance(rule_id, bool) or not isinstance(rule_id, (str, int)) or rule_id == "":
            raise PolicyDocumentError("Rule id must be a nonempty string or integer")
        if rule_id in seen_ids:
            raise PolicyDocumentError(f"Duplicate rule id: {rule_id}")
        seen_ids.add(rule_id)
        rule["id"] = rule_id
        rule["enabled"] = rule.get("enabled", True)
        if type(rule["enabled"]) is not bool:
            raise PolicyDocumentError("Rule enabled must be a boolean")
        if "name" in rule:
            _text(rule["name"], "Rule name")
        then = rule.get("then", rule.get("action", "allow"))
        if isinstance(then, dict):
            _check_keys(then, {"action"}, "then")
            action = then.get("action")
        else:
            action = then
        if not isinstance(action, str) or action not in SUPPORTED_ACTIONS:
            raise PolicyDocumentError(f"Unsupported rule action: {action}")
        if "action" in rule and rule["action"] != action:
            raise PolicyDocumentError("Conflicting rule action and then.action")
        rule.pop("action", None)
        rule["then"] = {"action": action}
        rule["if"] = _validate_condition(rule.get("if"), objects)
        normalized_rules.append(rule)
    cfg["rules"] = normalized_rules
    return {"policy_engine": cfg, "groups": groups}


class PolicyService:
    def __init__(self, config, config_path, daemon=None):
        self.config = config
        self.config_path = str(Path(config_path).resolve())
        self.daemon = daemon
        policy = _object(config.get("policy", {}), "policy settings")
        filename = policy.get("policy_file", "policy.yaml")
        if not isinstance(filename, str) or not filename.strip():
            raise PolicyDocumentError("policy.policy_file must be a nonempty path")
        self.path = Path(filename)
        if not self.path.is_absolute():
            self.path = Path(self.config_path).parent / self.path
        self.path = self.path.resolve()
        if str(self.path) == self.config_path:
            raise PolicyDocumentError("Policy file must be separate from the repeater configuration")
        self._lock = threading.RLock()
        self.load_error = None
        self._document, self.exists = self._read()
        self._activate(self._document)

    def _read(self):
        try:
            with self.path.open("r", encoding="utf-8") as stream:
                payload = yaml.safe_load(stream)
        except FileNotFoundError:
            if self.path.is_symlink():
                raise PolicyDocumentError(f"Policy file is an invalid symlink: {self.path}")
            return normalize_document({"policy_engine": self.config.get("policy_engine", default_policy_engine_config())}), False
        except (OSError, yaml.YAMLError) as exc:
            raise PolicyDocumentError(f"Cannot read policy file {self.path}: {exc}") from exc
        try:
            return normalize_document(payload), True
        except (ValueError, TypeError) as exc:
            raise PolicyDocumentError(f"Invalid policy file {self.path}: {exc}") from exc

    def _activate(self, document, engine=None):
        self.engine = engine or PolicyEngine(document["policy_engine"])
        self._document = document
        self.config["policy_engine"] = deepcopy(document["policy_engine"])
        self.config["policy_file_path"] = str(self.path)

    def evaluate(self, packet, context):
        with self._lock:
            return self.engine.evaluate(packet, context)

    def evaluate_with_engine(self, packet, context):
        """Return the exact engine used, even during a concurrent live update."""
        with self._lock:
            engine = self.engine
            return engine.evaluate(packet, context), engine

    def snapshot(self):
        with self._lock:
            # Refuse to present defaults for a file that became malformed after
            # startup. Keep the last successfully installed engine running.
            document, exists = self._read()
            return {"policy_file": str(self.path), "exists": exists, "supported": True,
                    "runtime_active": self.daemon is not None, **deepcopy(document)}

    def validate(self, payload, *, preview=False):
        with self._lock:
            existing, _ = self._read()
            # Console validation sends its draft object projection before the
            # draft groups are saved; those references must be validated as sent.
            return normalize_document(payload, existing["groups"], project_groups=not preview or "groups" in payload)

    def _persist(self, document):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        previous = self.path.stat() if self.path.exists() else None
        descriptor, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                if previous is not None:
                    current = os.fstat(stream.fileno())
                    if (previous.st_uid, previous.st_gid) != (current.st_uid, current.st_gid):
                        os.fchown(stream.fileno(), previous.st_uid, previous.st_gid)
                    os.fchmod(stream.fileno(), stat.S_IMODE(previous.st_mode))
                yaml.safe_dump(document, stream, sort_keys=False, allow_unicode=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _save(self, document):
        # Compile and persist completely before publishing the new engine.
        engine = PolicyEngine(document["policy_engine"])
        self._persist(document)
        self._activate(document, engine)
        self.exists = True
        return {"policy_file": str(self.path), "exists": True, "supported": True,
                "runtime_active": self.daemon is not None, **deepcopy(document)}

    def update(self, payload):
        with self._lock:
            return self._save(self.validate(payload))

    def groups(self, kind=None):
        if kind is not None and kind not in GROUP_KINDS:
            raise PolicyDocumentError("Invalid kind. Use 'channel_hashes' or 'pubkeys'")
        snapshot = self.snapshot()
        return {"policy_file": snapshot["policy_file"], "exists": snapshot["exists"], "kind": kind,
                "groups": snapshot["groups"][kind] if kind else snapshot["groups"]}

    @staticmethod
    def _find_group(document, kind, group_id):
        if kind not in GROUP_KINDS:
            raise PolicyDocumentError("Invalid kind. Use 'channel_hashes' or 'pubkeys'")
        if not isinstance(group_id, str) or not group_id:
            raise PolicyDocumentError("group_id parameter required")
        for group in document["groups"][kind]:
            if group["id"] == group_id:
                return group
        raise PolicyDocumentError(f"Group not found: {group_id}")

    def group_entries(self, kind, group_id):
        snapshot = self.snapshot()
        group = self._find_group(snapshot, kind, group_id)
        return {"policy_file": str(self.path), "exists": snapshot["exists"], "kind": kind,
                "group_id": group_id, "group": group, "entries": group["entries"]}

    def mutate_group(self, payload, delete=False):
        _object(payload, "Group request")
        _check_keys(payload, {"kind", "group_id", "friendly_name", "description"}, "group request")
        with self._lock:
            document, _ = self._read()
            kind = payload.get("kind")
            if kind not in GROUP_KINDS:
                raise PolicyDocumentError("Invalid kind. Use 'channel_hashes' or 'pubkeys'")
            group_id = _identifier(payload.get("group_id"), payload.get("friendly_name"))
            if delete:
                self._find_group(document, kind, group_id)
                document["groups"][kind] = [group for group in document["groups"][kind] if group["id"] != group_id]
                group = None
            else:
                if any(group["id"] == group_id for group in document["groups"][kind]):
                    raise PolicyDocumentError(f"Group already exists: {group_id}")
                group = {"id": group_id, "friendly_name": payload.get("friendly_name", group_id),
                         "description": payload.get("description", ""), "entries": []}
                document["groups"][kind].append(group)
            saved = self._save(normalize_document(document))
            if not delete:
                group = self._find_group(saved, kind, group_id)
            return {"policy_file": str(self.path), "exists": True, "kind": kind, "group_id": group_id,
                    "group": deepcopy(group), "groups": saved["groups"][kind]}

    def mutate_entry(self, payload, delete=False):
        _object(payload, "Entry request")
        _check_keys(payload, {"kind", "group_id", "entry_id", "friendly_name", "value"}, "entry request")
        with self._lock:
            document, _ = self._read()
            kind, group_id = payload.get("kind"), payload.get("group_id")
            group = self._find_group(document, kind, group_id)
            if delete:
                entry_id = payload.get("entry_id")
                if not isinstance(entry_id, str) or not entry_id:
                    raise PolicyDocumentError("entry_id parameter required")
                if not any(entry["id"] == entry_id for entry in group["entries"]):
                    raise PolicyDocumentError(f"Entry not found: {entry_id}")
                group["entries"] = [entry for entry in group["entries"] if entry["id"] != entry_id]
                entry = None
            else:
                value = normalize_entry_value(kind, payload.get("value"))
                entry_id = _identifier(payload.get("entry_id"), payload.get("friendly_name") or value)
                if any(entry["id"] == entry_id for entry in group["entries"]):
                    raise PolicyDocumentError(f"Entry already exists: {entry_id}")
                entry = {"id": entry_id, "friendly_name": payload.get("friendly_name", entry_id), "value": value}
                group["entries"].append(entry)
            saved = self._save(normalize_document(document))
            group = self._find_group(saved, kind, group_id)
            if not delete:
                entry = next(item for item in group["entries"] if item["id"] == entry_id)
            return {"policy_file": str(self.path), "exists": True, "kind": kind, "group_id": group_id,
                    "entry_id": entry_id, "entry": deepcopy(entry), "entries": deepcopy(group["entries"])}


def get_policy_service(config, config_path, daemon=None):
    service = getattr(daemon, "policy_service", None) if daemon is not None else None
    if service is None:
        service = PolicyService(config, config_path, daemon)
        if daemon is not None:
            daemon.policy_service = service
    return service
