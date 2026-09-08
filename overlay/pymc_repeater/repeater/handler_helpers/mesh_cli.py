import logging
import math
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import yaml
from openhop_core.paths import resolve_config_path  # WM1303 v2.7: central config-path helper

logger = logging.getLogger(__name__)


# ==================== WM1303 overlay additions ====================
def _build_dynamic_owner_info(max_len: int = 115) -> str:
    """Build the 'owner.info' string dynamically from runtime device info.

    Used by the WM1303 overlay 'get owner.info' CLI command so each repeater
    reports its own hardware/version, instead of a static config value.

    Fields (in order): software version, hardware model, total RAM, total disk.
    Companion apps use '|' as a line separator inside the value.

    The result is hard-capped at max_len characters (default 115). Fields are
    appended one at a time; if adding a field would exceed max_len, that field
    and any following field are skipped (graceful degradation).
    """
    segments = []

    # 1. Software version (pyMC_WM1303)
    try:
        with open(resolve_config_path('version')) as f:
            version = f.read().strip()
        if version:
            segments.append(f'pyMC_WM1303 v{version}')
        else:
            segments.append('pyMC_WM1303')
    except Exception:
        segments.append('pyMC_WM1303')

    # 2. Hardware model (e.g. 'Raspberry Pi 4 Model B Rev 1.4')
    try:
        with open('/sys/firmware/devicetree/base/model', 'rb') as f:
            model = f.read().rstrip(b'\x00').decode('utf-8', 'replace').strip()
        if model:
            segments.append(model)
    except Exception:
        pass

    # 3. Total RAM
    try:
        with open('/proc/meminfo') as f:
            for line in f:
                if line.startswith('MemTotal:'):
                    kib = int(line.split()[1])
                    segments.append(f'RAM: {kib // 1024} MiB')
                    break
    except Exception:
        pass

    # 4. Total disk on root filesystem
    try:
        st = os.statvfs('/')
        gib = st.f_blocks * st.f_frsize / (1024 ** 3)
        segments.append(f'Disk: {gib:.1f} GiB')
    except Exception:
        pass

    # Join with '|' as line separator, respect max_len.
    result = ''
    for seg in segments:
        candidate = seg if not result else f'{result}|{seg}'
        if len(candidate) > max_len:
            break
        result = candidate
    return result
# ==================== end WM1303 overlay additions ====================


class MeshCLI:

    def __init__(
        self,
        config_path: str,
        config: Dict[str, Any],
        config_manager,  # ConfigManager instance for save & live updates
        identity_type: str = "repeater",
        enable_regions: bool = True,
        send_advert_callback: Optional[Callable] = None,
        identity=None,
        storage_handler=None,
        room_name: Optional[str] = None,
    ):

        self.config_path = Path(config_path)
        self.config = config
        self.config_manager = config_manager
        self.identity_type = identity_type
        self.enable_regions = enable_regions
        self.send_advert_callback = send_advert_callback
        self.identity = identity
        self.storage_handler = storage_handler
        self.room_name = room_name

        # Store event loop reference for thread-safe scheduling
        import asyncio
        try:
            self._event_loop = asyncio.get_running_loop()
        except RuntimeError:
            self._event_loop = None

        # Get repeater config shortcut
        self.repeater_config = config.get("repeater", {})

    def handle_command(self, sender_pubkey: bytes, command: str, is_admin: bool) -> str:

        # Check admin permission first
        if not is_admin:
            return "Error: Admin permission required"

        logger.debug("CLI command received (%d characters)", len(command))

        # Extract optional sequence prefix (XX|)
        prefix = ""
        if len(command) > 4 and command[2] == "|":
            prefix = command[:3]
            command = command[3:]

        # Strip leading/trailing whitespace
        command = command.strip()

        # Route to appropriate handler
        reply = self._route_command(command)

        # Add prefix back to reply if present
        if prefix:
            return prefix + reply
        return reply

    def _route_command(self, command: str) -> str:

        # A room administrator owns one identity, not the shared repeater host.
        # Keep this allowlist ahead of every generic host/credential handler.
        if self.identity_type == "room_server":
            return self._route_room_command(command)

        # Help
        if command == "help" or command.startswith("help "):
            return self._cmd_help(command)

        # System commands
        elif command == "reboot":
            return self._cmd_reboot()
        elif command == "advert":
            return self._cmd_advert()
        elif command.startswith("clock"):
            return self._cmd_clock(command)
        elif command.startswith("time "):
            return self._cmd_time(command)
        elif command == "start ota":
            return "Error: OTA not supported in Python repeater"
        elif command.startswith("password "):
            return self._cmd_password(command)
        elif command == "clear stats":
            return self._cmd_clear_stats()
        elif command == "ver":
            return self._cmd_version()

        # Get commands
        elif command.startswith("get "):
            return self._cmd_get(command[4:])

        # Set commands
        elif command.startswith("set "):
            return self._cmd_set(command[4:])

        # ACL commands
        elif command.startswith("setperm "):
            return self._cmd_setperm(command)
        elif command == "get acl":
            return "Error: Use 'get acl' via serial console only"

        # Region commands (repeaters only)
        elif command.startswith("region"):
            if self.enable_regions:
                return self._cmd_region(command)
            else:
                return "Error: Region commands not available for room servers"

        # Neighbor commands
        elif command == "neighbors":
            return self._cmd_neighbors()
        elif command.startswith("neighbor.remove "):
            return self._cmd_neighbor_remove(command)

        # Temporary radio params
        elif command.startswith("tempradio "):
            return self._cmd_tempradio(command)

        # Sensor commands
        elif command.startswith("sensor "):
            return "Error: Sensor commands not implemented in Python repeater"

        # GPS commands
        elif command.startswith("gps"):
            return "Error: GPS commands not implemented in Python repeater"

        # Logging commands
        elif command.startswith("log "):
            return self._cmd_log(command)

        # Statistics commands
        elif command.startswith("stats-"):
            return "Error: Stats commands not fully implemented yet"

        else:
            return "Unknown command"

    def _saved_room_entry(self, config: dict) -> dict:
        """Resolve this live room in a detached desired snapshot, never recreate it."""
        from repeater.companion.identity_resolve import derive_companion_public_key_hex

        if not self.identity or not self.room_name:
            raise ValueError("Room identity is unavailable")
        identities = config.get("identities")
        if not isinstance(identities, dict):
            raise ValueError("Room is no longer configured; restart required")
        rooms = identities.get("room_servers")
        if not isinstance(rooms, list) or any(not isinstance(room, dict) for room in rooms):
            raise ValueError("Invalid saved room configuration")
        matches = [room for room in rooms
                   if isinstance(room.get("name"), str)
                   and room["name"].strip() == self.room_name.strip()]
        if (len(matches) != 1
                or derive_companion_public_key_hex(matches[0].get("identity_key"))
                != self.identity.get_public_key().hex()):
            raise ValueError("Room was removed, renamed or replaced; restart required")
        return matches[0]

    def _route_room_command(self, command: str) -> str:
        """Room-only CLI; editable values are desired settings, not live ACL state."""
        if command == "help" or command.startswith("help "):
            if command == "help get":
                return (
                    "get public.key|role|name|lat|lon|guest.password|allow.read.only. "
                    "Editable settings report saved values, not the live ACL."
                )
            if command == "help set":
                return (
                    "set name|lat|lon|guest.password|allow.read.only <value>; password <pw>. "
                    "Passwords: max 15 UTF-8 bytes. Saves require restart."
                )
            return (
                "Room: help get|set; password <pw>; advert; clock; ver. "
                "Settings are saved/desired; writes need restart. Host commands require host admin."
            )
        if command == "advert":
            return self._cmd_advert()
        if command == "clock":
            return self._cmd_clock(command)
        if command == "ver":
            return self._cmd_version()
        if command == "get public.key":
            return self._cmd_get("public.key")
        if command == "get role":
            return "> room_server"

        fields = {
            "name": "node_name", "lat": "latitude", "lon": "longitude",
            "password": "admin_password", "admin.password": "admin_password",
            "guest.password": "guest_password", "allow.read.only": "allow_read_only",
        }
        try:
            if command.startswith("get "):
                key = command[4:].strip()
                if key in ("password", "admin.password"):
                    return "Error: Admin password is not readable"
                if key not in fields:
                    return "Error: Shared-host commands require repeater/console admin"
                if self.config_manager is None:
                    return "Error: Configuration manager unavailable"
                with self.config_manager._lock:
                    room = self._saved_room_entry(self.config_manager.read_saved_config())
                    settings = room.get("settings", {})
                    if not isinstance(settings, dict):
                        raise ValueError("Invalid saved room settings")
                    if key == "name":
                        value = settings.get("node_name", settings.get("room_name", room["name"]))
                    elif key == "allow.read.only":
                        value = "on" if settings.get("allow_read_only", True) else "off"
                    elif key in ("lat", "lon"):
                        value = settings.get(fields[key], 0.0)
                    else:
                        value = settings.get(fields[key]) or ""
                return f"> {value} [saved; restart applies]"

            if command.startswith("password "):
                key, value = "password", command[9:]
            elif command.startswith("set "):
                parts = command[4:].split(None, 1)
                if len(parts) != 2:
                    return "Error: Missing value"
                key, value = parts
            else:
                return "Error: Shared-host commands require repeater/console admin"
            if key not in fields:
                return "Error: Shared-host commands require repeater/console admin"
            if key in ("lat", "lon"):
                value = float(value)
                limit = 90 if key == "lat" else 180
                if not math.isfinite(value) or not -limit <= value <= limit:
                    return "Error: Invalid coordinate"
            elif key == "allow.read.only":
                if value.lower() not in ("on", "off"):
                    return "Error: allow.read.only must be on or off"
                value = value.lower() == "on"
            elif not value or "\x00" in value:
                return "Error: Value must be nonempty text without NUL"

            from repeater.room_settings import validate_room_settings

            if self.config_manager is None:
                return "Error: Configuration manager unavailable"
            with self.config_manager._lock:
                saved = self.config_manager.read_saved_config()
                room = self._saved_room_entry(saved)
                settings = room.get("settings", {})
                if not isinstance(settings, dict):
                    raise ValueError("Invalid saved room settings")
                settings = dict(settings)
                settings[fields[key]] = value
                validate_room_settings(settings)
                room["settings"] = settings
                # This is a locked read/modify/save of a fresh detached snapshot.
                # update_and_save(live_update=False) still publishes into config;
                # leave both the running room and its ACL unchanged until restart.
                if not self.config_manager.save_to_file(saved):
                    return "Error: Failed to save room settings"
            return "OK - Room settings saved; restart required to apply"
        except ValueError:
            return "Error: Invalid room setting or room no longer configured; check Console"
        except Exception as exc:
            logger.warning("Room CLI configuration failed (%s)", type(exc).__name__)
            return "Error: Failed to read or save room settings"

    # ==================== Help Command ====================

    def _cmd_help(self, command: str) -> str:
        """Show available commands or detailed help for a specific command."""
        parts = command.split(None, 1)
        if len(parts) == 2:
            return self._help_detail(parts[1])

        lines = [
            "=== pyMC CLI Commands ===",
            "",
            "System:",
            "  reboot              Restart the repeater service",
            "  advert              Send self advertisement",
            "  clock               Show current UTC time",
            "  clock sync          Sync clock (no-op, uses system time)",
            "  ver                 Show version info",
            "  password <pw>       Change admin password",
            "  clear stats         Clear statistics",
            "",
            "Get:",
            "  get name            Node name",
            "  get radio           Radio params (freq,bw,sf,cr)",
            "  get freq            Frequency (MHz)",
            "  get tx              TX power",
            "  get af              Airtime factor",
            "  get repeat          Repeat mode (on/off)",
            "  get lat / get lon   GPS coordinates",
            "  get role            Identity role",
            "  get guest.password  Guest password",
            "  get allow.read.only Read-only access setting",
            "  get advert.interval Advert interval (minutes)",
            "  get flood.advert.interval  Flood advert interval (hours)",
            "  get flood.max       Max flood hops",
            "  get rxdelay         RX delay base",
            "  get txdelay         TX delay factor",
            "  get direct.txdelay  Direct TX delay factor",
            "  get multi.acks      Multi-ack count",
            "  get int.thresh      Interference threshold",
            "  get agc.reset.interval  AGC reset interval",
            "",
            "Set:  (use 'help set' for details)",
            "  set <param> <value>",
            "",
            "Other:",
            "  neighbors           List neighbors",
            "  neighbor.remove <key>  Remove neighbor by pubkey",
            "  tempradio <freq> <bw> <sf> <cr> <timeout_mins>",
            "  setperm <pubkey> <perm>  Set ACL permissions",
            "  log start|stop|erase    Logging control",
        ]
        if self.enable_regions:
            lines.append("  region ...          Region commands")
        lines += ["", "Type 'help <command>' for details on a specific command."]
        return "\n".join(lines)

    def _help_detail(self, topic: str) -> str:
        """Return detailed help for a specific command topic."""
        topic = topic.strip()
        details = {
            "set": (
                "Set commands \u2014 set <param> <value>:\n"
                "  set name <name>        Set node name\n"
                "  set radio <f> <bw> <sf> <cr>  Set radio (restart required)\n"
                "  set freq <mhz>         Set frequency (restart required)\n"
                "  set tx <power>         Set TX power\n"
                "  set af <factor>        Airtime factor\n"
                "  set repeat on|off      Enable/disable repeating\n"
                "  set lat <deg>          Latitude\n"
                "  set lon <deg>          Longitude\n"
                "  set guest.password <pw> Guest password\n"
                "  set allow.read.only on|off  Read-only access\n"
                "  set advert.interval <min>   60-240 minutes\n"
                "  set flood.advert.interval <hr>  3-48 hours\n"
                "  set flood.max <hops>   Max flood hops (max 64)\n"
                "  set rxdelay <val>      RX delay base (>=0)\n"
                "  set txdelay <val>      TX delay factor (>=0)\n"
                "  set direct.txdelay <val>  Direct TX delay (>=0)\n"
                "  set multi.acks <n>     Multi-ack count\n"
                "  set int.thresh <dbm>   Interference threshold\n"
                "  set agc.reset.interval <n>  AGC reset (rounded to x4)"
            ),
            "get": "Get commands \u2014 type 'help' to see all 'get' parameters.",
            "reboot": "Restart the repeater service via systemd.",
            "advert": "Trigger a self-advertisement flood packet.",
            "clock": "'clock' shows UTC time. 'clock sync' is a no-op (system time used).",
            "ver": "Show repeater version and identity type.",
            "password": "password <new_password> \u2014 Change the admin password.",
            "tempradio": (
                "tempradio <freq_mhz> <bw_khz> <sf> <cr> <timeout_mins>\n"
                "  Apply temporary radio parameters that revert after timeout.\n"
                "  freq: 300-2500 MHz, bw: 7-500 kHz, sf: 5-12, cr: 5-8"
            ),
            "neighbors": "List known neighbor nodes from the routing table.",
            "setperm": "setperm <pubkey_hex> <permission_int> \u2014 Set ACL permissions for a node.",
            "log": "log start|stop|erase \u2014 Control logging.",
        }
        return details.get(topic, f"No detailed help for '{topic}'. Type 'help' for command list.")

    # ==================== System Commands ====================

    def _cmd_reboot(self) -> str:
        """Reboot the repeater process."""
        from repeater.service_utils import restart_service

        logger.warning("Reboot command received via mesh CLI")
        success, message = restart_service()

        if success:
            return f"OK - {message}"
        else:
            return f"Error: {message}"

    def _cmd_advert(self) -> str:
        """Send self advertisement."""
        if not self.send_advert_callback:
            logger.warning("Advert command received but no callback configured")
            return "Error: Advert functionality not configured"

        try:
            import asyncio

            async def delayed_advert():
                """Delay advert to let CLI response send first (matches C++ 1500ms delay)."""
                await asyncio.sleep(1.5)
                await self.send_advert_callback()

            if self._event_loop and self._event_loop.is_running():
                asyncio.run_coroutine_threadsafe(delayed_advert(), self._event_loop)
            else:
                return "Error: Event loop not available"

            logger.info("Advert scheduled for sending (1.5s delay)")
            return "OK - Advert scheduled"
        except Exception as e:
            logger.error(f"Failed to schedule advert: {e}", exc_info=True)
            return f"Error: {e}"

    def _cmd_clock(self, command: str) -> str:
        """Handle clock commands."""
        if command == "clock":
            # Display current time
            import datetime

            dt = datetime.datetime.utcnow()
            return f"{dt.hour:02d}:{dt.minute:02d} - {dt.day}/{dt.month}/{dt.year} UTC"
        elif command == "clock sync":
            # Clock sync happens automatically via sender_timestamp in protocol
            return "OK - clock sync not needed (system time used)"
        else:
            return "Unknown clock command"

    def _cmd_time(self, command: str) -> str:
        """Set time - not supported in Python (use system time)."""
        return "Error: Time setting not supported (system time is used)"

    def _cmd_password(self, command: str) -> str:
        """Change admin password."""
        new_password = command[9:].strip()

        if not new_password:
            return "Error: Password cannot be empty"

        try:
            return self._save_changes(
                {"repeater": {"security": {"admin_password": new_password}}},
                reply=f"password now: {new_password}",
            )
        except Exception as e:
            logger.error(f"Failed to save password: {e}")
            return "Error: Failed to save password"

    def _cmd_clear_stats(self) -> str:
        """Clear statistics."""
        # TODO: Implement stats clearing
        return "Error: Not yet implemented"

    def _cmd_version(self) -> str:
        """Get version information."""
        role = "room_server" if self.identity_type == "room_server" else "repeater"
        version = self.config.get("version", "1.0.0")
        return f"pyMC_{role} v{version}"

    # ==================== Get Commands ====================

    def _cmd_get(self, param: str) -> str:
        """Handle get commands."""
        param = param.strip()
        logger.debug("CLI get received (%d characters)", len(param))

        if param in ("radio", "freq", "tx") and self.config.get("radio_type") == "wm1303":
            return "Error: Multichannel radio; view channels A-F in WM1303 Manager"

        if param == "af":
            af = self.repeater_config.get("airtime_factor", 1.0)
            return f"> {af}"

        elif param == "name":
            name = self.repeater_config.get("node_name", self.repeater_config.get("name", "Unknown"))
            return f"> {name}"

        elif param == "repeat":
            mode = self.repeater_config.get("mode", "forward")
            return f"> {'on' if mode == 'forward' else 'off'}"

        elif param == "lat":
            lat = self.repeater_config.get("latitude", 0.0)
            return f"> {lat}"

        elif param == "lon":
            lon = self.repeater_config.get("longitude", 0.0)
            return f"> {lon}"

        elif param == "radio":
            radio = self.config.get("radio", {})
            freq_hz = radio.get("frequency", 915000000)
            bw_hz = radio.get("bandwidth", 125000)
            sf = radio.get("spreading_factor", 7)
            cr = radio.get("coding_rate", 5)
            # Convert Hz to MHz for freq, Hz to kHz for bandwidth (match C++ ftoa output)
            freq_mhz = freq_hz / 1_000_000.0
            bw_khz = bw_hz / 1_000.0
            return f"> {freq_mhz},{bw_khz},{sf},{cr}"

        elif param == "freq":
            freq_hz = self.config.get("radio", {}).get("frequency", 915000000)
            freq_mhz = freq_hz / 1_000_000.0
            return f"> {freq_mhz}"

        elif param == "tx":
            power = self.config.get("radio", {}).get("tx_power", 20)
            return f"> {power}"

        elif param == "public.key":
            if not self.identity:
                return "Error: Identity not available"
            try:
                pubkey = self.identity.get_public_key()
                pubkey_hex = pubkey.hex()
                return f"> {pubkey_hex}"
            except Exception as e:
                logger.error(f"Failed to get public key: {e}")
                return f"Error: {e}"

        elif param == "role":
            role = "room_server" if self.identity_type == "room_server" else "repeater"
            return f"> {role}"

        elif param == "guest.password":
            guest_pw = self.repeater_config.get("security", {}).get("guest_password", "")
            return f"> {guest_pw}"

        elif param == "allow.read.only":
            allow = self.repeater_config.get("security", {}).get("allow_read_only", False)
            return f"> {'on' if allow else 'off'}"

        elif param == "advert.interval":
            interval = self.repeater_config.get("advert_interval_minutes", 120)
            return f"> {interval}"

        elif param == "flood.advert.interval":
            interval = self.repeater_config.get("send_advert_interval_hours", 10)
            return f"> {interval}"

        elif param == "flood.max":
            max_flood = self.repeater_config.get("max_flood_hops", 64)
            return f"> {max_flood}"

        elif param == "rxdelay":
            delay = self.config.get("delays", {}).get("rx_delay_base", 0.0)
            return f"> {delay}"

        elif param == "txdelay":
            delay = self.config.get("delays", {}).get("tx_delay_factor", 1.0)
            return f"> {delay}"

        elif param == "direct.txdelay":
            delay = self.config.get("delays", {}).get("direct_tx_delay_factor", 0.5)
            return f"> {delay}"

        elif param == "multi.acks":
            acks = self.repeater_config.get("multi_acks", 0)
            return f"> {acks}"

        elif param == "int.thresh":
            thresh = self.repeater_config.get("interference_threshold", -120)
            return f"> {thresh}"

        elif param == "agc.reset.interval":
            interval = self.repeater_config.get("agc_reset_interval", 0)
            return f"> {interval}"

        elif param == "path.hash.mode":
            return f"> {self.config.get('mesh', {}).get('path_hash_mode', 0)}"

        elif param == "loop.detect":
            return f"> {self.config.get('mesh', {}).get('loop_detect', 'minimal')}"

        # ==================== WM1303 overlay additions ====================
        # Companion apps query "owner.info" via TXT_MSG CLI 'get' command.
        # Upstream pymc_repeater does not implement this key, so it falls
        # through to '??: <key>' which the companion displays literally.
        # We build the value dynamically from runtime device info so each
        # repeater reports its own hardware/version. Hard-capped at 115 chars.
        # Companion apps use '|' as a line separator inside the value.
        elif param == "owner.info":
            return f"> {_build_dynamic_owner_info(max_len=115)}"
        # ==================== end WM1303 overlay additions ====================

        else:
            return f"??: {param}"

    # ==================== Set Commands ====================

    def _save_changes(self, updates: dict, *, live_update: bool = True, reply: str = "OK") -> str:
        """Use the same transactional save path as the web configuration API."""
        result = self.config_manager.update_and_save(updates, live_update=live_update)
        if not result.get("saved"):
            return "Error: Failed to save config"
        self.repeater_config = self.config.get("repeater", {})
        return reply

    def _cmd_set(self, param: str) -> str:
        """Handle set commands."""
        parts = param.split(None, 1)
        if len(parts) < 2:
            return "Error: Missing value"

        key, value = parts[0], parts[1]

        try:
            updates = {"repeater": {}}
            section = updates["repeater"]
            reply = "OK"
            live_update = True

            if key == "af":
                factor = float(value)
                if not math.isfinite(factor) or factor < 0:
                    return "Error: airtime factor must be non-negative"
                section["airtime_factor"] = factor
            elif key == "name":
                section["node_name"] = value
            elif key == "repeat":
                if value.lower() not in ("on", "off"):
                    return "Error: repeat must be on or off"
                section["mode"] = "forward" if value.lower() == "on" else "monitor"
                reply = f"OK - repeat is now {value.upper()}"
            elif key in ("lat", "lon"):
                coordinate = float(value)
                limit = 90 if key == "lat" else 180
                if not -limit <= coordinate <= limit:
                    return "Error: invalid coordinate"
                section["latitude" if key == "lat" else "longitude"] = coordinate
            elif key in ("radio", "freq", "tx"):
                if self.config.get("radio_type") == "wm1303":
                    return "Error: Configure channels A-F in WM1303 Manager, then restart"
                radio = {}
                if key == "radio":
                    radio_parts = value.split()
                    if len(radio_parts) != 4:
                        return "Error: Expected freq bw sf cr"
                    frequency, bandwidth = map(float, radio_parts[:2])
                    sf, cr = map(int, radio_parts[2:])
                    if not (150 <= frequency <= 2500 and 7 <= bandwidth <= 500
                            and 5 <= sf <= 12 and 5 <= cr <= 8):
                        return "Error, invalid radio params"
                    radio.update(frequency=round(frequency * 1_000_000),
                                 bandwidth=round(bandwidth * 1_000),
                                 spreading_factor=sf, coding_rate=cr)
                elif key == "freq":
                    frequency = float(value)
                    if not 150 <= frequency <= 2500:
                        return "Error, invalid radio params"
                    radio["frequency"] = round(frequency * 1_000_000)
                else:
                    radio["tx_power"] = int(value)
                updates = {"radio": radio}
                live_update = False
                reply = "OK - restart repeater to apply"
            elif key in ("guest.password", "allow.read.only"):
                if key == "allow.read.only":
                    if value.lower() not in ("on", "off"):
                        return "Error: allow.read.only must be on or off"
                    section["security"] = {"allow_read_only": value.lower() == "on"}
                else:
                    section["security"] = {"guest_password": value}
            elif key == "advert.interval":
                minutes = int(value)
                if minutes != 0 and not 60 <= minutes <= 240:
                    return "Error: interval range is 60-240 minutes"
                section["advert_interval_minutes"] = minutes
            elif key == "flood.advert.interval":
                hours = int(value)
                if hours != 0 and not 3 <= hours <= 48:
                    return "Error: interval range is 3-48 hours"
                section["send_advert_interval_hours"] = hours
            elif key == "flood.max":
                hops = int(value)
                if not 0 <= hops <= 64:
                    return "Error: max 64"
                section["max_flood_hops"] = hops
            elif key in ("rxdelay", "txdelay", "direct.txdelay"):
                delay = float(value)
                limit = 20 if key == "rxdelay" else 2
                if not 0 <= delay <= limit:
                    return f"Error, must be 0-{limit}"
                setting = {"rxdelay": "rx_delay_base", "txdelay": "tx_delay_factor",
                           "direct.txdelay": "direct_tx_delay_factor"}[key]
                updates = {"delays": {setting: delay}}
            elif key in ("path.hash.mode", "loop.detect"):
                if key == "path.hash.mode":
                    mode = int(value)
                    if mode not in (0, 1, 2):
                        return "Error: path.hash.mode must be 0, 1, or 2"
                    updates = {"mesh": {"path_hash_mode": mode}}
                else:
                    mode = value.lower()
                    if mode not in ("off", "minimal", "moderate", "strict"):
                        return "Error: loop.detect must be off, minimal, moderate, or strict"
                    updates = {"mesh": {"loop_detect": mode}}
            elif key == "multi.acks":
                section["multi_acks"] = int(value)
            elif key == "int.thresh":
                section["interference_threshold"] = int(value)
            elif key == "agc.reset.interval":
                rounded = (int(value) // 4) * 4
                section["agc_reset_interval"] = rounded
                reply = f"OK - interval rounded to {rounded}"
            elif key == "owner.info":
                # WM1303 reports runtime device information, not a stored label.
                return "Error: owner.info is dynamic and read-only"
            else:
                return f"unknown config: {key}"

            return self._save_changes(updates, live_update=live_update, reply=reply)

        except ValueError as e:
            return f"Error: invalid value - {e}"
        except Exception as e:
            logger.error(f"Set command error: {e}")
            return f"Error: {e}"

    # ==================== ACL Commands ====================

    def _cmd_setperm(self, command: str) -> str:
        """Set permissions for a public key."""
        # Format: setperm {pubkey-hex} {permissions-int}
        parts = command[8:].split()
        if len(parts) < 2:
            return "Err - bad params"

        pubkey_hex = parts[0]
        try:
            permissions = int(parts[1])
        except ValueError:
            return "Err - invalid permissions"

        # TODO: Apply permissions via ACL
        logger.info(f"setperm command: {pubkey_hex} -> {permissions}")
        return "Error: Not yet implemented - use config file"

    # ==================== Region Commands ====================

    def _cmd_region(self, command: str) -> str:
        """Handle region commands."""
        parts = command.split()

        if len(parts) == 1:
            return "Error: Region commands not implemented in Python repeater"

        subcommand = parts[1]

        if subcommand == "load":
            return "Error: Region commands not implemented"
        elif subcommand == "save":
            return "Error: Region commands not implemented"
        elif subcommand in ("allowf", "denyf", "get", "home", "put", "remove"):
            return "Error: Region commands not implemented"
        else:
            return "Err - ??"

    # ==================== Neighbor Commands ====================

    def _cmd_neighbors(self) -> str:
        """List neighbors."""
        if not self.storage_handler:
            return "Error: Storage not available"

        try:
            neighbors = self.storage_handler.get_neighbors()

            if not neighbors:
                return "No neighbors discovered yet"

            # MeshCore neighbours means repeaters heard without an intermediate hop.
            filtered_neighbors = {
                pubkey: info
                for pubkey, info in neighbors.items()
                if info.get("is_repeater", False) and info.get("zero_hop", False)
            }

            if not filtered_neighbors:
                return "No zero hop repeaters discovered yet"

            # Format output similar to C++ version
            # Format: "<pubkey_prefix> heard Xs ago"
            import time

            current_time = int(time.time())

            lines = []
            for pubkey, info in filtered_neighbors.items():
                last_seen = info.get("last_seen", 0)
                seconds_ago = int(current_time - last_seen)

                # Get first 4 bytes of pubkey as hex (match C++ format)
                pubkey_short = pubkey[:8] if len(pubkey) >= 8 else pubkey
                snr = info.get("snr", 0) or 0

                # Format: <4byte_hex>:<seconds_ago>:<snr> (matches C++ format)
                lines.append(f"{pubkey_short}:{seconds_ago}:{int(snr)}")

            return "\n".join(lines)

        except Exception as e:
            logger.error(f"Failed to list neighbors: {e}", exc_info=True)
            return f"Error: {e}"

    def _cmd_neighbor_remove(self, command: str) -> str:
        """Remove a neighbor."""
        pubkey_hex = command[16:].strip()

        if not pubkey_hex:
            return "ERR: Missing pubkey"

        # TODO: Remove neighbor from routing table
        logger.info(f"neighbor.remove: {pubkey_hex}")
        return "Error: Not yet implemented"

    # ==================== Temporary Radio Commands ====================

    def _cmd_tempradio(self, command: str) -> str:
        """Apply temporary radio parameters."""
        # Format: tempradio {freq} {bw} {sf} {cr} {timeout_mins}
        parts = command[10:].split()

        if len(parts) < 5:
            return "Error: Expected freq bw sf cr timeout_mins"

        try:
            freq = float(parts[0])
            bw = float(parts[1])
            sf = int(parts[2])
            cr = int(parts[3])
            timeout_mins = int(parts[4])

            # Validate
            if not (300.0 <= freq <= 2500.0):
                return "Error: invalid frequency"
            if not (7.0 <= bw <= 500.0):
                return "Error: invalid bandwidth"
            if not (5 <= sf <= 12):
                return "Error: invalid spreading factor"
            if not (5 <= cr <= 8):
                return "Error: invalid coding rate"
            if timeout_mins <= 0:
                return "Error: invalid timeout"

            # TODO: Apply temporary radio parameters
            logger.info(f"tempradio: {freq}MHz {bw}kHz SF{sf} CR4/{cr} for {timeout_mins}min")
            return "Error: Not yet implemented"

        except ValueError:
            return "Error, invalid params"

    # ==================== Logging Commands ====================

    def _cmd_log(self, command: str) -> str:
        """Handle log commands."""
        if command == "log start":
            # TODO: Enable logging
            return "Error: Not yet implemented"
        elif command == "log stop":
            # TODO: Disable logging
            return "Error: Not yet implemented"
        elif command == "log erase":
            # TODO: Clear log file
            return "Error: Not yet implemented"
        elif command == "log":
            return "Error: Use journalctl to view logs"
        else:
            return "Unknown log command"
