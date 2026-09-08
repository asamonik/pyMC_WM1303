import asyncio
import functools
import logging
import os
import signal
import sys
import socket
import time
from concurrent.futures import Future

from repeater.companion.utils import validate_companion_node_name, normalize_companion_identity_key
from repeater.config import get_radio_for_board, load_config
from repeater.data_acquisition.gps_service import GPSService
from repeater.config_manager import ConfigManager
from repeater.data_acquisition.glass_integration import GlassHandler
from repeater.engine import RepeaterHandler
from repeater.handler_helpers import (
    AdvertHelper,
    DiscoveryHelper,
    LoginHelper,
    PathHelper,
    ProtocolRequestHelper,
    TextHelper,
    TraceHelper,
)
from repeater.identity_manager import IdentityManager
from repeater.packet_router import PacketRouter
from repeater.room_lifecycle import RoomLifecycleMixin
from repeater.web.http_server import HTTPStatsServer, _log_buffer
from openhop_core.paths import resolve_config_path  # WM1303 v2.7: central config-path helper

# WM1303 overlay: telemetry-enabled protocol request helper (adds REQ_TYPE_GET_TELEMETRY_DATA)
try:
    from repeater.wm1303_telemetry_helper import WM1303ProtocolRequestHelper
except ImportError:
    # Fallback to upstream helper if WM1303 overlay is not present
    WM1303ProtocolRequestHelper = ProtocolRequestHelper

# WM1303 bridge (optional)
try:
    from repeater.bridge_engine import BridgeEngine  # noqa: F401
    _BRIDGE_AVAILABLE = True
except ImportError:
    _BRIDGE_AVAILABLE = False

# Uniform all-channel RX tracer (v2.4.7+)
try:
    from repeater.uniform_tracer import install_on_radio as _install_uniform_tracer
    from repeater.uniform_tracer import schedule_channel_e_wrap_retry as _uniform_tracer_retry_ch_e
    _UNIFORM_TRACER_AVAILABLE = True
except Exception:
    _install_uniform_tracer = None  # type: ignore[assignment]
    _uniform_tracer_retry_ch_e = None  # type: ignore[assignment]
    _UNIFORM_TRACER_AVAILABLE = False


def json_safe(obj):
    """Recursively convert bytes/bytearray to hex strings so any structure is
    JSON-serializable. Applied at the get_stats() exit so no bytes field from
    any source (repeater handler, bridge, gps, etc.) can break /api/stats.
    Reusable safeguard against bytes leaking into API responses.
    """
    if isinstance(obj, (bytes, bytearray)):
        return obj.hex()
    if isinstance(obj, dict):
        return {k: json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [json_safe(v) for v in obj]
    return obj


# Register packet-trace callbacks with openhop_core modules so they can emit
# TX-phase trace events without importing repeater.web (layering boundary).
try:
    from repeater.web.packet_trace import trace_event as _trace_event_fn
    from openhop_core.hardware import wm1303_backend as _wm1303_backend_mod
    from openhop_core.hardware import tx_queue as _tx_queue_mod
    if hasattr(_wm1303_backend_mod, 'set_trace_callback'):
        _wm1303_backend_mod.set_trace_callback(_trace_event_fn)
    if hasattr(_tx_queue_mod, 'set_trace_callback'):
        _tx_queue_mod.set_trace_callback(_trace_event_fn)
except Exception as _trace_setup_err:
    logging.getLogger(__name__).warning(
        'Packet-trace callback registration failed: %s', _trace_setup_err)


logger = logging.getLogger("RepeaterDaemon")


class RepeaterDaemon(RoomLifecycleMixin):

    def __init__(self, config: dict, radio=None):

        self.config = config
        self.radio = radio
        self.dispatcher = None
        self.repeater_handler = None
        self.local_hash = None
        self.local_identity = None
        self.identity_manager = None
        self.config_manager = None
        self.http_server = None
        self.trace_helper = None
        self.advert_helper = None
        self.discovery_helper = None
        self.login_helper = None
        self.text_helper = None
        self.path_helper = None
        self.protocol_request_helper = None
        self.glass_handler = None
        self.acl = None
        self.router = None
        self.companion_bridges: dict[int, object] = {}
        self.companion_frame_servers: list = []
        self._companion_activation_lock = asyncio.Lock()
        self._room_activation_tasks = set()
        self._room_sync_failure_event = asyncio.Event()
        self._room_sync_failure = None
        self.bridge_engine = None
        self.gps_service = None
        self._shutdown_started = False
        self._shutdown_task = None
        self._main_task = None
        self._dispatcher_task = None
        self._service_tasks = []
        self._metrics_retention = None

        log_level = config.get("logging", {}).get("level", "INFO")
        logging.basicConfig(
            level=getattr(logging, log_level),
            format=config.get("logging", {}).get("format"),
        )

        root_logger = logging.getLogger()
        _log_buffer.setLevel(getattr(logging, log_level))
        root_logger.addHandler(_log_buffer)

    async def initialize(self):

        logger.info(f"Initializing repeater: {self.config['repeater']['node_name']}")

        #-----------------------------------------------
        # Get the actual Network IP Address 
        try:
            # This looks for the IP assigned to the default hostname
            host_name = socket.gethostname()
            # We try to get the IP associated with the hostname
            self.network_ip = socket.gethostbyname(host_name)
            
            # If that still gives 127.0.x.x, let's try a different internal method
            if self.network_ip.startswith("127."):
                # UDP connect selects a local address without sending a packet.
                # Close the probe even when route lookup or getsockname fails.
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                    probe.connect(("10.255.255.255", 1))
                    self.network_ip = probe.getsockname()[0]
        except Exception as e:
            logger.warning(f"Could not determine network IP: {e}")
            self.network_ip = "Unknown"

        logger.info(f"System Network IP: {self.network_ip}")
        #-----------------------------------------------

        if self.radio is None:
            radio_type = self.config.get("radio_type", "sx1262")
            logger.info(f"Initializing radio hardware... (radio_type={radio_type})")
            try:
                self.radio = get_radio_for_board(self.config)

                # KISS modem: schedule RX callbacks on the event loop for thread safety
                if hasattr(self.radio, "set_event_loop"):
                    self.radio.set_event_loop(asyncio.get_running_loop())

                if hasattr(self.radio, "set_custom_cad_thresholds"):
                    # Load CAD settings from config, with defaults
                    cad_config = self.config.get("radio", {}).get("cad", {})
                    peak_threshold = cad_config.get("peak_threshold", 23)
                    min_threshold = cad_config.get("min_threshold", 11)

                    self.radio.set_custom_cad_thresholds(peak=peak_threshold, min_val=min_threshold)
                    logger.info(
                        f"CAD thresholds set from config: peak={peak_threshold}, min={min_threshold}"
                    )
                else:
                    logger.warning("Radio does not support CAD configuration")

                if hasattr(self.radio, "get_frequency"):
                    logger.info(f"Radio config - Freq: {self.radio.get_frequency():.1f}MHz")
                if hasattr(self.radio, "get_spreading_factor"):
                    logger.info(f"Radio config - SF: {self.radio.get_spreading_factor()}")
                if hasattr(self.radio, "get_bandwidth"):
                    logger.info(f"Radio config - BW: {self.radio.get_bandwidth()}kHz")
                if hasattr(self.radio, "get_coding_rate"):
                    logger.info(f"Radio config - CR: {self.radio.get_coding_rate()}")
                if hasattr(self.radio, "get_tx_power"):
                    logger.info(f"Radio config - TX Power: {self.radio.get_tx_power()}dBm")

                logger.info("Radio hardware initialized")

                # v2.4.7+: install uniform all-channel RX tracer
                # (hooks _dispatch_rx + channel_e callback so every RX emits
                # a 'received' trace for the Tracing tab, across all 5 channels,
                # with correct 1/2/3-byte hash-size detection).
                if _UNIFORM_TRACER_AVAILABLE and _install_uniform_tracer is not None:
                    try:
                        _install_uniform_tracer(self.radio)
                    except Exception as _ut_err:
                        logger.warning(f"Uniform tracer install failed: {_ut_err}")

                # v2.4.7+: register friendly channel-name map with packet_trace
                # so every trace_event() call (from bridge_engine, tx_queue,
                # uniform_tracer, etc.) displays the same UI-friendly channel
                # name (e.g. 'channel_e' -> 'EU-Narrow', 'channel_a' -> 'local-test').
                try:
                    from repeater.web.packet_trace import set_channel_name_map
                    _ch_map = getattr(self.radio, '_ch_id_to_ui_name', {}) or {}
                    if _ch_map:
                        set_channel_name_map(_ch_map)
                except Exception as _cn_err:
                    logger.debug(f"Channel-name map registration failed: {_cn_err}")
            except Exception as e:
                logger.error(f"Failed to initialize radio hardware: {e}")
                raise RuntimeError("Repeater requires real LoRa hardware") from e

        try:
            from openhop_core import LocalIdentity
            from openhop_core.node.dispatcher import Dispatcher

            self.dispatcher = Dispatcher(self.radio)
            logger.info("Dispatcher initialized")

            # Initialize Identity Manager for additional identities (e.g., room servers)
            self.identity_manager = IdentityManager(self.config)
            logger.info("Identity manager initialized")

            # Set up default repeater identity (not managed by identity manager)
            identity_key = self.config.get("repeater", {}).get("identity_key")
            if not identity_key:
                logger.error("No identity key found in configuration. Cannot init repeater.")
                raise RuntimeError("Identity key is required for repeater operation")

            local_identity = LocalIdentity(seed=identity_key)
            self.local_identity = local_identity
            self.dispatcher.local_identity = local_identity

            pubkey = local_identity.get_public_key()
            self.local_hash = pubkey[0]
            self.local_hash_bytes = bytes(pubkey[:3])

            logger.info(f"Local identity set: {local_identity.get_address_bytes().hex()}")
            local_hash_hex = f"0x{self.local_hash:02x}"
            logger.info(f"Local node hash (from identity): {local_hash_hex}")

            # v2.5.x: register local identity with the WM1303 radio backend so it
            # can do PATH-BASED echo classification (self_echo vs mesh_echo vs
            # unknown_echo) instead of the legacy time-based heuristic. This is
            # repeater-agnostic because each device has a unique pubkey.
            try:
                _path_hash_size = (self.config.get("mesh", {}).get("path_hash_mode", 0) or 0) + 1
                if hasattr(self.radio, "set_local_identity"):
                    self.radio.set_local_identity(pubkey, _path_hash_size)
            except Exception as _exc:
                logger.warning(f"Could not register local identity with radio backend: {_exc}")

            # Load additional identities from config (e.g., room servers)
            await self._load_additional_identities()

            self.dispatcher._is_own_packet = lambda pkt: False

            self.repeater_handler = RepeaterHandler(
                self.config, self.dispatcher, self.local_hash,
                local_hash_bytes=self.local_hash_bytes,
                send_advert_func=self.send_advert,
            )

            # Create router
            self.router = PacketRouter(self)
            await self.router.start()

            # Register router as entry point for ALL packets via fallback handler
            # All received packets flow through router → helpers → repeater engine
            self.dispatcher.register_fallback_handler(self._router_callback)
            logger.info("Packet router registered as fallback (catches all packets)")

            # Set default path hash mode for flood 0-hop packets (adverts, etc.)
            path_hash_mode = self.config.get("mesh", {}).get("path_hash_mode", 0)
            if path_hash_mode not in (0, 1, 2):
                logger.warning(
                    f"Invalid mesh.path_hash_mode={path_hash_mode}, must be 0/1/2; using 0"
                )
                path_hash_mode = 0
            self.dispatcher.set_default_path_hash_mode(path_hash_mode)
            mode_names = {0: "1-byte", 1: "2-byte", 2: "3-byte"}
            logger.info(
                f"Path hash mode set to {mode_names[path_hash_mode]} (mesh.path_hash_mode={path_hash_mode})"
            )

            # Create processing helpers (handlers created internally)
            self.trace_helper = TraceHelper(
                local_hash=self.local_hash,
                repeater_handler=self.repeater_handler,
                packet_injector=self._trace_packet_injector,
                log_fn=logger.info,
                local_identity=self.local_identity,
            )
            logger.info("Trace processing helper initialized")

            # Create advert helper for neighbor tracking
            self.advert_helper = AdvertHelper(
                local_identity=self.local_identity,
                storage=self.repeater_handler.storage if self.repeater_handler else None,
                config=self.config,
                log_fn=logger.info,
            )
            logger.info("Advert processing helper initialized")

            # Set up discovery handler if enabled
            allow_discovery = self.config.get("repeater", {}).get("allow_discovery", True)
            if allow_discovery:
                self.discovery_helper = DiscoveryHelper(
                    local_identity=self.local_identity,
                    packet_injector=self._response_injector,
                    node_type=2,
                    log_fn=logger.info,
                    debug_log_fn=logger.debug,
                )
                logger.info("Discovery processing helper initialized")
            else:
                logger.info("Discovery response handler disabled")

            # Create login helper (will create per-identity ACLs)
            # WM1303 v2.4.11: use bridge-aware _response_injector so login
            # responses go out via channel_e/f instead of classic radios[0]/[1].
            self.login_helper = LoginHelper(
                identity_manager=self.identity_manager,
                packet_injector=self._response_injector,
                log_fn=logger.info,
                sqlite_handler=self.repeater_handler.storage.sqlite_handler,
                config=self.config,
            )

            # Register default repeater identity
            self.login_helper.register_identity(
                name="repeater",
                identity=self.local_identity,
                identity_type="repeater",
                config=self.config,  # Pass full config so repeater can access top-level security section
            )

            # Register room server identities with their configs
            for name, identity, config in self.identity_manager.get_identities_by_type(
                "room_server"
            ):
                self.login_helper.register_identity(
                    name=name,
                    identity=identity,
                    identity_type="room_server",
                    config=config,  # Pass room-specific config
                )

            logger.info("Login processing helper initialized")

            # Initialize ConfigManager for centralized config management
            self.config_manager = ConfigManager(
                config_path=getattr(self, "config_path", str(resolve_config_path('config.yaml'))),
                config=self.config,
                daemon_instance=self,
            )
            logger.info("Config manager initialized")

            self.gps_service = GPSService(
                self.config,
                location_update_callback=self._update_repeater_location_from_gps,
            )
            self.gps_service.start()
            if self.config.get("gps", {}).get("enabled", False):
                logger.info("GPS diagnostics initialized")
            else:
                logger.info("GPS diagnostics disabled")

            # Initialize text message helper with per-identity ACLs
            # WM1303 v2.4.11: use bridge-aware _response_injector so CLI command
            # responses go out via channel_e/f instead of classic radios[0]/[1].
            self.text_helper = TextHelper(
                identity_manager=self.identity_manager,
                packet_injector=self._response_injector,
                acl_dict=self.login_helper.get_acl_dict(),  # Per-identity ACLs
                log_fn=logger.info,
                config_path=getattr(self, "config_path", None),  # For CLI to save changes
                config=self.config,  # For CLI to read/modify settings
                config_manager=self.config_manager,  # New centralized config manager
                sqlite_handler=(
                    self.repeater_handler.storage.sqlite_handler
                    if self.repeater_handler and self.repeater_handler.storage
                    else None
                ),  # For room server database
                send_advert_callback=self.send_advert,  # For CLI advert command
            )
            self.text_helper._loop = asyncio.get_running_loop()

            # Register default repeater identity for text messages
            self.text_helper.register_identity(
                name="repeater",
                identity=self.local_identity,
                identity_type="repeater",
                radio_config=self.config.get("radio", {}),
            )

            # Register room server identities for text messages
            room_start_tasks = []
            for name, identity, config in self.identity_manager.get_identities_by_type(
                "room_server"
            ):
                pending_before = set(self.text_helper._pending_tasks)
                self.text_helper.register_identity(
                    name=name,
                    identity=identity,
                    identity_type="room_server",
                    radio_config=config,  # Pass room-specific config (includes max_posts, etc.)
                )
                # Capture before yielding: upstream removes completed startup
                # tasks from its owned set, including failed ones.
                room_start_tasks.extend(
                    (name, task) for task in self.text_helper._pending_tasks - pending_before
                )

            logger.info("Text message processing helper initialized")

            # Initialize PATH packet helper for updating client out_path
            self.path_helper = PathHelper(
                acl_dict=self.login_helper.get_acl_dict(),  # Per-identity ACLs
                log_fn=logger.info,
                ack_received_callback=self.dispatcher._register_ack_received,
            )
            logger.info("PATH packet processing helper initialized")

            # Initialize protocol request handler for status/telemetry requests
            # WM1303 v2.4.11: use bridge-aware _response_injector so protocol
            # request responses go out via channel_e/f instead of classic radios.
            self.protocol_request_helper = WM1303ProtocolRequestHelper(
                identity_manager=self.identity_manager,
                packet_injector=self._response_injector,
                acl_dict=self.login_helper.get_acl_dict(),
                radio=self.radio,
                engine=self.repeater_handler,
                neighbor_tracker=self.advert_helper,
                config=self.config,
            )
            # Register repeater identity for protocol requests
            self.protocol_request_helper.register_identity(
                name="repeater", identity=self.local_identity, identity_type="repeater"
            )
            for name, identity, _ in self.identity_manager.get_identities_by_type("room_server"):
                self.protocol_request_helper.register_identity(
                    name=name, identity=identity, identity_type="room_server"
                )
            logger.info("Protocol request handler initialized")

            # Earlier identity loading only stages the manager entries. All
            # helpers now exist, so require actual room activation before READY.
            # RoomServer.start() is finite; its long-lived _sync_task is not.
            for name, task in room_start_tasks:
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    if task.cancelled():
                        raise RuntimeError(f"Room server {name!r} startup was cancelled") from None
                    raise
                except Exception as exc:
                    raise RuntimeError(f"Room server {name!r} failed to start") from exc
            for name, identity, _ in self.identity_manager.get_identities_by_type("room_server"):
                room = self._require_room_ready(name, identity)
                self._monitor_room_sync(name, room)

            # Load companion identities (CompanionBridge + frame server per companion)
            await self._load_companion_identities()

            # Subscribe to raw RX in pyMC_core so we can push PUSH_CODE_LOG_RX_DATA to companion clients.
            # Note: on WM1303 hardware the Dispatcher path is bypassed (BridgeEngine handles RF),
            # so this subscriber only fires for non-bridge radios.  The main companion delivery
            # path is via the BridgeEngine raw RX callback registered in _setup_bridge_engine.
            async def _async_raw_rx_wrapper(data, rssi, snr):
                self._on_raw_rx_for_companions(data, 'dispatcher', rssi, snr)
            self.dispatcher.add_raw_rx_subscriber(_async_raw_rx_wrapper)
            n = len(getattr(self, "companion_frame_servers", []))
            logger.info(
                "Raw RX subscriber registered (%s companion frame server(s)). Connect a client to see rx_log (0x88).",
                n,
            )

            # Subscribe to parsed packets (pre-dedup) so duplicate path variants
            # still appear in the web UI even though the Dispatcher blocks them.
            self.dispatcher.add_raw_packet_subscriber(self._on_raw_packet_for_dedup_logging)

            # When trace reaches final node, push PUSH_CODE_TRACE_DATA (0x89) to companion clients (firmware onTraceRecv)
            self.trace_helper.on_trace_complete = self._on_trace_complete_for_companions

            # Optional pyMC_Glass integration loop (inform/control plane)
            self.glass_handler = GlassHandler(
                config=self.config,
                daemon_instance=self,
                config_manager=self.config_manager,
            )
            await self.glass_handler.start()
            if (
                self.repeater_handler
                and self.repeater_handler.storage
                and hasattr(self.repeater_handler.storage, "set_glass_publisher")
            ):
                self.repeater_handler.storage.set_glass_publisher(self.glass_handler.publish_telemetry)

        except Exception as e:
            logger.error(f"Failed to initialize dispatcher: {e}")
            raise

    async def _load_additional_identities(self):
        from openhop_core import LocalIdentity

        identities_config = self.config.get("identities")
        if identities_config is None:
            identities_config = {}
        if not isinstance(identities_config, dict):
            raise RuntimeError("Cannot load room servers: identities must be a mapping")
        room_servers = identities_config.get("room_servers")
        if room_servers is None:
            room_servers = []
        if not isinstance(room_servers, list):
            raise RuntimeError("Cannot load room servers: identities.room_servers must be a list")

        # Every configured room must reach the later readiness gate. Staging
        # happens before helper construction, so failure here creates no room
        # workers and the ordinary initialization cleanup retains ownership.
        seen_names = {"repeater"}
        for index, room_config in enumerate(room_servers, 1):
            if not isinstance(room_config, dict):
                raise RuntimeError(f"Room server entry #{index} must be a mapping")
            name = room_config.get("name")
            if not isinstance(name, str) or not name.strip():
                raise RuntimeError(f"Room server entry #{index} requires a nonempty string name")
            if name.strip() in seen_names:
                raise RuntimeError(f"Room server {name!r} has a duplicate or reserved name")
            seen_names.add(name.strip())
            if room_config.get("type", "room_server") != "room_server":
                raise RuntimeError(f"Room server {name!r} must have type 'room_server'")
            if not isinstance(room_config.get("settings", {}), dict):
                raise RuntimeError(f"Room server {name!r} settings must be a mapping")

            identity_key = room_config.get("identity_key")
            if isinstance(identity_key, bytes):
                identity_key_bytes = identity_key
            elif isinstance(identity_key, str):
                try:
                    identity_key_bytes = bytes.fromhex(identity_key)
                except ValueError:
                    raise RuntimeError(f"Room server {name!r} identity_key is not valid hexadecimal") from None
            else:
                raise RuntimeError(f"Room server {name!r} requires an identity_key as bytes or hexadecimal text")
            if len(identity_key_bytes) not in (32, 64):
                raise RuntimeError(f"Room server {name!r} identity_key must contain 32 or 64 bytes")

            try:
                room_identity = LocalIdentity(seed=identity_key_bytes)
            except Exception as exc:
                raise RuntimeError(f"Room server {name!r} identity could not be loaded ({type(exc).__name__})") from exc

            try:
                success = self._register_identity_everywhere(
                    name=name,
                    identity=room_identity,
                    config=room_config,
                    identity_type="room_server",
                )
            except Exception as exc:
                raise RuntimeError(f"Room server {name!r} registration failed ({type(exc).__name__})") from exc
            if not success:
                raise RuntimeError(f"Room server {name!r} registration was rejected; check name and routing-hash conflicts")
            room_hash = room_identity.get_public_key()[0]
            logger.info(
                f"Loaded room server '{name}': hash=0x{room_hash:02x}, "
                f"address={room_identity.get_address_bytes().hex()}"
            )

        # Summary logging
        total_identities = len(self.identity_manager.list_identities())
        logger.info(f"Identity manager loaded {total_identities} total identities")

    async def _load_companion_identities(self) -> None:
        """Use the same setup/rollback path for startup and live additions."""
        companions = (self.config.get("identities") or {}).get("companions") or []
        for comp_config in companions:
            try:
                await self.add_companion_from_config(comp_config)
            except Exception as e:
                name = comp_config.get("name") if isinstance(comp_config, dict) else "<invalid>"
                logger.error("Failed to load companion '%s': %s", name, e, exc_info=True)

    def _sync_companion_node_name(
        self, companion_name: str, new_node_name: str, *, expected_public_key: bytes
    ) -> bool:
        from openhop_core import LocalIdentity

        validated = validate_companion_node_name(new_node_name)
        manager = self.config_manager
        if manager is None:
            raise RuntimeError("Cannot persist companion name before configuration is initialized")
        with manager._lock:
            saved = manager.read_saved_config()
            companions = (saved.get("identities") or {}).get("companions") or []
            for entry in companions:
                if entry.get("name") != companion_name:
                    continue
                # A replacement identity can be staged under this same name.
                # An old live companion must not rewrite its replacement's name.
                saved_key = entry.get("identity_key")
                try:
                    if isinstance(saved_key, str):
                        saved_key = bytes.fromhex(normalize_companion_identity_key(saved_key))
                    if (not isinstance(saved_key, bytes) or len(saved_key) not in (32, 64)
                            or LocalIdentity(seed=saved_key).get_public_key() != expected_public_key):
                        return False
                except (TypeError, ValueError):
                    return False
                settings = entry.setdefault("settings", {})
                if not isinstance(settings, dict):
                    raise ValueError("Saved companion settings must contain a mapping")
                if settings.get("node_name") == validated:
                    return True
                settings["node_name"] = validated
                result = manager.update_and_save(
                    {"identities": {"companions": companions}}, live_update=False
                )
                if not result.get("saved"):
                    raise RuntimeError("Could not persist companion name in configuration")
                return True
            # A staged deletion must not be undone by a still-active companion.
            return False

    async def add_companion_from_config(self, comp_config: dict) -> None:
        # Keep activation and rollback owned even when an HTTP caller times out.
        # Shutdown joins this lock before releasing radio/storage dependencies.
        async with self._companion_activation_lock:
            await self._add_companion_from_config(comp_config)

    async def _add_companion_from_config(self, comp_config: dict) -> None:
        """
        Load a single companion from config and register it (hot-reload).
        Creates RepeaterCompanionBridge, CompanionFrameServer, starts the server,
        and registers with identity_manager. Raises on error.
        """
        from openhop_core import LocalIdentity
        from openhop_core.companion.models import Channel

        from repeater.companion import RepeaterCompanionBridge
        from repeater.companion.frame_server_lifecycle import CompanionFrameServer
        from repeater.companion.constants import DEFAULT_PUBLIC_CHANNEL_SECRET
        from repeater.companion_storage import (
            companion_limits_from_settings, legacy_owner_from_settings,
            storage_key_for_public_key, validated_channel_rows,
        )

        if getattr(self, "_shutdown_started", False):
            raise RuntimeError("Cannot add a companion while the daemon is shutting down")
        if not isinstance(comp_config, dict):
            raise ValueError("Companion config must be a mapping")
        name = comp_config.get("name")
        identity_key = comp_config.get("identity_key")
        settings = comp_config.get("settings") or {}
        if not isinstance(settings, dict):
            raise ValueError("Companion settings must be a mapping")
        legacy_owner = legacy_owner_from_settings(settings)
        bridge_limits = companion_limits_from_settings(settings)

        if not name or not identity_key:
            raise ValueError("Companion config missing name or identity_key")

        if isinstance(identity_key, str):
            try:
                identity_key_bytes = bytes.fromhex(normalize_companion_identity_key(identity_key))
            except ValueError as e:
                raise ValueError(f"Companion '{name}' identity_key invalid hex: {e}") from e
        elif isinstance(identity_key, bytes):
            identity_key_bytes = identity_key
        else:
            raise ValueError(f"Companion '{name}' identity_key has unknown type")

        if len(identity_key_bytes) not in (32, 64):
            raise ValueError(
                f"Companion '{name}' identity_key must be 32 bytes (hex) or 64 bytes (MeshCore firmware key)"
            )

        # Already registered?
        if name == "repeater" or name in self.identity_manager.named_identities:
            raise ValueError(f"Companion '{name}' is already registered")

        identity = LocalIdentity(seed=identity_key_bytes)
        pubkey = identity.get_public_key()
        companion_hash = pubkey[0]
        companion_hash_str = f"0x{companion_hash:02x}"
        storage_key = storage_key_for_public_key(pubkey)

        if (companion_hash in self.companion_bridges
                or self.identity_manager.has_identity(companion_hash)
                or companion_hash == self.local_hash):
            raise ValueError(f"Companion hash 0x{companion_hash:02x} conflicts with a local identity")

        sqlite_handler = None
        if self.repeater_handler and self.repeater_handler.storage:
            sqlite_handler = self.repeater_handler.storage.sqlite_handler

        radio_config = (
            self.repeater_handler.radio_config
            if self.repeater_handler
            else self.config.get("radio", {})
        )

        node_name = validate_companion_node_name(settings.get("node_name", name))
        tcp_port = settings.get("tcp_port", 5000)
        bind_address = settings.get("bind_address", "0.0.0.0")
        tcp_timeout_raw = settings.get("tcp_timeout", 8 * 60 * 60)
        client_idle_timeout_sec = None if tcp_timeout_raw == 0 else int(tcp_timeout_raw)

        if sqlite_handler is not None:
            # No automatic claim of legacy state: its rows have no local public
            # key. A confirmed migration completes before anything can load or
            # save this identity's state, including the bridge constructor.
            sqlite_handler.prepare_companion_storage(pubkey, legacy_owner)

        bridge = RepeaterCompanionBridge(
            identity=identity,
            packet_injector=self._companion_injector,
            node_name=node_name,
            radio_config=radio_config,
            sqlite_handler=sqlite_handler,
            companion_hash=storage_key,
            on_prefs_saved=functools.partial(
                self._sync_companion_node_name, name, expected_public_key=pubkey
            ),
            **bridge_limits,
        )

        if sqlite_handler:
            contact_rows = sqlite_handler.companion_load_contacts(storage_key)
            if contact_rows is None:
                raise RuntimeError(f"Cannot load persisted contacts for companion '{name}'")
            if len(contact_rows) > bridge.contacts.max_contacts:
                raise ValueError(
                    f"Companion '{name}' has {len(contact_rows)} stored contacts but capacity "
                    f"is {bridge.contacts.max_contacts}; increase settings.max_contacts before starting"
                )
            if any(row.get("adv_type", 0) == 0 for row in contact_rows):
                # Anonymous request placeholders are intentionally omitted from
                # final snapshots. Do not load and then erase a persisted one.
                raise ValueError(f"Companion '{name}' contains persisted anonymous contacts; stored data was preserved")
            if contact_rows:
                records = []
                for row in contact_rows:
                    d = dict(row)
                    d["public_key"] = d.pop("pubkey", d.get("public_key", b""))
                    records.append(d)
                bridge.contacts.load_from_dicts(records)

            channel_rows = sqlite_handler.companion_load_channels(storage_key)
            if channel_rows is None:
                raise RuntimeError(f"Cannot load persisted channels for companion '{name}'")
            channel_rows = validated_channel_rows(channel_rows, max_channels=bridge.channels.max_channels)
            for row in channel_rows:
                ch = Channel(name=row["name"], secret=row["secret"])
                if not bridge.channels.set(row["channel_idx"], ch):
                    raise ValueError(f"Companion '{name}' channel could not be loaded")

            # FrameServer pops persisted messages directly from SQLite when its
            # live queue is empty. Preloading them here would deliver each twice.

        if bridge.get_channel(0) is None:
            bridge.set_channel(0, "Public", DEFAULT_PUBLIC_CHANNEL_SECRET)

        frame_server = CompanionFrameServer(
            bridge=bridge,
            companion_hash=companion_hash_str,
            port=tcp_port,
            bind_address=bind_address,
            client_idle_timeout_sec=client_idle_timeout_sec,
            sqlite_handler=sqlite_handler,
            local_hash=self.local_hash,
            stats_getter=self._get_companion_stats,
            control_handler=(
                self.discovery_helper.control_handler if self.discovery_helper else None
            ),
        )
        try:
            await bridge.start()
            await frame_server.start()
            if self._shutdown_started:
                raise RuntimeError("Companion startup interrupted by daemon shutdown")
            if not self.identity_manager.register_identity(
                name=name, identity=identity, config=comp_config, identity_type="companion"
            ):
                raise ValueError(f"Companion '{name}' could not register its identity")
        except BaseException:
            try:
                await frame_server.stop_clients()
            finally:
                try:
                    await bridge.stop()
                finally:
                    await frame_server.stop()
            raise
        self.companion_bridges[companion_hash] = bridge
        self.companion_frame_servers.append(frame_server)

        logger.info(
            f"Hot-reload: Loaded companion '{name}': hash=0x{companion_hash:02x}, "
            f"port={tcp_port}, bind={bind_address}, client_idle_timeout_sec={client_idle_timeout_sec}"
        )

    def _on_raw_rx_for_companions(self, data: bytes, source_name: str = '',
                                    rssi=None, snr=None) -> None:
        """Raw RX callback: push PUSH_CODE_LOG_RX_DATA (0x88) to connected
        companion clients.  Registered as a BridgeEngine raw RX callback so it
        is invoked for ALL RF packets BEFORE echo/dedup filtering, enabling
        Heard Repeats and node discovery in companion apps.

        Signature matches BridgeEngine._fire_raw_rx_callbacks:
            callback(data, source_name, rssi, snr)
        """
        servers = getattr(self, "companion_frame_servers", [])
        if not servers:
            return
        _rssi = int(rssi) if rssi is not None else 0
        _snr = float(snr) if snr is not None else 0.0
        for fs in servers:
            try:
                fs.push_rx_raw(_snr, _rssi, data)
            except Exception as e:
                logger.debug("Push RX raw to companion: %s", e)
        logger.info(
            "BridgeEngine raw RX → %d companion server(s) (%d bytes, src=%s, rssi=%s, snr=%s)",
            len(servers), len(data), source_name, _rssi, _snr
        )

    def _on_raw_packet_for_dedup_logging(self, pkt, data: bytes, analysis: dict) -> None:
        """Record duplicate packets for UI visibility.

        Called by Dispatcher's raw_packet_subscriber (pre-dedup) so we see
        all path variants.  Only records packets the engine has already seen;
        novel packets are left for the normal handler path.
        """
        if not self.repeater_handler:
            return
        if not self.repeater_handler.is_duplicate(pkt):
            return  # First variant — will reach engine via normal handler path
        rssi = getattr(pkt, "_rssi", 0) or 0
        snr = getattr(pkt, "_snr", 0.0) or 0.0
        self.repeater_handler.record_duplicate(pkt, rssi=rssi, snr=snr)

    async def deliver_control_data(
        self,
        snr: float,
        rssi: int,
        path_len: int,
        path_bytes: bytes,
        payload_bytes: bytes,
    ) -> None:
        """Deliver CONTROL payload (e.g. discovery response) to companion clients (PUSH_CODE_CONTROL_DATA 0x8E)."""
        # Only push discovery responses (0x90); client expects these, not the request (0x80)
        if len(payload_bytes) < 6 or (payload_bytes[0] & 0xF0) != 0x90:
            return
        # Push every discovery response to the client, including our own (snr=0, rssi=0 = local node's response)
        servers = getattr(self, "companion_frame_servers", [])
        if not servers:
            return
        tag = int.from_bytes(payload_bytes[2:6], "little") if len(payload_bytes) >= 6 else 0
        logger.debug(
            "Delivering discovery response to %s companion(s): tag=0x%08X, len=%s",
            len(servers),
            tag,
            len(payload_bytes),
        )
        for fs in servers:
            try:
                await fs.push_control_data(snr, rssi, path_len, path_bytes, payload_bytes)
            except Exception as e:
                logger.warning("Companion push_control_data error: %s", e)

    async def _on_trace_complete_for_companions(self, packet, parsed_data) -> None:
        """Trace completed at this node: push PUSH_CODE_TRACE_DATA (0x89) to companion clients (firmware onTraceRecv)."""
        path_hashes = parsed_data.get("trace_path_bytes") or b""
        if not path_hashes:
            return
        flags = parsed_data.get("flags", 0)
        path_sz = flags & 0x03
        hash_len = len(path_hashes)
        expected_snr_len = hash_len >> path_sz
        if expected_snr_len <= 0:
            return
        tag = parsed_data.get("tag", 0)
        auth_code = parsed_data.get("auth_code", 0)
        snr_scaled = max(-128, min(127, int(round(packet.get_snr() * 4))))
        snr_byte = snr_scaled if snr_scaled >= 0 else (256 + snr_scaled)
        # Firmware: memcpy path_snrs from pkt->path (length hash_len >> path_sz), then final SNR byte
        raw = bytes(packet.path)[:expected_snr_len]
        if len(raw) < expected_snr_len:
            raw = raw + b"\x00" * (expected_snr_len - len(raw))
        path_snrs = raw
        for fs in getattr(self, "companion_frame_servers", []):
            try:
                await fs.push_trace_data_async(
                    hash_len, flags, tag, auth_code, path_hashes, path_snrs, snr_byte
                )
            except Exception as e:
                logger.debug("Push trace data to companion: %s", e)

    def _register_identity_everywhere(
        self, name: str, identity, config: dict, identity_type: str
    ) -> bool:
        """
        Stage an identity during initialization, before its helpers exist.
        Hot room additions must await add_room_from_config() instead.
        """
        if any((self.login_helper, self.text_helper, self.protocol_request_helper)):
            raise RuntimeError("Live room activation must await add_room_from_config()")
        # Helpers route local server identities by one-byte hash. Do not replace
        # the primary repeater's handler/ACL with a colliding room identity.
        if (name == "repeater" or (identity_type == "room_server"
                and identity.get_public_key()[0] == self.local_hash)):
            logger.error("Cannot register '%s': conflicts with the repeater identity", name)
            return False

        return self.identity_manager.register_identity(
            name=name, identity=identity, config=config, identity_type=identity_type
        )

    async def _router_callback(self, packet):
        """
        Single entry point for ALL packets.
        Enqueues packets for router processing.
        """
        if self.router:
            try:
                await self.router.enqueue(packet)
            except Exception as e:
                logger.error(f"Error enqueuing packet in router: {e}", exc_info=True)

    def register_text_handler_for_identity(
        self, name: str, identity, identity_type: str = "room_server", radio_config: dict = None
    ):

        if not self.text_helper:
            logger.warning("Text helper not initialized, cannot register identity")
            return False

        try:
            self.text_helper.register_identity(
                name=name,
                identity=identity,
                identity_type=identity_type,
                radio_config=radio_config or self.config.get("radio", {}),
            )
            logger.info(f"Registered text handler for {identity_type} '{name}'")
            return True
        except Exception as e:
            logger.error(f"Failed to register text handler for '{name}': {e}")
            return False

    async def _trace_packet_injector(self, packet, wait_for_ack: bool = False):
        """Route TRACE using packet-local origin, never a shared temporary sender."""
        if getattr(self, "_shutdown_started", False):
            return False
        if not hasattr(packet, "_trace_bridge_origin"):
            if self.router is None:
                return False
            return await self.router.inject_packet(packet, wait_for_ack=wait_for_ack)
        if (self.config.get("repeater", {}).get("mode", "forward") != "forward"
                or self.bridge_engine is None):
            return False
        try:
            data = packet.write_to()
        except Exception as exc:
            logger.warning("TRACE forward serialization failed (%s)", type(exc).__name__)
            return False
        return await self.bridge_engine.inject_packet(
            "repeater", data, origin_channel=packet._trace_bridge_origin,
        )

    async def _response_injector(self, packet, wait_for_ack: bool = False, *, expected_crc=None, ack_timeout_s=None):
        """Responses use the same TX and local-delivery path as companion traffic.

        A co-hosted companion must receive its login/status reply without
        depending on a radio echo, which the bridge correctly suppresses.
        """
        return await self._companion_injector(
            packet, wait_for_ack=wait_for_ack,
            expected_crc=expected_crc, ack_timeout_s=ack_timeout_s,
        )

    async def _companion_injector(self, packet, wait_for_ack: bool = False, *, expected_crc=None, ack_timeout_s=None):
        """WM1303: bridge-aware packet injector for companion-originated TX.

        Used as the `packet_injector` for RepeaterCompanionBridge so that
        messages sent from a TCP companion app are transmitted via the
        BridgeEngine (RF TX on all active channels) instead of the upstream
        PacketRouter/Dispatcher which targets classic radios that are not
        active in a WM1303 setup.

        After bridge injection the packet is also enqueued on the PacketRouter
        so that:
        - The sending companion sees its own message echoed back.
        - Other companion bridges receive a copy.
        - The _injected_for_tx flag prevents the engine from re-processing it.
        """
        if (getattr(self, "_shutdown_started", False)
                or self.config.get("repeater", {}).get("mode") == "no_tx"):
            return False
        if self.dispatcher:
            self.dispatcher._apply_default_path_hash_mode(packet)
        if self.bridge_engine is not None:
            try:
                packet_bytes = packet.write_to()
                sent = await self.bridge_engine.inject_packet('repeater', packet_bytes)
                if sent is False:
                    return False
                ptype = getattr(packet, 'get_payload_type', lambda: None)()
                logger.info(
                    "_companion_injector: bridge TX OK (%d bytes, type=%s)",
                    len(packet_bytes), ptype,
                )
            except Exception as e:
                logger.warning(
                    "_companion_injector: bridge inject failed: %s",
                    e,
                )
                return False

        else:
            # Fallback: classic router (for setups without bridge_engine).
            if self.router is not None:
                try:
                    return await self.router.inject_packet(
                        packet, wait_for_ack=wait_for_ack,
                        expected_crc=expected_crc, ack_timeout_s=ack_timeout_s,
                    )  # router.inject_packet already enqueues
                except Exception as e:
                    logger.error("_companion_injector: router fallback failed: %s", e)
            else:
                logger.error(
                    "_companion_injector: no injector available (bridge_engine and router both None)"
                )
            return False

        # Bridge injection succeeded — also enqueue on the PacketRouter so
        # companion bridges (including the sender) receive a copy.
        if self.router is not None:
            try:
                packet._injected_for_tx = True
                await self.router.enqueue(packet)
            except Exception as e:
                logger.debug(
                    "_companion_injector: router enqueue failed (non-fatal): %s", e
                )
        if wait_for_ack:
            return bool(self.router) and await self.router.wait_for_packet_ack(
                packet, expected_crc, ack_timeout_s
            )
        return True

    async def _bridge_repeater_handler(self, data: bytes,
                                        origin_channel: str | None = None,
                                        rssi: float | None = None,
                                        snr: float | None = None) -> None:
        """Bridge-to-repeater handler: parse raw bytes, run repeater logic, re-inject result.

        Called by BridgeEngine when a rule forwards to 'repeater' endpoint.
        Parses raw bytes into a Packet, runs process_packet() for path/hop
        modification, then re-injects the forwarded packet back into the
        bridge engine as 'repeater' source so downstream rules (repeater -> channels)
        can fire and TX on all configured destination channels.

        Args:
            data: Raw packet bytes from the bridge engine.
            origin_channel: Optional channel_id where the packet was originally
                received (e.g. 'channel_a'). Passed through to inject_packet
                so the TX serializer can prioritize the origin channel.
            rssi: Optional RSSI value (dBm) from the receiving radio.
            snr: Optional SNR value (dB) from the receiving radio.

        NOTE: Do NOT call mark_seen() before process_packet()! The
        flood_forward/direct_forward methods inside process_packet check
        is_duplicate() first and call mark_seen() themselves after the check
        passes.  Calling mark_seen early causes ALL packets to be silently
        dropped as duplicates.
        """
        from openhop_core.protocol.packet import Packet
        from openhop_core.node.handlers.advert import AdvertHandler
        from openhop_core.node.handlers.trace import TraceHandler
        from openhop_core.node.handlers.login_server import LoginServerHandler
        from openhop_core.node.handlers.text import TextMessageHandler
        from openhop_core.node.handlers.protocol_request import ProtocolRequestHandler

        # Parse raw bytes into Packet object
        pkt = Packet()
        if not pkt.read_from(data):
            logger.warning("BridgeRepeaterHandler: failed to parse %d bytes", len(data))
            return

        # Attach RSSI/SNR from bridge metadata to the parsed Packet so
        # downstream handlers (advert processing, neighbor tracking) can
        # access signal quality even though it's not in the wire format.
        if rssi is not None:
            pkt._rssi = int(rssi)
        if snr is not None:
            # The Python core stores SNR in dB (Dispatcher uses the same
            # convention). TRACE handlers encode quarter-dB bytes on the wire.
            pkt._snr = float(snr)

        logger.info(
            "BridgeRepeaterHandler: parsed %d bytes, header=0x%02x, origin_channel=%s, rssi=%s, snr=%s",
            len(data), pkt.header, origin_channel, rssi, snr
        )

        # NOTE: Raw RX push to companion frame servers (0x88 / Heard Repeats)
        # is now handled by the BridgeEngine raw RX callback mechanism.
        # The callback is registered in _setup_bridge_engine and fires BEFORE
        # echo/dedup filtering, so companions receive ALL RF packets including
        # TX echoes (which are "heard repeats" of our own transmissions).

        # Deliver before mutating the forwarding path. Enqueuing the same
        # Packet here raced with process_packet below: a transit DIRECT could
        # look like a final-hop packet by the time the queue consumed it.
        # The router also reports authenticated local consumption, preventing
        # duplicate helper responses and forwarding of messages addressed here.
        payload_type = pkt.get_payload_type()
        locally_routed = False
        if self.router and payload_type != TraceHandler.payload_type():
            try:
                pkt._injected_for_tx = True
                handled = await self.router._route_packet(pkt, origin_channel=origin_channel)
                locally_routed = True
                if handled:
                    return
            except Exception as e:
                logger.warning(
                    "BridgeRepeaterHandler: local delivery failed: %s", e
                )

        transit_direct = (pkt.is_route_direct() and pkt.get_path_hash_count() > 0)
        # Process ADVERT packets for neighbor tracking
        if (not locally_routed and not transit_direct
                and payload_type == AdvertHandler.payload_type() and self.advert_helper):
            try:
                _rssi = int(rssi) if rssi is not None else 0
                _snr = float(snr) if snr is not None else 0.0
                await self.advert_helper.process_advert_packet(pkt, _rssi, _snr, origin_channel or "")
                logger.info("BridgeRepeaterHandler: processed ADVERT for neighbor tracking (rssi=%s, snr=%s, channel=%s)",
                            _rssi, _snr, origin_channel)
            except Exception as e:
                logger.warning("BridgeRepeaterHandler: advert processing error: %s", e)

        # WM1303 v2.4.11: dispatch TRACE packets to TraceHelper for proper
        # response generation. The upstream pymc_repeater packet_router.py
        # routes TRACE via TraceHelper.process_trace_packet(), but our bridge
        # bypasses that router entirely. Without this dispatch, TRACE pings
        # to the repeater are silently forwarded as flood broadcasts and
        # never produce a TRACE response, so companion node pings time out.
        #
        # Keep the origin on this packet. Multiple radio RX tasks share the
        # helper, so replacing its injector across an await can restore another
        # packet's stale sender or route an unrelated TRACE to the wrong channel.
        # The helper's permanent injector selects bridge versus router per packet.
        if payload_type == TraceHandler.payload_type() and self.trace_helper:
            pkt._trace_bridge_origin = origin_channel
            try:
                await self.trace_helper.process_trace_packet(pkt)
                logger.info(
                    "BridgeRepeaterHandler: TRACE dispatched to TraceHelper "
                    "(origin_channel=%s, rssi=%s, snr=%s)",
                    origin_channel, rssi, snr
                )
            except Exception as e:
                logger.warning("BridgeRepeaterHandler: trace processing error: %s", e)
            return
        if payload_type == TraceHandler.payload_type():
            return  # generic forwarding would interpret TRACE SNR bytes as hashes

        # WM1303 v2.4.11: dispatch ANON_REQ / TXT_MSG / PROTOCOL_REQ packets to
        # their respective helpers so remote admin from a companion node works
        # over channel_e/f, just like over classic radios[0]/[1] via the upstream
        # packet_router. Without this dispatch, login/text/protocol-request
        # packets are silently forwarded as floods and the helpers never run, so
        # companion login + CLI commands time out.
        #
        # The helpers are initialised with self._response_injector as their
        # packet_injector (see helper init in _initialise_repeater_helpers),
        # which routes any response through the BridgeEngine. So we just need
        # to call process_*_packet(pkt). If handled=True we return (skip the
        # generic forward). If handled=False we fall through so the packet is
        # still re-broadcast for mesh propagation.

        # ANON_REQ -> LoginHelper (companion login / authentication)
        if (not locally_routed and not transit_direct
                and payload_type == LoginServerHandler.payload_type() and self.login_helper):
            try:
                handled = await self.login_helper.process_login_packet(pkt)
                logger.info(
                    "BridgeRepeaterHandler: ANON_REQ dispatched to LoginHelper "
                    "(handled=%s, origin_channel=%s)",
                    handled, origin_channel
                )
                if handled:
                    return
            except Exception as e:
                logger.warning("BridgeRepeaterHandler: login processing error: %s", e)

        # TXT_MSG -> TextHelper (admin CLI commands like reboot, neighbors, ...)
        if (not locally_routed and not transit_direct
                and payload_type == TextMessageHandler.payload_type() and self.text_helper):
            try:
                handled = await self.text_helper.process_text_packet(pkt)
                logger.info(
                    "BridgeRepeaterHandler: TXT_MSG dispatched to TextHelper "
                    "(handled=%s, origin_channel=%s)",
                    handled, origin_channel
                )
                if handled:
                    return
            except Exception as e:
                logger.warning("BridgeRepeaterHandler: text processing error: %s", e)

        # PROTOCOL_REQ -> ProtocolRequestHelper (status queries, etc.)
        if (not locally_routed and not transit_direct
                and payload_type == ProtocolRequestHandler.payload_type()
                and self.protocol_request_helper):
            try:
                handled = await self.protocol_request_helper.process_request_packet(pkt)
                logger.info(
                    "BridgeRepeaterHandler: PROTOCOL_REQ dispatched to "
                    "ProtocolRequestHelper (handled=%s, origin_channel=%s)",
                    handled, origin_channel
                )
                if handled:
                    return
            except Exception as e:
                logger.warning(
                    "BridgeRepeaterHandler: protocol_request processing error: %s", e
                )

        # Run repeater forwarding logic
        result = self.repeater_handler.process_packet(pkt)
        if result is None:
            drop_reason = getattr(pkt, 'drop_reason', 'unknown')
            logger.info(
                "BridgeRepeaterHandler: process_packet returned None "
                "(drop_reason=%s)", drop_reason
            )
            return

        fwd_pkt, delay = result
        # ACK redundancy packets share the primary deadline. Serialize bridge
        # injections so the wrapper is queued before the plain ACK, with each
        # delay measured from the original forwarding decision.
        forward_start = time.monotonic()
        for extra_packet, extra_delay in getattr(result, "extras", ()):
            try:
                remaining = max(0.0, extra_delay - (time.monotonic() - forward_start))
                if remaining:
                    await asyncio.sleep(remaining)
                if self.bridge_engine:
                    await self.bridge_engine.inject_packet(
                        'repeater', extra_packet.write_to(), origin_channel=origin_channel
                    )
            except Exception as e:
                logger.warning("BridgeRepeaterHandler: extra ACK transmission failed: %s", e)

        fwd_bytes = fwd_pkt.write_to()

        logger.info(
            "BridgeRepeaterHandler: forwarding %d -> %d bytes (delay=%.3fs, origin_channel=%s)",
            len(data), len(fwd_bytes), delay, origin_channel
        )

        # Re-inject into bridge engine as 'repeater' source
        if self.bridge_engine:
            # Honor the repeater's collision-avoidance delay as well as ACK
            # spacing. Hardware CAD still runs independently before RF TX.
            remaining = max(0.0, delay - (time.monotonic() - forward_start))
            if remaining:
                await asyncio.sleep(remaining)
            await self.bridge_engine.inject_packet('repeater', fwd_bytes,
                                                   origin_channel=origin_channel)

    def _init_wm1303_bridge(self):
        """Initialize the WM1303 bridge engine with dual-channel radios."""
        from openhop_core.hardware import WM1303Backend

        if not isinstance(WM1303Backend, type) or not isinstance(self.radio, WM1303Backend):
            return
        backend = self.radio
        cfg_bridge = self.config.get("bridge", {})

        # Load rules from SSOT (wm1303_ui.json)
        rules = self._load_bridge_rules_from_ui()
        if rules is None:
            rules = cfg_bridge.get("bridge_rules", [])
            if rules:
                logger.info("No SSOT bridge rules -- falling back to config.yaml bridge_rules")
        if not rules:
            logger.info("No bridge rules configured -- starting receive-only bridge")

        radios = backend.get_radios()
        if len(radios) < 1:
            # Channel E / F operate independently of channels A-D.
            # If at least one of them is enabled we must still start the
            # BridgeEngine so their RX callbacks get registered.
            active_ui = backend._read_active_ui()
            _has_ef = any(active_ui.get(name, {}).get('enabled', False)
                          for name in ('channel_e', 'channel_f'))
            if not _has_ef:
                logger.warning(f"Need >= 1 radio for bridge, got {len(radios)}")
                return
            logger.info(
                'WM1303: no A-D channels active but channel_e/f enabled '
                '— proceeding with bridge init (radios=%d)', len(radios))

        dedup_ttl = cfg_bridge.get("dedup_ttl_seconds",
                                    cfg_bridge.get("dedup_ttl", 300.0))

        from repeater.bridge_engine import BridgeEngine
        self.bridge_engine = BridgeEngine(
            radios=radios,
            rules=rules,
            dedup_ttl=dedup_ttl,
        )

        # Register repeater handler for bridge->repeater forwarding
        if self.repeater_handler:
            if hasattr(self.repeater_handler, "process_packet"):
                self.bridge_engine.set_repeater_handler(
                    self._bridge_repeater_handler
                )
                logger.info("WM1303: repeater handler registered with bridge engine")
                self.bridge_engine.set_repeater_engine(self.repeater_handler)
                logger.info("WM1303: repeater engine reference set with bridge engine")
            else:
                logger.warning(
                    "WM1303: repeater_handler exists but has no process_packet method! "
                    "Available methods: %s",
                    [m for m in dir(self.repeater_handler) if not m.startswith('_')]
                )
        else:
            logger.warning(
                "WM1303: repeater_handler is None at bridge init time! "
                "Will attempt re-registration after startup."
            )

        # Register raw RX callback so companion frame servers receive ALL RF
        # packets (including TX echoes and duplicates) BEFORE echo/dedup
        # filtering.  This enables Heard Repeats and node discovery in
        # companion apps.  The callback is invoked from BridgeEngine._rx_loop
        # and inject_packet before _is_tx_echo / _is_duplicate checks.
        self.bridge_engine.register_on_raw_rx(self._on_raw_rx_for_companions)
        logger.info("WM1303: raw RX callback registered with bridge engine for companion delivery")

        logger.info(
            f"BridgeEngine initialized: {len(radios)} radios, {len(rules)} rules, "
            f"dedup_ttl={dedup_ttl}s"
        )

        # Connect SQLite handler for dedup event persistence
        _sh = None
        if self.repeater_handler and self.repeater_handler.storage:
            _sh = self.repeater_handler.storage.sqlite_handler
        if _sh:
            self.bridge_engine.set_sqlite_handler(_sh)
            logger.info("WM1303: SQLite handler connected to BridgeEngine for dedup persistence")
        else:
            logger.warning("WM1303: No SQLite handler available for BridgeEngine dedup persistence")

    @staticmethod
    def _load_bridge_rules_from_ui() -> list | None:
        """Load bridge rules from SSOT: /etc/openhop_repeater/wm1303_ui.json (or legacy /etc/pymc_repeater/wm1303_ui.json)."""
        import json
        ui_path = str(resolve_config_path('wm1303_ui.json'))
        try:
            with open(ui_path) as f:
                ui = json.load(f)
            raw_rules = ui.get("bridge", {}).get("rules", [])
            if not isinstance(raw_rules, list) or any(not isinstance(r, dict) for r in raw_rules):
                raise ValueError("Bridge rules must be a list of objects")
            rules = []
            for r in raw_rules:
                rule = dict(r)
                if "from" in rule:
                    rule["source"] = rule.pop("from")
                if "to" in rule:
                    rule["target"] = rule.pop("to")
                rules.append(rule)
            logger.info(f"SSOT: loaded {len(rules)} bridge rules from {ui_path}")
            return rules
        except FileNotFoundError:
            logger.warning(f"SSOT file not found: {ui_path}")
            return None
        except Exception as e:
            logger.error(f"Failed to load bridge rules from SSOT: {e}")
            raise ValueError("Cannot load bridge rules from Manager configuration") from e

    def reload_bridge_rules(self) -> bool:
        """Hot-reload bridge rules from SSOT without restarting."""
        if not self.bridge_engine:
            logger.warning("Cannot reload bridge rules: bridge_engine is None")
            return False
        try:
            rules = self._load_bridge_rules_from_ui()
            if rules is None:
                return False  # keep working rules if the file cannot be loaded
            main_task = getattr(self, "_main_task", None)
            loop = main_task.get_loop() if main_task else None
            try:
                caller_loop = asyncio.get_running_loop()
            except RuntimeError:
                caller_loop = None
            if loop and loop.is_running() and caller_loop is not loop:
                # HTTP runs on a worker thread. Apply the rule/alias snapshot
                # between RX tasks, not concurrently with a forwarding decision.
                applied = Future()

                def apply_rules():
                    if not applied.set_running_or_notify_cancel():
                        return
                    try:
                        self.bridge_engine.update_rules(rules)
                        applied.set_result(True)
                    except Exception as exc:
                        applied.set_exception(exc)

                loop.call_soon_threadsafe(apply_rules)
                try:
                    applied.result(timeout=3)
                except TimeoutError:
                    applied.cancel()
                    raise
            else:
                self.bridge_engine.update_rules(rules)
            logger.info(f"Bridge rules hot-reloaded: {len(rules)} rules")
            return True
        except Exception as e:
            logger.error(f"Failed to hot-reload bridge rules: {e}")
            return False

    def get_stats(self) -> dict:
        stats = {}

        if self.repeater_handler:
            stats = self.repeater_handler.get_stats()
            storage = getattr(self.repeater_handler, "storage", None)
            if storage and hasattr(storage, "get_storage_stats"):
                stats["storage_writer"] = storage.get_storage_stats()
            # Add public key if available
            if self.local_identity:
                try:
                    pubkey = self.local_identity.get_public_key()
                    stats["public_key"] = pubkey.hex()
                except Exception:
                    stats["public_key"] = None


        # Add WM1303 bridge stats
        if getattr(self, "bridge_engine", None):
            try:
                bstats = self.bridge_engine.get_stats()
                stats["bridge"] = bstats
                stats["bridge_forwarded"] = bstats.get("forwarded_packets", 0)
                stats["bridge_dropped_duplicate"] = bstats.get("dropped_duplicate", 0)
            except Exception as e:
                logger.debug(f"Failed to get bridge stats: {e}")

        if self.gps_service:
            stats["gps"] = self.gps_service.get_summary()


        # Sanitize the full stats dict at the exit point so no bytes field from
        # any source (repeater handler, bridge, gps, etc.) can break /api/stats.
        return json_safe(stats)

    async def _get_companion_stats(self, stats_type: int) -> dict:
        """Return stats dict for companion CMD_GET_STATS (format expected by frame_server + meshcore_py)."""
        from repeater.companion.constants import (
            STATS_TYPE_CORE,
            STATS_TYPE_PACKETS,
            STATS_TYPE_RADIO,
        )

        if not self.repeater_handler:
            return {}
        engine = self.repeater_handler
        airtime = engine.airtime_mgr.get_stats()
        uptime_secs = int(time.time() - engine.start_time)
        queue_len = 0
        for bridge in getattr(self, "companion_bridges", {}).values():
            queue_len += getattr(getattr(bridge, "message_queue", None), "count", 0) or 0
        if stats_type == STATS_TYPE_CORE:
            return {
                "battery_mv": 0,
                "uptime_secs": uptime_secs,
                "errors": 0,
                "queue_len": min(255, queue_len),
            }
        if stats_type == STATS_TYPE_RADIO:
            noise_floor = int(engine.get_cached_noise_floor() or 0)
            radio = getattr(self, "dispatcher", None) and getattr(self.dispatcher, "radio", None)
            if radio:
                _r = getattr(radio, "get_last_rssi", lambda: 0)
                _s = getattr(radio, "get_last_snr", lambda: 0.0)
                last_rssi = _r() if callable(_r) else _r
                last_snr = _s() if callable(_s) else _s
            else:
                last_rssi, last_snr = 0, 0.0
            tx_air_secs = int(airtime.get("total_airtime_ms", 0) / 1000)
            return {
                "noise_floor": noise_floor,
                "last_rssi": int(last_rssi) if last_rssi is not None else 0,
                "last_snr": float(last_snr) if last_snr is not None else 0.0,
                "tx_air_secs": tx_air_secs,
                "rx_air_secs": 0,
            }
        if stats_type == STATS_TYPE_PACKETS:
            return {
                "recv": getattr(engine, "rx_count", 0),
                "sent": getattr(engine, "forwarded_count", 0),
                "flood_tx": getattr(engine, "forwarded_count", 0),
                "direct_tx": 0,
                "flood_rx": getattr(engine, "rx_count", 0),
                "direct_rx": 0,
                "recv_errors": getattr(engine, "dropped_count", 0),
            }
        return {}

    async def send_advert(self) -> bool:

        if not self.dispatcher or not self.local_identity:
            logger.error("Cannot send advert: dispatcher or identity not initialized")
            return False

        mode = self.config.get("repeater", {}).get("mode", "forward")
        if mode == "no_tx":
            logger.debug("Adverts disabled in no_tx mode")
            return False

        try:
            from openhop_core.protocol import PacketBuilder
            from openhop_core.protocol.constants import ADVERT_FLAG_HAS_NAME, ADVERT_FLAG_IS_REPEATER

            # Get node name and location from config
            repeater_config = self.config.get("repeater", {})
            node_name = repeater_config.get("node_name", "Repeater")
            latitude = repeater_config.get("latitude", 0.0)
            longitude = repeater_config.get("longitude", 0.0)
            location_source = "config"

            if self.gps_service:
                location = self.gps_service.get_repeater_location()
                latitude = location.get("latitude", latitude)
                longitude = location.get("longitude", longitude)
                location_source = str(location.get("source", location_source))


            flags = ADVERT_FLAG_IS_REPEATER | ADVERT_FLAG_HAS_NAME

            packet = PacketBuilder.create_advert(
                local_identity=self.local_identity,
                name=node_name,
                lat=latitude,
                lon=longitude,
                feature1=0,
                feature2=0,
                flags=flags,
                route_type="flood",
            )

            if not await self._response_injector(packet):
                logger.warning("Advert was not transmitted")
                return False

            # The shared injector queues this advert for local companions once.

            # Mark our own advert as seen to prevent re-forwarding it
            if self.repeater_handler:
                self.repeater_handler.mark_seen(packet)
                logger.debug("Marked own advert as seen in duplicate cache")

            logger.info(f"Sent flood advert '{node_name}' at ({latitude: .6f}, {longitude: .6f})")
            return True

        except Exception as e:
            logger.error(f"Failed to send advert: {e}", exc_info=True)
            return False

    def _signal_shutdown(self, sig, loop):
        """Handle SIGTERM/SIGINT by setting the stop event for cooperative exit."""
        if self._shutdown_started:
            logger.info(f"Received signal {sig.name}, shutdown already in progress")
            return
        logger.info(f"Received signal {sig.name}, shutting down...")
        self._stop_event.set()

    def _update_repeater_location_from_gps(self, location: dict) -> bool:
        """Persist the latest valid GPS fix as the repeater's advertised location."""
        latitude = location.get("latitude")
        longitude = location.get("longitude")
        if latitude is None or longitude is None:
            return False

        repeater_config = self.config.setdefault("repeater", {})
        current_latitude = repeater_config.get("latitude")
        current_longitude = repeater_config.get("longitude")
        try:
            if (
                current_latitude is not None
                and current_longitude is not None
                and abs(float(current_latitude) - float(latitude)) < 0.000001
                and abs(float(current_longitude) - float(longitude)) < 0.000001
            ):
                return False
        except (TypeError, ValueError):
            pass

        updates = {
            "repeater": {
                "latitude": float(latitude),
                "longitude": float(longitude),
            }
        }
        if self.config_manager:
            result = self.config_manager.update_and_save(
                updates=updates,
                live_update=True,
                live_update_sections=["repeater"],
            )
            if not result.get("success"):
                logger.warning(
                    "GPS location fix could not update repeater config: %s",
                    result.get("error", "unknown error"),
                )
                return False
        else:
            repeater_config.update(updates["repeater"])

        logger.info(
            "Updated repeater location from GPS fix: latitude=%.6f longitude=%.6f",
            latitude,
            longitude,
        )
        return True

    async def _shutdown(self):
        """Start cleanup once; every caller joins the same owned work."""
        if self._shutdown_task is None:
            self._shutdown_started = True
            self._shutdown_task = asyncio.create_task(
                self._finish_shutdown(), name="repeater shutdown",
            )
        await asyncio.shield(self._shutdown_task)

    async def _join_shutdown(self):
        """Keep run() alive through caller cancellation until cleanup settles."""
        cancellation = None
        while True:
            try:
                await self._shutdown()
            except asyncio.CancelledError as exc:
                task = self._shutdown_task
                if task is None:
                    raise
                if task.cancelled():
                    # This is cancellation of the actual cleanup worker, not
                    # its waiter. It is terminal and must not look successful.
                    raise RuntimeError("Repeater shutdown was cancelled before completion") from exc
                cancellation = exc
                if not task.done():
                    continue
                # Cancellation can race a completed cleanup failure. Retrieve
                # that result rather than masking it with caller cancellation.
                task.result()
            break
        if cancellation is not None:
            raise cancellation

    async def _finish_shutdown(self):
        """Stop background services and release hardware in dependency order."""
        cleanup_error = None

        # Room pushes own ACK waits and cursor updates. Stop them before a
        # potentially long HTTP drain, while SQLite and the radio still exist;
        # rejected sends during shutdown must not count as client failures.
        for room in getattr(self.text_helper, "room_servers", {}).values():
            try:
                await room.stop()
            except Exception as e:
                logger.warning("Error stopping room server: %s", e)
                if cleanup_error is None:
                    cleanup_error = e

        # Finish in-flight Glass commands while radio/storage are still usable.
        # Its MQTT publisher remains available until accepted storage work drains.
        if self.glass_handler:
            try:
                await self.glass_handler.stop_informing()
            except Exception as e:
                logger.warning("Error stopping Glass inform loop: %s", e)

        # Close HTTP admission and drain handlers/streams before their radio and
        # storage dependencies. The loop stays available for request futures;
        # timing out to_thread() would leave its stop worker running unowned.
        if self.http_server:
            try:
                await asyncio.to_thread(self.http_server.stop)
            except Exception as e:
                logger.warning(f"Error stopping HTTP server: {e}")
                if cleanup_error is None:
                    cleanup_error = e
            finally:
                # stop() ran in a worker; this connection belongs to our loop.
                sqlite_handler = getattr(self.http_server, "sqlite_handler", None)
                if sqlite_handler is not None:
                    try:
                        sqlite_handler.close_thread_connection()
                    except Exception as e:
                        logger.warning("Error closing HTTP authentication connection: %s", e)
                        if cleanup_error is None:
                            cleanup_error = e

        # A room's HTTP waiter may have timed out while the owned activation
        # is still starting or rolling back. Drain it before its dependencies.
        await self._drain_room_activations()

        # A cancelled HTTP Future returns before its companion-start coroutine
        # finishes rollback. Join that activation while its dependencies remain.
        activation_lock = getattr(self, "_companion_activation_lock", None)
        if activation_lock is not None:
            async with activation_lock:
                pass

        # HTTP's synchronous wait can time out before an initial login send
        # finishes. Close all login admission before joining that finite work;
        # late scheduled HTTP coroutines then fail without touching the radio.
        for bridge in getattr(self, "companion_bridges", {}).values():
            bridge.stop_login_admission()
        for bridge in getattr(self, "companion_bridges", {}).values():
            await bridge.drain_login_starts()

        # Calibration owns awaited radio restoration after cancellation. Close
        # admission and finish that work before releasing any radio resources.
        calibration = getattr(getattr(getattr(self.http_server, "app", None), "api", None), "cad_calibration", None)
        if calibration is not None:
            await calibration.close()

        # Stop every companion's admission before waiting for any one client.
        # Commands finish while their radio/identity/storage dependencies exist;
        # final contact/channel snapshots are saved later, after RX has drained.
        for frame_server in getattr(self, "companion_frame_servers", ()):
            frame_server.stop_admission()
        for frame_server in getattr(self, "companion_frame_servers", ()):
            await frame_server.stop_clients()

        if self.dispatcher:
            try:
                dispatcher_task = getattr(self, "_dispatcher_task", None)
                if dispatcher_task is None:
                    await self.dispatcher.stop()
                else:
                    # Drain the real maintenance task, including any threaded
                    # health check, before releasing the radio. Its start can
                    # fail before setting the upstream stopped event, so that
                    # event alone is not a reliable completion signal.
                    # Let a just-scheduled run_forever finish its synchronous
                    # prelude before signaling; that prelude clears stop state.
                    await asyncio.sleep(0)
                    self.dispatcher.cleanup()
                    results = await asyncio.gather(dispatcher_task, return_exceptions=True)
                    if isinstance(results[0], Exception):
                        logger.warning("Dispatcher exited with an error: %s", results[0])
            except Exception as e:
                logger.warning("Error stopping dispatcher: %s", e)

        for channel_name in ("_channel_e", "_channel_f"):
            channel = getattr(self, channel_name, None)
            if channel:
                try:
                    channel.stop()
                except Exception as e:
                    logger.warning("Error stopping %s: %s", channel_name, e)

        # Stop radio-owned RX/metrics producers before closing bridge admission
        # or draining storage. WM1303 owns processes, sockets and DB-writing
        # threads through stop(); older radios expose cleanup().
        if self.radio:
            try:
                if callable(getattr(self.radio, "cleanup", None)):
                    self.radio.cleanup()
                elif callable(getattr(self.radio, "stop", None)):
                    await asyncio.to_thread(self.radio.stop)
            except Exception as e:
                logger.warning(f"Error cleaning up radio: {e}")

        # Stop WM1303 bridge engine
        if getattr(self, "bridge_engine", None):
            try:
                self.bridge_engine.stop()
                logger.info("Bridge engine stopped")
            except Exception as e:
                logger.warning(f"Error stopping bridge engine: {e}")

        # Stop the remaining async producers before closing storage.
        # Router RX may still finish after this task snapshot. Disarm room
        # ACK timers first so a late accepted post cannot escape the drain.
        if self.text_helper is not None:
            self.text_helper._room_ack_admission_closed = True
        tasks = set(getattr(self, "_service_tasks", []))
        timer = getattr(self.repeater_handler, "_background_task", None)
        if timer:
            tasks.add(timer)
        for helper in (self.login_helper, self.text_helper, self.discovery_helper):
            tasks.update(getattr(helper, "_pending_tasks", ()))
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        # A bridge can own its writer even if startup failed before run().
        # Keep the loop available while accepted dedup batches finish.
        if getattr(self, "bridge_engine", None):
            try:
                await self.bridge_engine.wait_closed()
            except Exception as e:
                logger.warning("Error draining bridge persistence: %s", e)

        retention = getattr(self, "_metrics_retention", None)
        if retention:
            try:
                # A SQLite operation may outlast its busy timeout. Keep owning
                # the join until the worker exits; timing out to_thread() would
                # leave maintenance running while storage is being closed.
                await asyncio.to_thread(retention.stop)
            except Exception as e:
                logger.warning("Error stopping metrics retention: %s", e)

        # Stop GPS diagnostics.
        if self.gps_service:
            try:
                # GPS can still be reading its source or saving a location.
                # Its default timed join reports stopped even if work remains.
                await asyncio.to_thread(self.gps_service.stop, timeout=None)
            except Exception as e:
                logger.warning(f"Error stopping GPS diagnostics: {e}")



        # Stop router admission and its in-flight delivery before draining
        # companion callbacks or taking final contact/channel snapshots.
        if self.router:
            try:
                await self.router.stop()
            except Exception as e:
                logger.warning(f"Error stopping router: {e}")

        # Drain companion-owned RX, response waiters and persistence callbacks.
        if hasattr(self, "companion_bridges"):
            for bridge in self.companion_bridges.values():
                if hasattr(bridge, "stop"):
                    try:
                        await bridge.stop()
                    except Exception as e:
                        logger.warning(f"Companion bridge stop error: {e}")

        # No client/RX callback can now race these final snapshots.
        for frame_server in getattr(self, "companion_frame_servers", []):
            try:
                await frame_server.stop()
            except Exception as e:
                logger.warning(f"Companion frame server stop error: {e}")

        # Drain accepted records before disconnecting publishers. A timeout on
        # to_thread would not stop its worker; it would merely let shutdown race
        # ahead and drop queued publications.
        storage = None
        try:
            if self.repeater_handler and self.repeater_handler.storage:
                storage = self.repeater_handler.storage
                await asyncio.to_thread(storage.close)
        except Exception as e:
            logger.warning(f"Error closing storage: {e}")
            if cleanup_error is None:
                cleanup_error = e
        finally:
            # The worker cannot close this loop's thread-local connection.
            # Attempt it even when worker cleanup failed, retaining the first
            # error while the remaining independent cleanup still runs.
            sqlite_handler = getattr(storage, "sqlite_handler", None)
            if sqlite_handler and hasattr(sqlite_handler, "close_thread_connection"):
                try:
                    sqlite_handler.close_thread_connection()
                except Exception as e:
                    logger.warning("Error closing loop storage connection: %s", e)
                    if cleanup_error is None:
                        cleanup_error = e

        # Glass remains available to the storage writer until it is drained.
        if self.glass_handler:
            try:
                await self.glass_handler.stop()
            except Exception as e:
                logger.warning(f"Error stopping Glass handler: {e}")

        # Release CH341 USB device if in use
        try:
            if self.config.get("radio_type", "sx1262").lower() == "sx1262_ch341":
                from openhop_core.hardware.ch341.ch341_async import CH341Async

                CH341Async.reset_instance()
        except Exception as e:
            logger.debug(f"CH341 reset skipped/failed: {e}")

        # Do not force-stop the event loop here; asyncio.run() owns loop lifecycle.
        if cleanup_error is not None:
            raise RuntimeError("Repeater shutdown incomplete") from cleanup_error

    @staticmethod
    def _detect_container() -> bool:
        """Detect if running inside an LXC/Docker/systemd-nspawn container."""
        try:
            with open("/proc/1/environ", "rb") as f:
                if b"container=" in f.read():
                    return True
        except (OSError, PermissionError):
            pass
        return os.path.exists("/run/host/container-manager")

    async def run(self):

        logger.info("Repeater daemon started")
        self._main_task = asyncio.current_task()

        # Shutdown event — set by signal handler to unblock the main wait
        self._stop_event = asyncio.Event()

        # Register signal handlers for graceful shutdown
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(
                sig,
                functools.partial(self._signal_shutdown, sig, loop),
            )

        # Warn if running inside a container (udev rules won't work here)
        if os.path.exists("/.dockerenv") or os.environ.get("container") or self._detect_container():
            logger.warning(
                "Container environment detected. "
                "USB device udev rules must be configured on the HOST, not inside this container."
            )

        run_error = None
        try:
            await self.initialize()

            # --- WM1303 Bridge Engine ---
            # Observe room failures too, without moving their cleanup ownership
            # into _service_tasks: RoomServer.stop() drains its own sync task.
            required_tasks = list(
                room._sync_task for room in self.text_helper.room_servers.values()
            )
            # This single event-driven supervisor also covers rooms activated
            # after the fixed asyncio.wait snapshot has already begun.
            room_health = asyncio.create_task(
                self._supervise_room_sync(), name="room server health",
            )
            self._service_tasks.append(room_health)
            required_tasks.append(room_health)

            # --- WM1303 TX Queue Scheduler (CRITICAL for TX) ---
            from openhop_core import hardware
            _WM1303BE = getattr(hardware, 'WM1303Backend', None)
            active_ui = {}
            if isinstance(_WM1303BE, type) and isinstance(self.radio, _WM1303BE):
                self._init_wm1303_bridge()
                active_ui = self.radio._read_active_ui()
                if not self.radio._idle_mode and self.bridge_engine is None:
                    raise RuntimeError("Active WM1303 channels have no bridge")
                _loop = asyncio.get_running_loop()
                for _vr in self.radio.virtual_radios.values():
                    _vr._loop = _loop
                await self.radio.ensure_tx_queues_started()
                manager = self.radio._tx_queue_manager
                if manager and manager.queues:
                    scheduler = self.radio._global_tx_scheduler
                    if not scheduler or not scheduler._running or scheduler._task is None:
                        raise RuntimeError("WM1303 TX queues have no running scheduler")
                    required_tasks.append(scheduler._task)

            # --- Start BridgeEngine RX loops (CRITICAL for cross-channel forwarding) ---
            if self.bridge_engine:
                task = asyncio.create_task(self.bridge_engine.run(), name="WM1303 bridge")
                self._service_tasks.append(task)
                required_tasks.append(task)

                # --- Channel E (native LoRa) ---
                try:
                    from repeater.channel_e_bridge import ChannelEBridge
                    if active_ui.get('channel_e', {}).get('enabled', False):
                        self._channel_e = ChannelEBridge(self.bridge_engine, backend=self.radio)
                        task = asyncio.create_task(self._channel_e.run(), name="WM1303 channel E")
                        self._service_tasks.append(task)
                        required_tasks.append(task)
                    # v2.4.7+: schedule background retry to wrap channel_e RX
                    # callback. ChannelEBridge installs its handler inside its
                    # async run() task, which may not have completed yet; retry
                    # for up to ~20 s until the callback appears.
                    if _UNIFORM_TRACER_AVAILABLE and _uniform_tracer_retry_ch_e is not None:
                        try:
                            _uniform_tracer_retry_ch_e(self.radio)
                        except Exception as _we:
                            logger.debug(f"Uniform tracer channel_e retry failed: {_we}")

                    # v2.4.7+: re-register the friendly channel-name map now
                    # that ChannelEBridge has populated channel_e in
                    # _ch_id_to_ui_name. Without this second call, bridge_engine
                    # traces (which emit `channel='channel_e'`) would not be
                    # rewritten to 'EU-Narrow' because the initial registration
                    # ran before channel_e was added.
                    try:
                        from repeater.web.packet_trace import set_channel_name_map
                        _ch_map2 = getattr(self.radio, '_ch_id_to_ui_name', {}) or {}
                        # Ensure channel_e is present even if the radio hasn't
                        # registered it yet — use the UI config fallback.
                        if 'channel_e' not in _ch_map2:
                            try:
                                _che = active_ui.get('channel_e', {}) or {}
                                _friendly = _che.get('friendly_name') or _che.get('name')
                                if _friendly:
                                    _ch_map2 = dict(_ch_map2)
                                    _ch_map2['channel_e'] = _friendly
                            except Exception:
                                pass
                        if _ch_map2:
                            set_channel_name_map(_ch_map2)
                    except Exception as _cn_err:
                        logger.debug(f"Channel-name map re-registration failed: {_cn_err}")
                except Exception as e:
                    raise RuntimeError("Channel E initialization failed") from e

                # --- Channel F (chan_Lora_std on SX1302 RF0) ---
                # Channel F shares the HAL pkt_fwd UDP port with channels A-D
                # (no separate UDP listener), so the bridge plugin only registers
                # a TX handler + RX callback. The backend dispatches matching
                # chan_Lora_std packets via _channel_f_rx_callback.
                try:
                    from repeater.channel_f_bridge import ChannelFBridge
                    if active_ui.get('channel_f', {}).get('enabled', False):
                        self._channel_f = ChannelFBridge(self.bridge_engine, backend=self.radio)
                        task = asyncio.create_task(self._channel_f.run(), name="WM1303 channel F")
                        self._service_tasks.append(task)
                        required_tasks.append(task)
                    # Re-register friendly channel-name map so 'channel_f' resolves
                    # to its UI-configured friendly_name in trace events.
                    try:
                        from repeater.web.packet_trace import set_channel_name_map
                        _ch_map3 = getattr(self.radio, '_ch_id_to_ui_name', {}) or {}
                        if 'channel_f' not in _ch_map3:
                            try:
                                _chf = active_ui.get('channel_f', {}) or {}
                                _friendly = _chf.get('friendly_name') or _chf.get('name')
                                if _friendly:
                                    _ch_map3 = dict(_ch_map3)
                                    _ch_map3['channel_f'] = _friendly
                            except Exception:
                                pass
                        if _ch_map3:
                            set_channel_name_map(_ch_map3)
                    except Exception as _cnf_err:
                        logger.debug(f"Channel F name map registration failed: {_cnf_err}")
                except Exception as e:
                    raise RuntimeError("Channel F initialization failed") from e

            # Safety-net: re-register repeater handler if not yet registered
            if (self.bridge_engine and self.repeater_handler
                    and not getattr(self.bridge_engine, '_repeater_handler', None)):
                if hasattr(self.repeater_handler, "process_packet"):
                    self.bridge_engine.set_repeater_handler(
                        self._bridge_repeater_handler
                    )
                    logger.info("WM1303: repeater handler registered (safety-net, post-startup)")
                    self.bridge_engine.set_repeater_engine(self.repeater_handler)
                    logger.info("WM1303: repeater engine reference set (safety-net, post-startup)")
                else:
                    logger.warning("WM1303: safety-net registration failed - no process_packet")

            # --- FIX: Unregister Dispatcher direct RX callback ---
            # Bridge engine handles ALL RX via VirtualLoRaRadio queues.
            if self.bridge_engine and hasattr(self.radio, "set_rx_callback"):
                self.radio.set_rx_callback(None)
                logger.info("Unregistered Dispatcher direct RX callback (bridge engine handles RX)")
            elif self.dispatcher:
                task = asyncio.create_task(self.dispatcher.run_forever(), name="packet dispatcher")
                self._dispatcher_task = task
                self._service_tasks.append(task)
                required_tasks.append(task)

            # Let listener setup run before announcing readiness. A failed UDP
            # bind or RX task must not leave a seemingly healthy, deaf daemon.
            await asyncio.sleep(0)
            for task in required_tasks:
                if task.done():
                    if task.cancelled():
                        raise RuntimeError(f"{task.get_name()} was cancelled during startup")
                    await task
                    raise RuntimeError(f"{task.get_name()} stopped during startup")

            # Start HTTP stats server
            http_config = self.config.get("http") or self.config.get("web", {})
            http_port = http_config.get("port", 8000)
            http_host = http_config.get("host", "0.0.0.0")

            node_name = self.config.get("repeater", {}).get("node_name", "Repeater")

            # Format public key for display
            pub_key_formatted = ""
            if self.local_identity:
                pub_key_hex = self.local_identity.get_public_key().hex()
                # Format as <first8...last8>
                if len(pub_key_hex) >= 16:
                    pub_key_formatted = f"{pub_key_hex[:8]}...{pub_key_hex[-8:]}"
                else:
                    pub_key_formatted = pub_key_hex

            current_loop = asyncio.get_event_loop()

            self.http_server = HTTPStatsServer(
                host=http_host,
                port=http_port,
                stats_getter=self.get_stats,
                node_name=node_name,
                pub_key=pub_key_formatted,
                send_advert_func=self.send_advert,
                config=self.config,
                event_loop=current_loop,
                daemon_instance=self,
                config_path=getattr(self, "config_path", str(resolve_config_path('config.yaml'))),
            )

            # HTTP is required for management. Let startup failure reach the
            # owned-resource shutdown below; never signal READY without it.
            self.http_server.start()

            # Centralized metrics retention (8-day default, hourly cleanup, weekly VACUUM)
            try:
                from repeater.metrics_retention import start as _start_retention
                self._metrics_retention = _start_retention(self.config)
            except Exception as _e:
                logger.warning(f"metrics_retention start failed: {_e}")

            # HTTP startup is synchronous. Let queued worker completions run
            # before READY, then retain the same tasks in the runtime health wait.
            await asyncio.sleep(0)
            for task in required_tasks:
                if task.done():
                    if task.cancelled():
                        raise RuntimeError(f"{task.get_name()} was cancelled during startup")
                    await task
                    raise RuntimeError(f"{task.get_name()} stopped during startup")

            # -----------------------------------------------------------------
            # Notify systemd we are ready (Type=notify + TimeoutStartSec=120s).
            # Without this, systemd never sees READY=1, so it marks the unit
            # 'Failed with result timeout' after 120s and restarts the daemon
            # in an endless slow loop even though HTTP/DB/RX are fully up.
            # The optional Python bindings are not required on WM1303: reuse
            # the backend's dependency-free UNIX socket notifier as fallback.
            # -----------------------------------------------------------------
            try:
                try:
                    from systemd.daemon import notify as _sd_notify  # type: ignore[import-not-found]
                except ImportError:
                    from openhop_core.hardware.wm1303_backend import _sd_notify
                if _sd_notify("READY=1"):
                    logger.info("Sent sd_notify READY=1 (Type=notify readiness signalled)")
            except Exception as _sd_err:
                logger.warning(
                    "sd_notify(READY=1) skipped (systemd bindings unavailable?): %s",
                    _sd_err,
                )

            # Keep the daemon alive until a shutdown signal is received.
            # The dispatcher RX/TX processing happens via callbacks and
            # background tasks; we just need to block here until SIGTERM.
            logger.info("Repeater daemon running (waiting for shutdown signal)")
            stop_task = asyncio.create_task(self._stop_event.wait(), name="repeater stop wait")
            self._service_tasks.append(stop_task)
            try:
                done, _ = await asyncio.wait(
                    [stop_task, *required_tasks], return_when=asyncio.FIRST_COMPLETED)
                if stop_task not in done:
                    for task in required_tasks:
                        if task in done:
                            if task.cancelled():
                                raise RuntimeError(f"{task.get_name()} was cancelled unexpectedly")
                            await task
                            raise RuntimeError(f"{task.get_name()} stopped unexpectedly")
            finally:
                stop_task.cancel()
                # The owned shutdown worker drains this harmless event waiter.
                # An await here could replace a required-worker failure with
                # caller cancellation before the outer handler records it.
            logger.info("Shutdown signal received, cleaning up...")

        except BaseException as exc:
            run_error = exc
            raise
        finally:
            try:
                # Exiting run() early would let asyncio.run() cancel the owned
                # shutdown worker along with every other remaining task.
                await self._join_shutdown()
            except asyncio.CancelledError:
                if run_error is None or isinstance(run_error, asyncio.CancelledError):
                    raise
                # Keep an existing startup/worker failure as the fatal result;
                # later caller cancellation must not turn it into a clean exit.
            except Exception:
                if run_error is None or isinstance(run_error, asyncio.CancelledError):
                    raise
                logger.exception("Repeater cleanup also failed; preserving the original failure")


def main():

    import argparse

    parser = argparse.ArgumentParser(description="pyMC Repeater Daemon")
    parser.add_argument(
        "--config",
        help="Path to config file (default: /etc/openhop_repeater/config.yaml, falls back to /etc/pymc_repeater/config.yaml)",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log level (default: INFO)",
    )

    args = parser.parse_args()

    # Load configuration
    config = load_config(args.config)
    config_path = args.config if args.config else str(resolve_config_path('config.yaml'))

    if args.log_level:
        if "logging" not in config:
            config["logging"] = {}
        config["logging"]["level"] = args.log_level

    # Don't initialize radio here - it will be done inside the async event loop
    daemon = RepeaterDaemon(config, radio=None)
    daemon.config_path = config_path

    # Run
    try:
        asyncio.run(daemon.run())
    except KeyboardInterrupt:
        logger.info("Repeater stopped")
    except asyncio.CancelledError:
        # run() defers cancellation until its owned cleanup has completed.
        logger.info("Repeater stopped (clean shutdown)")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
