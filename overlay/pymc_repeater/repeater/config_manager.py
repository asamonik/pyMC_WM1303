import logging
import os
from copy import deepcopy
import yaml
from typing import Optional, Dict, Any, List

from repeater.atomic_file import atomic_write_text
from repeater.config import CONFIG_WRITE_LOCK
from repeater.room_settings import validate_room_configuration

logger = logging.getLogger("ConfigManager")


class ConfigManager:
    """Manages configuration persistence and live updates to the daemon."""
    
    def __init__(self, config_path: str, config: dict, daemon_instance=None):
        """
        Initialize ConfigManager.
        
        Args:
            config_path: Path to the YAML config file
            config: Reference to the config dictionary
            daemon_instance: Optional reference to the daemon for live updates
        """
        self.config_path = config_path
        self.config = config
        self.daemon = daemon_instance
        self._lock = CONFIG_WRITE_LOCK

    def read_saved_config(self) -> Dict[str, Any]:
        """Read a detached desired snapshot without applying staged settings.

        Callers doing read/modify/write hold _lock for the whole transaction.
        Missing initial files can be created from the supplied configuration;
        unreadable or malformed existing files must never be overwritten.
        """
        with self._lock:
            try:
                with open(self.config_path, encoding="utf-8") as stream:
                    saved = yaml.safe_load(stream)
            except FileNotFoundError:
                return deepcopy(self.config)
            if not isinstance(saved, dict):
                raise ValueError("Saved configuration must be a YAML mapping")
            return saved

    @staticmethod
    def _merge_config(target: dict, updates: dict) -> None:
        """Merge nested settings without discarding siblings or sharing inputs."""
        for key, value in updates.items():
            if isinstance(value, dict) and isinstance(target.get(key), dict):
                ConfigManager._merge_config(target[key], value)
            else:
                target[key] = deepcopy(value)

    def _get_live_radio_snapshot(self) -> Dict[str, Any]:
        radio_cfg = self.config.get("radio", {}) or {}
        return {
            "frequency": int(radio_cfg.get("frequency", 0) or 0),
            "bandwidth": int(radio_cfg.get("bandwidth", 0) or 0),
            "spreading_factor": int(radio_cfg.get("spreading_factor", 0) or 0),
            "coding_rate": int(radio_cfg.get("coding_rate", 0) or 0),
            "tx_power": int(radio_cfg.get("tx_power", 0) or 0),
        }

    def _sync_repeater_handler_radio_config(self, radio_cfg: Dict[str, Any]) -> None:
        repeater_handler = getattr(self.daemon, "repeater_handler", None)
        if not repeater_handler or not hasattr(repeater_handler, "radio_config"):
            return

        if not isinstance(repeater_handler.radio_config, dict):
            repeater_handler.radio_config = {}

        repeater_handler.radio_config.update(
            {
                key: value
                for key, value in radio_cfg.items()
                if value is not None and (key == "tx_power" or value != 0)
            }
        )

    def _kiss_transport_restart_required(self) -> bool:
        radio = getattr(self.daemon, "radio", None)
        kiss_cfg = self.config.get("kiss", {}) or {}
        if radio is None or not kiss_cfg:
            return False

        runtime_port = getattr(radio, "port", None)
        runtime_baudrate = getattr(radio, "baudrate", None)

        configured_port = kiss_cfg.get("port")
        configured_baudrate = kiss_cfg.get("baud_rate")

        if configured_port and runtime_port and str(configured_port) != str(runtime_port):
            logger.info("KISS port change detected; service restart required")
            return True

        if configured_baudrate and runtime_baudrate and int(configured_baudrate) != int(runtime_baudrate):
            logger.info("KISS baud rate change detected; service restart required")
            return True

        return False

    def _apply_live_radio_config(self) -> bool:
        radio = getattr(self.daemon, "radio", None)
        if radio is None:
            logger.warning("Radio not available for live update")
            return False

        radio_cfg = self._get_live_radio_snapshot()

        try:
            if hasattr(radio, "configure_radio"):
                if hasattr(radio, "radio_config") and isinstance(radio.radio_config, dict):
                    radio.radio_config.update(radio_cfg)

                applied = radio.configure_radio(
                    frequency=radio_cfg["frequency"],
                    bandwidth=radio_cfg["bandwidth"],
                    spreading_factor=radio_cfg["spreading_factor"],
                    coding_rate=radio_cfg["coding_rate"],
                )
                if not applied:
                    logger.warning("Live radio reconfiguration failed")
                    return False
                # SX1262 configure_radio updates modulation only; KISS also
                # applies power. Check the resulting value before using the
                # separate setter so both contracts remain supported.
                if getattr(radio, "tx_power", None) != radio_cfg["tx_power"]:
                    set_power = getattr(radio, "set_tx_power", None)
                    if not callable(set_power) or not set_power(radio_cfg["tx_power"]):
                        logger.warning("Live TX power update failed or is unsupported")
                        return False
            else:
                current_frequency = getattr(radio, "frequency", None)
                current_bandwidth = getattr(radio, "bandwidth", None)
                current_spreading_factor = getattr(radio, "spreading_factor", None)
                current_coding_rate = getattr(radio, "coding_rate", None)
                current_tx_power = getattr(radio, "tx_power", None)

                if current_frequency != radio_cfg["frequency"]:
                    set_frequency = getattr(radio, "set_frequency", None)
                    if not callable(set_frequency) or not set_frequency(radio_cfg["frequency"]):
                        return False

                if current_tx_power != radio_cfg["tx_power"]:
                    set_power = getattr(radio, "set_tx_power", None)
                    if not callable(set_power) or not set_power(radio_cfg["tx_power"]):
                        return False

                coding_rate_changed = current_coding_rate != radio_cfg["coding_rate"]
                if coding_rate_changed:
                    setattr(radio, "coding_rate", radio_cfg["coding_rate"])

                if current_spreading_factor != radio_cfg["spreading_factor"]:
                    if not hasattr(radio, "set_spreading_factor"):
                        return False
                    if not radio.set_spreading_factor(radio_cfg["spreading_factor"]):
                        return False

                if current_bandwidth != radio_cfg["bandwidth"]:
                    if not hasattr(radio, "set_bandwidth"):
                        return False
                    if not radio.set_bandwidth(radio_cfg["bandwidth"]):
                        return False
                elif coding_rate_changed:
                    if hasattr(radio, "set_bandwidth"):
                        if not radio.set_bandwidth(radio_cfg["bandwidth"]):
                            return False
                    elif hasattr(radio, "set_spreading_factor"):
                        if not radio.set_spreading_factor(radio_cfg["spreading_factor"]):
                            return False
                    else:
                        return False

            self._sync_repeater_handler_radio_config(radio_cfg)
            handler = getattr(self.daemon, "repeater_handler", None)
            airtime_manager = getattr(handler, "airtime_mgr", None)
            if airtime_manager is not None and hasattr(airtime_manager, "refresh_radio_params"):
                airtime_manager.refresh_radio_params(deepcopy(self.config.get("radio", {}) or {}))
            logger.info("Applied live radio configuration to running daemon")
            return True
        except Exception as e:
            logger.error(f"Failed to apply live radio config: {e}", exc_info=True)
            return False
    
    def save_to_file(self, config: Optional[dict] = None) -> bool:
        """
        Save current config to YAML file.

        This is a full replacement, not a scoped update. Runtime callers should
        prefer update_and_save() so pending settings from other writers survive.
        
        Returns:
            True if successful, False otherwise
        """
        try:
            with self._lock:
                candidate = self.config if config is None else config
                validate_room_configuration(candidate)
                # Use safe_dump with explicit width to prevent line wrapping
                # Setting width to a very large number prevents truncation of long strings like identity keys
                content = yaml.safe_dump(
                    candidate,
                    default_flow_style=False, 
                    indent=2, 
                    width=1000000,  # Very large width to prevent any line wrapping
                    sort_keys=False,
                    allow_unicode=True
                )
                atomic_write_text(self.config_path, content)
            logger.info(f"Configuration saved to {self.config_path}")
            return True
        except Exception as e:
            logger.error(f"Failed to save config to {self.config_path}: {e}", exc_info=True)
            return False
    
    def live_update_daemon(self, sections: Optional[List[str]] = None, *,
                           replace_sections: bool = False) -> bool:
        """
        Apply configuration changes to the running daemon's in-memory config.
        
        Args:
            sections: List of config sections to update (e.g., ['repeater', 'delays']).
                     If None, updates all common sections.
            replace_sections: Replace supplied daemon sections, including removal
                of omitted nested keys, instead of merging them.
        
        Returns:
            True if live update was successful, False otherwise
        """
        if not self.daemon or not hasattr(self.daemon, 'config'):
            logger.warning("Daemon not available for live update")
            return False
        
        try:
            daemon_config = self.daemon.config
            live_update_ok = True
            
            # Default sections to update if not specified
            if sections is None:
                sections = ['repeater', 'delays', 'radio', 'acl', 'identities', 'glass']
            
            # Update each section
            for section in sections:
                if section in self.config:
                    if section not in daemon_config:
                        daemon_config[section] = {}
                    
                    # Deep copy the section to avoid reference issues
                    if replace_sections:
                        daemon_config[section] = deepcopy(self.config[section])
                    elif isinstance(self.config[section], dict):
                        if not isinstance(daemon_config[section], dict):
                            daemon_config[section] = {}
                        self._merge_config(daemon_config[section], self.config[section])
                    else:
                        daemon_config[section] = deepcopy(self.config[section])
                    
                    logger.debug(f"Live updated daemon config section: {section}")
            
            logger.info(f"Live updated daemon config sections: {', '.join(sections)}")

            # Login ACLs cache credentials at registration. Refresh only the
            # repeater ACL; room servers retain their per-identity passwords.
            if 'repeater' in sections:
                login_helper = getattr(self.daemon, 'login_helper', None)
                refresh_security = getattr(login_helper, 'refresh_repeater_security', None)
                if callable(refresh_security):
                    if refresh_security(daemon_config) is False:
                        logger.warning("Repeater ACL security refresh failed")
                        live_update_ok = False

                # Discovery handlers are constructed at startup and captured
                # by companion servers. A config edit cannot replace those
                # callbacks without restarting the service.
                if hasattr(self.daemon, 'discovery_helper'):
                    discovery_enabled = bool(daemon_config.get('repeater', {}).get('allow_discovery', True))
                    if discovery_enabled != (self.daemon.discovery_helper is not None):
                        logger.info("Discovery policy change requires a service restart")
                        live_update_ok = False

            if 'duty_cycle' in sections:
                # AirtimeManager reads enforcement dynamically but caches this
                # limit. Refresh it without resetting its rolling TX history.
                handler = getattr(self.daemon, 'repeater_handler', None)
                airtime_manager = getattr(handler, 'airtime_mgr', None)
                if airtime_manager is None:
                    live_update_ok = False
                else:
                    airtime_manager.max_airtime_per_minute = daemon_config.get('duty_cycle', {}).get(
                        'max_airtime_per_minute', 3600
                    )
            
            # Mesh settings include the engine's loop-detection policy.
            if self.daemon and hasattr(self.daemon, 'repeater_handler'):
                if any(s in ['delays', 'repeater', 'mesh'] for s in sections):
                    if hasattr(self.daemon.repeater_handler, 'reload_runtime_config'):
                        if self.daemon.repeater_handler.reload_runtime_config() is False:
                            logger.warning("RepeaterHandler runtime config reload failed")
                            live_update_ok = False
                        else:
                            logger.info("Reloaded RepeaterHandler runtime config")
            
            # Also reload advert_helper config if repeater section changed
            if self.daemon and hasattr(self.daemon, 'advert_helper') and self.daemon.advert_helper:
                if 'repeater' in sections:
                    if hasattr(self.daemon.advert_helper, 'reload_config'):
                        if self.daemon.advert_helper.reload_config() is False:
                            logger.warning("AdvertHelper config reload failed")
                            live_update_ok = False
                        else:
                            logger.info("Reloaded AdvertHelper config")

            # Re-apply dispatcher path hash mode when mesh section changed
            if 'mesh' in sections and self.daemon and hasattr(self.daemon, 'dispatcher'):
                mesh_cfg = self.daemon.config.get("mesh", {})
                path_hash_mode = mesh_cfg.get("path_hash_mode", 0)
                if path_hash_mode not in (0, 1, 2):
                    logger.warning(
                        f"Invalid mesh.path_hash_mode={path_hash_mode}, must be 0/1/2; using 0"
                    )
                    path_hash_mode = 0
                self.daemon.dispatcher.set_default_path_hash_mode(path_hash_mode)
                logger.info(f"Reloaded path hash mode: mesh.path_hash_mode={path_hash_mode}")

            if 'radio_type' in sections:
                logger.info("radio_type change detected; service restart required")
                live_update_ok = False

            if 'kiss' in sections and self._kiss_transport_restart_required():
                live_update_ok = False

            if 'radio' in sections:
                live_update_ok = self._apply_live_radio_config() and live_update_ok
            
            return live_update_ok
            
        except Exception as e:
            logger.error(f"Failed to live update daemon config: {e}", exc_info=True)
            return False
    
    def update_and_save(self, 
                       updates: Dict[str, Any], 
                       live_update: bool = True,
                       live_update_sections: Optional[List[str]] = None, *,
                       replace_sections: bool = False) -> Dict[str, Any]:
        """
        Apply updates to config, save to file, and optionally live update daemon.
        
        This is the main method that should be used by both mesh_cli and api_endpoints.
        
        Args:
            updates: Dictionary of config updates in nested format.
                    Example: {"repeater": {"node_name": "NewName"}, "delays": {"tx_delay_factor": 1.5}}
            live_update: Whether to apply changes to running daemon immediately
            live_update_sections: Specific sections to live update. If None, auto-detects from updates.
            replace_sections: Replace only the supplied top-level sections instead
                of recursively merging them. Unmentioned saved sections survive.
        
        Returns:
            Dict with keys:
                - success: bool - Whether operation succeeded
                - saved: bool - Whether config was saved to file
                - live_updated: bool - Whether daemon was live updated
                - error: str (optional) - Error message if failed
        """
        result: Dict[str, Any] = {
            "success": False,
            "saved": False,
            "live_updated": False
        }
        
        try:
            if not isinstance(updates, dict):
                raise ValueError("Configuration updates must be a mapping")
            with self._lock:
                # Preserve Manager/manual settings staged since startup. Only
                # requested updates are reflected into the running config below.
                candidate = self.read_saved_config()
                if replace_sections:
                    candidate.update(deepcopy(updates))
                else:
                    self._merge_config(candidate, updates)
                # Keep validation errors actionable and reject before either
                # persistence or publication of the merged live candidate.
                validate_room_configuration(candidate)
                result["saved"] = self.save_to_file(candidate)

                if not result["saved"]:
                    result["error"] = "Failed to save config to file"
                    return result

                if replace_sections:
                    self.config.update(deepcopy(updates))
                else:
                    self._merge_config(self.config, updates)
                if live_update:
                    if live_update_sections is None:
                        live_update_sections = list(updates.keys())
                    if replace_sections:
                        result["live_updated"] = self.live_update_daemon(
                            live_update_sections, replace_sections=True
                        )
                    else:
                        result["live_updated"] = self.live_update_daemon(live_update_sections)
            
            result["success"] = result["saved"]
            return result
            
        except Exception as e:
            logger.error(f"Error in update_and_save: {e}", exc_info=True)
            result["error"] = str(e)
            return result
    
    def update_nested(self, path: str, value: Any, live_update: bool = True) -> Dict[str, Any]:
        """
        Update a nested config value using dot notation.
        
        Convenience method for simple updates like "repeater.node_name" = "NewName"
        
        Args:
            path: Dot-separated path to config value (e.g., "repeater.node_name")
            value: Value to set
            live_update: Whether to apply changes to running daemon
        
        Returns:
            Result dict from update_and_save
        """
        parts = path.split('.')
        
        if len(parts) == 1:
            # Top-level key
            updates = {parts[0]: value}
        elif len(parts) == 2:
            # Nested one level (most common case)
            updates = {parts[0]: {parts[1]: value}}
        else:
            # Build nested dict for deeper paths
            updates = {}
            current = updates
            for i, part in enumerate(parts[:-1]):
                if i == 0:
                    current[part] = {}
                    current = current[part]
                else:
                    current[part] = {}
                    current = current[part]
            current[parts[-1]] = value
        
        # Determine which section to live update
        section = parts[0]
        
        return self.update_and_save(
            updates=updates,
            live_update=live_update,
            live_update_sections=[section] if live_update else None
        )
    
    def get_status(self) -> Dict[str, Any]:
        """
        Get status information about the ConfigManager.
        
        Returns:
            Dict with config file path, existence, daemon availability
        """
        return {
            "config_path": self.config_path,
            "config_exists": os.path.exists(self.config_path),
            "daemon_available": self.daemon is not None and hasattr(self.daemon, 'config'),
            "config_sections": list(self.config.keys()) if self.config else []
        }
