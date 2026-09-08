"""
Repeater CompanionBridge with SQLite-backed preference persistence.

Persists full NodePrefs as a JSON blob so companion settings (including
auto-add config) survive repeater restarts. Merge-on-load supports
schema evolution when NodePrefs gains or loses fields.
"""

from __future__ import annotations

import dataclasses
import asyncio
import logging
import math
import threading
from collections.abc import Mapping
from enum import Enum
from typing import Any, Callable, Optional

from openhop_core.companion import CompanionBridge
from openhop_core.companion.constants import NODE_NAME_MAX_BYTES
from openhop_core.node.handlers.result import HandlerResult
from openhop_core.node.events import MeshEvents
from openhop_core.protocol.constants import PAYLOAD_TYPE_RESPONSE
from openhop_core.protocol.packet_utils import PathUtils

from repeater.companion_storage import storage_key_for_public_key

from .ack_tasks import install_owned_text_acks
from .binary_requests import BinaryRequestsMixin
from .discovery_requests import PathDiscoveryRequestsMixin
from .login_requests import LoginRequestsMixin
from .message_contacts import MessageContacts
from .utils import validate_companion_node_name

logger = logging.getLogger(__name__)

def _prefs_bytes_from_json(value: Any) -> bytes:
    """Restore binary preferences saved as hex without exposing key material."""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, str):
        try:
            return bytes.fromhex(value.strip())
        except ValueError:
            pass
    raise ValueError("Binary companion preference must contain hexadecimal bytes")


def _pref_value(value: Any, default: Any) -> Any:
    """Restore scalar preferences without truthiness or lossy numeric coercion."""
    if isinstance(default, Enum):
        return type(default)(value)
    if isinstance(default, bytes):
        return _prefs_bytes_from_json(value)
    if isinstance(default, bool):
        if type(value) is not bool:
            raise ValueError("Expected a boolean")
        return value
    if isinstance(default, int):
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ValueError("Expected an integer")
        if isinstance(value, float) and (not math.isfinite(value) or not value.is_integer()):
            raise ValueError("Expected a finite whole number")
        return int(value)
    if isinstance(default, float):
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ValueError("Expected a number")
        result = float(value)
        if not math.isfinite(result):
            raise ValueError("Expected a finite number")
        return result
    if isinstance(default, str):
        if not isinstance(value, str):
            raise ValueError("Expected a string")
        return value
    raise ValueError("Unsupported preference type")


def _to_json_safe(value: Any) -> Any:
    """Convert a value to a JSON-serializable form (avoids TypeError from enums, bytes, etc.)."""
    if isinstance(value, Enum):
        return _to_json_safe(value.value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, (list, tuple)):
        return [_to_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {k: _to_json_safe(v) for k, v in value.items()}
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _to_json_safe(getattr(value, f.name)) for f in dataclasses.fields(value)}
    return value


class RepeaterCompanionBridge(
    BinaryRequestsMixin, PathDiscoveryRequestsMixin, LoginRequestsMixin, CompanionBridge,
):
    """CompanionBridge that persists and loads prefs (full NodePrefs) via SQLite JSON blob."""

    def __init__(
        self,
        identity,
        packet_injector: Callable[..., Any],
        node_name: str = "pyMC",
        adv_type: int = 1,
        max_contacts: int = 1000,
        max_channels: int = 40,
        offline_queue_size: int = 512,
        radio_config: Optional[dict] = None,
        authenticate_callback: Optional[Callable[..., tuple[bool, int]]] = None,
        initial_contacts: Optional[Any] = None,
        *,
        radio_settings_getter: Optional[Callable[[], Mapping[str, Any]]] = None,
        max_tx_power_getter: Optional[Callable[[], Optional[int]]] = None,
        sqlite_handler=None,
        companion_hash: str = "",
        on_prefs_saved: Optional[Callable[[str], Optional[bool]]] = None,
    ) -> None:
        self._sqlite_handler = sqlite_handler
        # Retain the private attribute expected by the upstream contact-import
        # endpoint, but never let a short routing hash select persisted state.
        # companion_hash remains an accepted legacy constructor argument only.
        self._companion_hash = storage_key_for_public_key(identity.get_public_key())
        self._on_prefs_saved = on_prefs_saved
        self._stop_task = None
        self._login_admission_closed = False
        self._login_start_tasks = set()
        self._prefs_lock = threading.RLock()
        self._advert_event_handler = None
        self._contact_path_handler = None
        self._message_persistence_lock = None
        self._message_event_handler = None
        self._path_discovery_deadlines = {}
        self._init_request_ownership()
        super().__init__(
            identity=identity,
            packet_injector=packet_injector,
            node_name=node_name,
            adv_type=adv_type,
            max_contacts=max_contacts,
            max_channels=max_channels,
            offline_queue_size=offline_queue_size,
            radio_config=radio_config,
            authenticate_callback=authenticate_callback,
            initial_contacts=initial_contacts,
            radio_settings_getter=radio_settings_getter,
            max_tx_power_getter=max_tx_power_getter,
        )
        # The core invokes _load_prefs once before constructing its handlers.
        # Do not reread the database and risk replacing that state on a failure.
        # Handlers retain this service instance. Replace only its scheduler so
        # queued events are owned even before their subscriber starts running.
        self._event_service.publish_sync = self._publish_mesh_event
        # Keep the handler, its waiters and all existing consumers intact. Its
        # ordinary path callback runs after _update_contact_path; defer that
        # earlier live mutation when a save-first contact owner is available.
        response_handler = self._get_protocol_response_handler()
        self._core_update_contact_path = response_handler._update_contact_path
        self._core_send_reciprocal_path = response_handler._send_reciprocal_path
        response_handler._update_contact_path = self._prepare_contact_path_update
        response_handler._send_reciprocal_path = self._send_contact_reciprocal_path
        self._text_contact_store = self._get_text_handler().contacts
        install_owned_text_acks(self)

    def _publish_mesh_event(self, event_type, data):
        event = self._event_service.publish(event_type, data)
        try:
            self._spawn_background_task(event, "companion mesh event")
        except RuntimeError:
            event.close()
            raise

    def set_advert_event_handler(self, handler):
        """Bind the contact persistence owner before any companion RX is admitted."""
        self._advert_event_handler = handler

    def set_contact_path_handler(self, handler):
        """Bind an awaited save-first route owner independently of TCP clients."""
        self._contact_path_handler = handler

    def set_message_persistence_lock(self, lock):
        """Serialize queue admission with its persistence and client consumers."""
        self._message_persistence_lock = lock

    def set_message_event_handler(self, handler):
        """Bind durable message admission before public notification callbacks."""
        self._get_text_handler().contacts = (
            self._text_contact_store if handler is None else MessageContacts(self._text_contact_store)
        )
        self._message_event_handler = handler

    async def _fire_callbacks(self, event_name, *args):
        handler = self._message_event_handler
        if event_name == "message_event" and handler is not None:
            try:
                # The existing ingress wrapper owns the message lock here.
                # This owner survives TCP callback resets and handles storage
                # before those callbacks notify the client about its RAM inbox.
                await handler(*args)
            except Exception as exc:
                logger.warning("Companion message could not be saved (%s)", type(exc).__name__)
        return await super()._fire_callbacks(event_name, *args)

    async def _handle_new_message(self, data):
        lock = self._message_persistence_lock
        if lock is None:
            return await super()._handle_new_message(data)
        async with lock:
            return await super()._handle_new_message(data)

    async def _handle_new_channel_message(self, data):
        lock = self._message_persistence_lock
        if lock is None:
            return await super()._handle_new_channel_message(data)
        async with lock:
            return await super()._handle_new_channel_message(data)

    async def _handle_group_data_packet(self, packet):
        lock = self._message_persistence_lock
        if lock is None:
            return await super()._handle_group_data_packet(packet)
        async with lock:
            return await super()._handle_group_data_packet(packet)

    @staticmethod
    def _valid_decrypted_path(path_len, decrypted):
        if (not isinstance(path_len, int) or isinstance(path_len, bool)
                or not 0 <= path_len <= 255 or not PathUtils.is_valid_path_len(path_len)
                or not isinstance(decrypted, (bytes, bytearray, memoryview))):
            return False
        return (len(decrypted) >= PathUtils.get_path_byte_len(path_len) + 2
                and decrypted[0] == path_len)

    def _is_pending_path_discovery(self, path_len, decrypted):
        self._prune_path_discovery_requests()
        if not self._valid_decrypted_path(path_len, decrypted):
            return False
        extra_offset = 1 + PathUtils.get_path_byte_len(path_len)
        # MyMesh::onContactPathRecv checks RESPONSE plus more than four extra
        # bytes (the reflected tag followed by response data). Authentication
        # and full sender matching already happened in the protocol handler.
        if (decrypted[extra_offset] & 0x0F != PAYLOAD_TYPE_RESPONSE
                or len(decrypted) <= extra_offset + 5):
            return False
        tag = int.from_bytes(decrypted[extra_offset + 1:extra_offset + 5], "little")
        return self._claim_path_discovery(tag)

    def _prepare_contact_path_update(self, public_key, src_hash, path_len, decrypted):
        if self._contact_path_handler is None:
            return self._core_update_contact_path(public_key, src_hash, path_len, decrypted)
        if (not isinstance(public_key, bytes) or len(public_key) != 32
                or not self._valid_decrypted_path(path_len, decrypted)):
            return False
        if self._is_pending_path_discovery(path_len, decrypted):
            return False
        # Returning True asks the existing handler to await its path callback;
        # neither the Contact nor its proxy has been modified at this point.
        return self.contacts.get_by_key(public_key) is not None

    async def _on_contact_path_updated(self, public_key, path_len, path_bytes):
        if self._contact_path_handler is None:
            return await super()._on_contact_path_updated(public_key, path_len, path_bytes)
        try:
            return await self._contact_path_handler(public_key, path_len, path_bytes)
        except Exception as exc:
            # Do not throw through _decrypt_protocol_response: it would lose
            # authenticated ownership before the handler can process ACK/data.
            # The owner leaves the live route unchanged and emits no update.
            logger.warning("Companion path could not be applied (%s)", type(exc).__name__)
            return False

    async def _send_contact_reciprocal_path(self, src_hash, secret, packet, decrypted, path_len):
        if (self._contact_path_handler is not None
                and self._is_pending_path_discovery(path_len, decrypted)):
            # The predicate above skipped the route callback, so no await has
            # intervened. The same pending tag is consumed later by the normal
            # binary-response callback, which reports both discovery paths.
            return
        return await self._core_send_reciprocal_path(
            src_hash, secret, packet, decrypted, path_len,
        )

    async def _handle_mesh_event(self, event_type, data):
        if event_type == MeshEvents.NODE_DISCOVERED and self._advert_event_handler is not None:
            try:
                # Bypass the upstream projection's timestamp clamp and
                # mutate-first contact application. The owner handles verified
                # adverts whether or not a TCP client has installed callbacks.
                await self._advert_event_handler(data)
            except Exception as exc:
                logger.warning("Companion advert could not be applied (%s)", type(exc).__name__)
            return
        await super()._handle_mesh_event(event_type, data)

    async def _apply_advert_to_stores(self, contact, inbound_path=None, *, path_len_encoded=None):
        # Standalone bridges without a frame/persistence owner still use the
        # core flow. A temporary anonymous recipient is a new real admission,
        # not an existing-contact bypass of type, hop and capacity filters.
        existing = self.contacts.get_by_key(contact.public_key)
        if existing is not None and existing.adv_type == 0:
            self.contacts.remove(contact.public_key)
        return await super()._apply_advert_to_stores(
            contact, inbound_path, path_len_encoded=path_len_encoded,
        )

    async def process_received_packet(self, packet):
        if self._stop_task is not None:
            return HandlerResult.not_for_us()
        # Router timeout/cancellation must not orphan an executor-backed
        # persistence callback. Bridge shutdown owns and drains this work.
        task = self._spawn_background_task(super().process_received_packet(packet), "companion RX")
        return await asyncio.shield(task)

    async def _start_login_request(self, pub_key: bytes, password: str) -> dict:
        if self._login_admission_closed or self._stop_task is not None:
            return {"success": False, "error": "bad_state", "reason": "Companion is stopping"}
        # HTTP may time out while the initial multi-channel transmission is
        # still running. Own it before the first await, not just its later RF
        # response waiter. This wrapper is login-specific: binary/PATH requests
        # deliberately register replies on their initiating command task.
        work = super()._start_login_request(pub_key, password)
        try:
            task = self._spawn_background_task(work, "companion login start")
        except BaseException:
            work.close()
            raise
        self._login_start_tasks.add(task)
        task.add_done_callback(self._login_start_tasks.discard)
        return await asyncio.shield(task)

    async def send_login(self, pub_key: bytes, password: str) -> dict:
        started = await self._start_login_request(pub_key, password)
        if not started.get("success"):
            return {"success": False, "reason": started.get("reason", "Login failed")}
        # A cancelled API waiter must not cancel the admitted response/retries.
        return await asyncio.shield(started["task"])

    def stop_login_admission(self):
        self._login_admission_closed = True

    async def drain_login_starts(self):
        """Join admitted initial sends before the caller releases radio resources."""
        self.stop_login_admission()
        while self._login_start_tasks:
            await asyncio.gather(
                *(asyncio.shield(task) for task in tuple(self._login_start_tasks)),
                return_exceptions=True,
            )

    async def stop(self):
        if self._stop_task is None:
            self.stop_login_admission()
            self._stop_task = asyncio.create_task(self._finish_stop(), name="companion-bridge-stop")
        await asyncio.shield(self._stop_task)

    async def _finish_stop(self):
        await self.drain_login_starts()
        await super().stop()
        # Callers stop external producers first. A callback may enqueue another
        # owned callback, so continue until the whole set has settled.
        while self._background_tasks:
            await asyncio.gather(*tuple(self._background_tasks), return_exceptions=True)
        self._pending_discovery_tags.clear()
        self._path_discovery_deadlines.clear()
        self._clear_request_ownership()

    def import_private_key(self, key: bytes) -> bool:
        # Routing registration, protocol handlers and storage all belong to the
        # constructed identity. Use the Console's staged replacement + restart.
        return False

    async def send_trace_path(self, pub_key, tag, auth_code, flags=0):
        """Trace only a known route, never its unused persisted buffer bytes."""
        contact = self.contacts.get_by_key(pub_key)
        if contact is None or isinstance(flags, bool) or not isinstance(flags, int) or not 0 <= flags <= 255:
            return False
        encoded = contact.out_path_len
        if (not isinstance(encoded, int) or isinstance(encoded, bool)
                or not 0 <= encoded <= 255 or not PathUtils.is_valid_path_len(encoded)):
            return False
        required = PathUtils.get_path_byte_len(encoded)
        path = contact.out_path
        if not isinstance(path, bytes) or len(path) < required:
            return False
        width = PathUtils.trace_payload_hash_width(flags)
        if required:
            # TRACE uses widths 1/2/4/8, while stored routes use 1/2/3. Do not
            # silently reinterpret a mismatched or three-byte route.
            if width != PathUtils.get_path_hash_size(encoded):
                return False
            path = path[:required]
        else:
            # A known zero-hop route probes the peer itself, at the requested
            # TRACE width, regardless of any stale bytes in its stored buffer.
            path = contact.public_key[:width]
        result = await self.send_trace_path_raw(tag, auth_code, flags, path)
        return result.success

    def _validated_prefs(self, stored):
        """Merge declared fields into a detached, validated candidate."""
        if not isinstance(stored, dict):
            raise ValueError("Companion preferences must contain an object")
        candidate = dataclasses.replace(self.prefs)
        defaults = type(self.prefs)()
        for field in dataclasses.fields(candidate):
            value = stored.get(field.name, getattr(candidate, field.name))
            try:
                value = _pref_value(value, getattr(defaults, field.name))
            except (TypeError, ValueError, OverflowError):
                raise ValueError(f"Invalid companion preference: {field.name}") from None
            setattr(candidate, field.name, value)

        # Protocol names preserve whitespace and may be empty. Console/config
        # naming policy applies only to the optional YAML mirror below.
        if len(candidate.node_name.encode("utf-8")) > NODE_NAME_MAX_BYTES:
            raise ValueError("Companion node name exceeds the protocol byte limit")
        bounds = {
            "latitude": (-90, 90), "longitude": (-180, 180),
            "path_hash_mode": (0, 2), "autoadd_max_hops": (0, 64),
            "telemetry_mode_base": (0, 3), "telemetry_mode_location": (0, 3),
            "telemetry_mode_environment": (0, 3),
            "manual_add_contacts": (0, 255), "autoadd_config": (0, 255),
            "multi_acks": (0, 255), "client_repeat": (0, 255),
            "adv_type": (0, 255), "advert_loc_policy": (0, 255),
            "rx_delay_base": (0, 0xFFFFFFFF / 1000),
            "airtime_factor": (0, 0xFFFFFFFF / 1000),
        }
        for field, (minimum, maximum) in bounds.items():
            if not minimum <= getattr(candidate, field) <= maximum:
                raise ValueError(f"Companion preference out of range: {field}")
        if len(candidate.default_scope_key) not in (0, 16):
            raise ValueError("Companion default scope key must be empty or 16 bytes")
        if (bool(candidate.default_scope_name.strip()) != bool(candidate.default_scope_key)
                or len(candidate.default_scope_name) > 30):
            raise ValueError("Companion default scope name and key do not match")
        return candidate

    def _sync_name(self, name):
        if self._on_prefs_saved and self._on_prefs_saved(name) is False:
            raise RuntimeError("Could not persist companion name in configuration")

    def _update_prefs(self, **updates):
        # HTTP setters run on worker threads; TCP setters run on the loop.
        # Serialize the entire read/modify/save/publish operation, not only SQL.
        with self._prefs_lock:
            candidate = self._validated_prefs(updates)
            mirror_name = "node_name" in updates and self._on_prefs_saved is not None
            if mirror_name:
                try:
                    config_name = validate_companion_node_name(candidate.node_name)
                except ValueError:
                    if self._sqlite_handler is None:
                        raise ValueError("This companion name requires SQLite persistence") from None
                    # SQLite is authoritative for names the Console config
                    # cannot represent. Do not make that YAML invalid on boot.
                    mirror_name = False
                else:
                    if self._sqlite_handler is None and config_name != candidate.node_name:
                        raise ValueError("This companion name requires SQLite persistence")
            if self._sqlite_handler is not None:
                saved = self._sqlite_handler.companion_save_prefs(
                    str(self._companion_hash), _to_json_safe(dataclasses.asdict(candidate))
                )
                if not saved:
                    raise RuntimeError("Companion preferences could not be saved")
            elif mirror_name:
                # Without SQLite the callback is the name's only durable save.
                self._sync_name(candidate.node_name)

            # Current core consumers obtain a copy or dereference self.prefs;
            # none retains the old object. Publish only the committed candidate.
            self.prefs = candidate
            self._apply_multi_acks_pref()
            if mirror_name and self._sqlite_handler is not None:
                try:
                    self._sync_name(candidate.node_name)
                except Exception:
                    # SQLite already committed. Do not undo its active state.
                    raise RuntimeError(
                        "Companion preferences saved, but the configuration name copy failed; retry the rename"
                    ) from None

    # Preserve the core setters' wire normalization, but save before publishing
    # instead of calling their mutate-first _save_prefs hook.
    def set_advert_name(self, name: str) -> None:
        name = name.encode("utf-8")[:NODE_NAME_MAX_BYTES].decode("utf-8", errors="ignore")
        self._update_prefs(node_name=name)

    def set_advert_latlon(self, lat: float, lon: float) -> None:
        self._update_prefs(latitude=lat, longitude=lon)

    def set_tuning_params(self, rx_delay: float, airtime_factor: float) -> None:
        self._update_prefs(rx_delay_base=rx_delay, airtime_factor=airtime_factor)

    def set_other_params(self, manual_add, telemetry_modes, advert_loc_policy, multi_acks) -> None:
        self._update_prefs(
            manual_add_contacts=manual_add,
            telemetry_mode_base=telemetry_modes & 0x03,
            telemetry_mode_location=(telemetry_modes >> 2) & 0x03,
            telemetry_mode_environment=(telemetry_modes >> 4) & 0x03,
            advert_loc_policy=advert_loc_policy, multi_acks=multi_acks,
        )

    def set_path_hash_mode(self, mode: int) -> None:
        self._update_prefs(path_hash_mode=mode)

    def set_autoadd_config(self, config: int, max_hops: Optional[int] = None) -> None:
        updates = {"autoadd_config": config}
        if max_hops is not None:
            updates["autoadd_max_hops"] = min(max_hops, 64)
        self._update_prefs(**updates)

    def set_client_repeat(self, value: int) -> None:
        self._update_prefs(client_repeat=int(value) & 0xFF)

    def set_default_flood_scope(self, scope_name, transport_key) -> bool:
        if not scope_name or not transport_key or len(transport_key) < 16:
            self._update_prefs(default_scope_name="", default_scope_key=b"")
            return True
        normalized = scope_name[:30].strip()
        if not normalized:
            return False
        self._update_prefs(default_scope_name=normalized, default_scope_key=bytes(transport_key[:16]))
        return True

    def _load_prefs(self) -> None:
        """Fail activation on unreadable state; an absent row keeps defaults."""
        if self._sqlite_handler is None:
            return
        with self._prefs_lock:
            stored = self._sqlite_handler.companion_load_prefs(self._companion_hash)
            if stored is None:
                return
            self.prefs = self._validated_prefs(stored)
