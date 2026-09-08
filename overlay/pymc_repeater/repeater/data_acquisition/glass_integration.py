"""Save-first Glass configuration updates using the repeater's transactions.

Keep upstream transport, command and telemetry behavior. Add save-first
persistence, certificate-set activation, and two-phase shutdown ownership.
"""

import asyncio
from copy import deepcopy
import json
import logging
import os
from pathlib import Path
import ssl
import sys
import tempfile
from urllib.parse import urlsplit

from repeater.atomic_file import atomic_write_text
from repeater.config import CONFIG_WRITE_LOCK

from .glass_handler import GlassHandler as _UpstreamGlassHandler

logger = logging.getLogger(__name__)


class GlassHandler(_UpstreamGlassHandler):
    @staticmethod
    def _validate_glass_settings(settings):
        """Reject values that would stop the upstream inform loop on reload."""
        if not isinstance(settings, dict):
            raise ValueError("glass must be an object")
        for key in ("request_timeout_seconds", "inform_interval_seconds"):
            if key in settings and (isinstance(settings[key], bool) or not isinstance(settings[key], int)):
                raise ValueError(f"glass.{key} must be an integer")
        for key in ("enabled", "verify_tls"):
            if key in settings and not isinstance(settings[key], bool):
                raise ValueError(f"glass.{key} must be a boolean")
        for key in ("base_url", "api_token", "cert_store_dir", "client_cert_path", "client_key_path", "ca_cert_path"):
            if key in settings and settings[key] is not None:
                if not isinstance(settings[key], str) or "\x00" in settings[key]:
                    raise ValueError(f"glass.{key} must be a string without NUL characters")
        base_url = settings.get("base_url", "http://localhost:8080")
        if not isinstance(base_url, str):
            raise ValueError("glass.base_url must be a string")
        parsed = urlsplit(base_url.strip() or "http://localhost:8080")
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("glass.base_url must be an HTTP or HTTPS URL")
        # Accessing port also validates malformed/out-of-range URL ports.
        if parsed.port == 0:
            raise ValueError("glass.base_url port must be between 1 and 65535")

    def _save_managed_settings(self, updates, *, replace):
        """Preserve managed shallow-patch semantics without partial JSON writes."""
        if not isinstance(updates, dict):
            return False, "glass_managed must be an object"
        try:
            with CONFIG_WRITE_LOCK:
                path = self._managed_settings_path()
                current = {}
                if not replace:
                    try:
                        current = json.loads(path.read_text(encoding="utf-8"))
                    except FileNotFoundError:
                        pass
                    if not isinstance(current, dict):
                        raise ValueError("Existing managed settings must be an object")
                current.update(deepcopy(updates))
                atomic_write_text(path, json.dumps(
                    current, indent=2, sort_keys=True, allow_nan=False
                ), mode=0o600)
            return True, "Managed settings updated"
        except Exception as exc:
            return False, f"Failed writing managed settings: {exc}"

    def _apply_config_update(self, updates, merge_mode="patch"):
        if not isinstance(updates, dict) or not updates:
            return False, "Config update payload must be a non-empty object"
        if not isinstance(merge_mode, str) or merge_mode.strip().lower() not in ("patch", "replace"):
            return False, "merge_mode must be patch or replace"
        replace = merge_mode.strip().lower() == "replace"
        values = deepcopy(updates)
        has_managed = "glass_managed" in values
        managed = values.pop("glass_managed", None)
        if has_managed and not isinstance(managed, dict):
            return False, "glass_managed must be an object"
        if any(not isinstance(section, str) for section in values):
            return False, "Configuration section names must be strings"
        for section in ("repeater", "mesh", "delays", "duty_cycle", "glass", "identities", "radio", "wm1303", "bridge"):
            if section in values and not isinstance(values[section], dict):
                return False, f"{section} must be an object"
        if "identities" in values:
            return False, "Manage companion and room identities in the Console"

        current_type = str(self.config.get("radio_type", "sx1262")).strip().lower()
        target_type = values.get("radio_type", current_type)
        if not isinstance(target_type, str) or target_type.strip().lower() != current_type:
            return False, "Changing radio_type requires local setup and a service restart"
        if "radio_type" in values:
            values["radio_type"] = current_type
        repeater = values.get("repeater", {})
        if "security" in repeater and not isinstance(repeater["security"], dict):
            return False, "repeater.security must be an object"
        if "node_name" in repeater and (not isinstance(repeater["node_name"], str) or not repeater["node_name"].strip()):
            return False, "repeater.node_name must be a non-empty string"
        if replace and "repeater" in values and "node_name" not in repeater:
            return False, "Repeater replacement must include node_name; use patch to preserve it"
        security = repeater.get("security", {})
        for key in ("admin_password", "guest_password", "jwt_secret"):
            if key in security and not isinstance(security[key], str):
                return False, f"repeater.security.{key} must be a string"

        manager = self.config_manager
        if values and manager is None:
            return False, "Configuration manager unavailable; nothing was changed"
        live_sections = [section for section in values
                         if section in ("repeater", "mesh", "delays", "duty_cycle", "glass")]
        restart_required = any(section not in live_sections for section in values)
        if (any(key in repeater for key in ("identity_key", "identity_file"))
                or any(key in security for key in ("jwt_secret", "jwt_expiry_minutes"))
                or (replace and "repeater" in values)):
            restart_required = True

        managed_saved = False
        saved = False
        result = {}
        try:
            with CONFIG_WRITE_LOCK:
                if values:
                    # Validate the saved input before changing either file.
                    candidate = manager.read_saved_config()
                    # Config may contain a staged radio-type change. Inspect the
                    # actual backend too, without importing unused radio drivers.
                    backend_module = sys.modules.get("openhop_core.hardware.wm1303_backend")
                    backend_class = getattr(backend_module, "WM1303Backend", None)
                    active_wm1303 = backend_class is not None and isinstance(
                        getattr(self.daemon_instance, "radio", None), backend_class
                    )
                    saved_type = str(candidate.get("radio_type", "sx1262")).strip().lower()
                    if active_wm1303 or "wm1303" in (current_type, saved_type):
                        if any(section in values for section in ("radio", "wm1303", "sx1261_lbt", "sx1262")):
                            return False, "Configure WM1303 radio settings in the Manager"
                        bridge = values.get("bridge", {})
                        for old_bridge in (candidate.get("bridge") or {}, self.config.get("bridge") or {}):
                            if any(key in bridge or (replace and "bridge" in values and key in old_bridge)
                                   for key in ("rules", "bridge_rules")):
                                return False, "Configure WM1303 bridge rules in the Manager"
                    old_repeater = candidate.get("repeater") or {}
                    runtime_repeater = self.config.get("repeater") or {}
                    if replace and "repeater" in values:
                        # Do not fall back to initial/default credentials because
                        # a replacement omitted the security section or secrets.
                        for old in (old_repeater, runtime_repeater):
                            if "security" in old and "security" not in repeater:
                                return False, "Repeater replacement must include security; use patch to preserve it"
                            for key in ("admin_password", "guest_password", "jwt_secret"):
                                if key in (old.get("security") or {}) and key not in security:
                                    return False, f"Repeater replacement must include security.{key}; use patch to preserve it"
                    for key in ("identity_key", "identity_file"):
                        if replace and "repeater" in values and key not in repeater:
                            if key in old_repeater or key in runtime_repeater:
                                return False, f"Repeater replacement must include {key}; use patch to preserve it"
                        if key in repeater:
                            previous = old_repeater.get(key, runtime_repeater.get(key))
                            requested = repeater[key]
                            if key == "identity_key":
                                # JSON exports encode YAML binary keys as hex.
                                if previous is None:
                                    previous = runtime_repeater.get(key)
                                if isinstance(requested, str):
                                    requested = bytes.fromhex(requested)
                                if isinstance(previous, str):
                                    previous = bytes.fromhex(previous)
                                if not isinstance(requested, bytes) or len(requested) not in (32, 64):
                                    return False, "Manage node identity changes in the Console"
                            if requested != previous:
                                return False, "Manage node identity changes in the Console"
                            repeater[key] = requested
                    if replace:
                        candidate.update(deepcopy(values))
                    else:
                        manager._merge_config(candidate, values)
                    if "glass" in values:
                        self._validate_glass_settings(candidate["glass"])
                    if has_managed:
                        glass = candidate.get("glass") or {}
                        directory = Path(glass.get("cert_store_dir") or "/etc/openhop_repeater/glass")
                        if directory.expanduser().resolve() != Path(self.cert_store_dir).expanduser().resolve():
                            return False, "Change the managed-settings directory and glass_managed in separate updates"
                if has_managed:
                    # Managed-only updates also reload the current YAML settings.
                    if "glass" not in values:
                        self._validate_glass_settings(self.config.get("glass", {}))
                    ok, message = self._save_managed_settings(managed, replace=replace)
                    if not ok:
                        return False, message
                    managed_saved = True
                if values:
                    result = manager.update_and_save(
                        values, live_update=bool(live_sections),
                        live_update_sections=live_sections, replace_sections=replace,
                    )
                    saved = bool(result.get("saved"))
                    if not saved:
                        prefix = "Managed settings saved, but configuration was not saved: " if managed_saved else ""
                        return False, prefix + str(result.get("error", "Configuration save failed"))

            if "glass" in values or has_managed:
                if replace and "glass" in values and "inform_interval_seconds" not in values["glass"]:
                    self.inform_interval_seconds = 30
                self._reload_runtime_settings()
                self._sync_mqtt_publisher()
            if live_sections and not result.get("live_updated"):
                restart_required = True
            message = ("Config replaced" if replace else "Config patched") if values else "Managed settings updated"
            if restart_required:
                message += "; saved, but a service restart is required to apply staged settings"
            return True, message
        except Exception as exc:
            if saved or managed_saved:
                return False, f"Settings were saved, but could not be fully applied: {exc}"
            return False, f"Configuration was not saved: {exc}"

    def _apply_cert_renewal(self, response):
        """Publish one validated credential set through the atomic YAML save.

        Immutable paths also make the upstream MQTT signature change, forcing
        its TLS context to reload. Previous credentials remain available for
        readers and configuration backups; renewal never overwrites them.
        """
        fields = {
            "client_cert_path": ("client_cert", "glass-client.crt"),
            "client_key_path": ("client_key", "glass-client.key"),
            "ca_cert_path": ("ca_cert", "glass-ca.crt"),
        }
        if not isinstance(response, dict) or not all(
            isinstance(response.get(field), str) and response[field].strip()
            for field, _ in fields.values()
        ):
            return False, "Missing certificate payload values"
        manager = self.config_manager
        if manager is None:
            return False, "Configuration manager unavailable; certificates were not changed"

        generation = None
        paths = {}
        saved = False
        cleanup_allowed = True
        try:
            with CONFIG_WRITE_LOCK:
                candidate = manager.read_saved_config()
                glass = candidate.get("glass", self.config.get("glass", {})) or {}
                self._validate_glass_settings(glass)
                self._validate_glass_settings(self.config.get("glass", {}))
                directory = Path(glass.get("cert_store_dir") or "/etc/openhop_repeater/glass").expanduser().resolve()
                directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                generation = Path(tempfile.mkdtemp(prefix="renewal-", dir=directory))
                paths = {key: str(generation / filename) for key, (_, filename) in fields.items()}
                for key, (field, _) in fields.items():
                    atomic_write_text(paths[key], response[field], overwrite=False, mode=0o600)

                def reject_password():
                    raise ValueError("Encrypted Glass private keys are not supported")

                # Parsing and key-pair checks only: remote authentication and
                # certificate validity dates still require a TLS handshake.
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                context.load_verify_locations(cafile=paths["ca_cert_path"])
                context.load_cert_chain(
                    certfile=paths["client_cert_path"], keyfile=paths["client_key_path"],
                    password=reject_password,
                )
                # Flush the new directory entries before YAML can reference
                # them. Failure here leaves the previous configuration intact.
                for parent in (generation, directory, directory.parent):
                    descriptor = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                    try:
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)

                # If persistence unexpectedly raises after committing, retain
                # the set: deleting paths with uncertain commit status is unsafe.
                cleanup_allowed = False
                result = manager.update_and_save(
                    {"glass": paths}, live_update=True, live_update_sections=["glass"]
                )
                saved = bool(result.get("saved"))
                cleanup_allowed = not saved
                if not saved:
                    return False, str(result.get("error", "Certificate paths were not saved"))
                if not result.get("success"):
                    return False, "Certificate paths saved, but activation was incomplete; restart required"

            self._reload_runtime_settings()
            self._sync_mqtt_publisher()
            message = "Certificate paths saved; Glass TLS settings reloaded"
            if not result.get("live_updated"):
                message += "; service restart required to apply staged settings"
            return True, message
        except Exception as exc:
            if saved:
                return False, f"Certificate paths saved, but activation failed; restart required: {exc}"
            if not cleanup_allowed:
                return False, f"Certificate save status uncertain; new files retained for recovery: {exc}"
            return False, f"Certificate renewal was not saved: {exc}"
        finally:
            if generation is not None and cleanup_allowed:
                # Only this invocation's uncommitted files are eligible. Never
                # prune prior generations or user-provided certificate files.
                for path in paths.values():
                    try:
                        Path(path).unlink(missing_ok=True)
                    except OSError as exc:
                        logger.warning("Could not remove uncommitted Glass file %s: %s", path, exc)
                try:
                    generation.rmdir()
                except OSError as exc:
                    logger.warning("Could not remove uncommitted Glass directory %s: %s", generation, exc)

    async def _execute_command_action(self, action, params):
        if action != "set_inform_interval":
            return await super()._execute_command_action(action, params)
        params = params if isinstance(params, dict) else {}
        interval = params.get("interval_seconds", params.get("interval"))
        if isinstance(interval, bool) or not isinstance(interval, int):
            return False, "interval_seconds must be an integer", None
        ok, message = self._apply_config_update({
            "glass": {"inform_interval_seconds": self._clamp_interval(interval)}
        })
        return ok, message, None

    async def stop_informing(self):
        """Drain the control task while leaving MQTT available to storage."""
        task = self._task
        if self._stop_event is not None:
            self._stop_event.set()
        if task is None:
            return
        try:
            # Cancelling the await cannot stop the executor's HTTP request.
            # Keep the inform task alive and owned until that request and its
            # response handling finish, before the daemon tears down services.
            await asyncio.shield(task)
        finally:
            if task.done() and self._task is task:
                self._task = None
                self._stop_event = None

    async def stop(self):
        """Close MQTT only after the inform task and storage have drained."""
        try:
            await self.stop_informing()
        finally:
            # On caller cancellation an unfinished inform task remains owned;
            # a later stop() can finish the drain and close its publisher.
            if self._task is None:
                self._close_mqtt_publisher()
