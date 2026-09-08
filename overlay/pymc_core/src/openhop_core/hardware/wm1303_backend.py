"""WM1303/SX1303 concentrator backend for pyMC_Repeater.

RF0-TX Architecture (proper hardware TX path):
  - SX1250_0 (RF0): RX + TX via SKY66420 FEM (PA + LNA + RF Switch)
  - SX1250_1 (RF1): RX only (no PA, SAW filter path)
  - IF chains 0-2 on RF0 for RX
  - TX via PULL_RESP to lora_pkt_fwd -> HAL routes to RF0 (tx_enable=true)
  - Direct TX: no stop/start cycle needed (RF0 has proper FEM for TX/RX switching)
  - Self-echo detection discards own TX heard back on RX
  - Per-channel TX queues serialize transmissions
"""
from __future__ import annotations

import asyncio
import base64
import collections
import json
import logging
import os
import random
import re
import signal
import socket
import struct
import subprocess
import tempfile
import threading
import time
from openhop_core.meshcore_wire import packet_hash
import sqlite3

from contextlib import contextmanager as _contextmanager
from openhop_core.paths import resolve_config_path  # WM1303 v2.7: central config-path helper

# Region configuration (regulatory frequency bands, SX1261 image calibration)
try:
    from .region_config import (
        get_tx_bounds as _region_get_tx_bounds,
        get_region_summary as _region_get_summary,
        DEFAULT_REGION as _REGION_DEFAULT,
    )
    _REGION_AVAILABLE = True
except ImportError:  # pragma: no cover
    _REGION_AVAILABLE = False
    _REGION_DEFAULT = "EU868"


# --- systemd watchdog notification (sd_notify) ---------------------------------
# Used to feed the systemd service-level hardware watchdog (Type=notify,
# WatchdogSec=). Prefers python3-systemd if available, otherwise falls back to a
# pure-Python AF_UNIX datagram write to $NOTIFY_SOCKET. No-op when not running
# under systemd (NOTIFY_SOCKET unset), so it is safe in dev/manual runs.
try:
    from systemd import daemon as _sd_daemon  # type: ignore
    _SD_NATIVE = True
except Exception:  # pragma: no cover
    _sd_daemon = None
    _SD_NATIVE = False


def _sd_notify(state: str) -> bool:
    """Send a notification (e.g. 'READY=1' or 'WATCHDOG=1') to systemd.

    Returns True if the message was sent, False otherwise (incl. no systemd).
    Never raises; watchdog keepalive must never crash the loop.
    """
    try:
        if _SD_NATIVE and _sd_daemon is not None:
            return bool(_sd_daemon.notify(state))
        addr = os.environ.get('NOTIFY_SOCKET')
        if not addr:
            return False
        # Abstract namespace socket starts with '@'
        if addr[0] == '@':
            addr = '\0' + addr[1:]
        _sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_CLOEXEC)
        try:
            _sock.connect(addr)
            _sock.sendall(state.encode('utf-8'))
            return True
        finally:
            _sock.close()
    except Exception as _e:  # pragma: no cover
        logger.debug('sd_notify(%s) failed: %s', state, _e)
        return False


class _SharedConn:
    """Module-level shared SQLite connection with thread-safe access.

    Instead of creating/closing a connection on every DB call (which causes FD
    churn and memory overhead), we maintain a single persistent connection per
    database path.  Thread safety is ensured via RLock + WAL mode + busy_timeout.
    """

    def __init__(self, path):
        self._path = str(path)
        self._conn = None
        self._lock = threading.RLock()

    def _ensure_conn(self):
        if self._conn is None:
            connection = sqlite3.connect(
                self._path,
                timeout=10,
                check_same_thread=False,
            )
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=NORMAL")
                connection.execute("PRAGMA busy_timeout=5000")
                connection.execute("PRAGMA cache_size=-512")
                connection.execute("PRAGMA mmap_size=0")
                connection.execute("PRAGMA temp_store=MEMORY")
            except BaseException:
                connection.close()
                raise
            self._conn = connection
        return self._conn

    def __enter__(self):
        self._lock.acquire()
        try:
            return self._ensure_conn()
        except BaseException:
            self._lock.release()
            raise

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            if self._conn:
                if exc_type is None:
                    self._conn.commit()
                else:
                    self._conn.rollback()
        finally:
            self._lock.release()
        return False


# Module-level shared connection registry (Python 3.13 compatible)
_shared_conn_instances = {}  # path -> _SharedConn
_shared_conn_lock = threading.Lock()


def _get_shared_conn(path):
    """Get or create a shared connection for the given DB path."""
    key = str(path)
    if key not in _shared_conn_instances:
        with _shared_conn_lock:
            if key not in _shared_conn_instances:
                _shared_conn_instances[key] = _SharedConn(path)
    return _shared_conn_instances[key]


@_contextmanager
def _db_conn(path, timeout=5):
    """Thread-safe access to a shared persistent SQLite connection."""
    shared = _get_shared_conn(path)
    with shared as conn:
        yield conn

import math
from pathlib import Path
from typing import Any

from openhop_core.hardware.tx_queue import (
    ChannelTXQueue, TXQueueManager, GlobalTXScheduler, MAX_CHANNELS,
    _bw_hz_to_str as _queue_bw_hz_to_str, estimate_lora_airtime_ms,
)

logger = logging.getLogger('WM1303Backend')

# ---------------------------------------------------------------------------
# Packet-trace callback hook (layering: openhop_core must NOT import
# pymc_repeater.web.packet_trace directly). The repeater registers a callback
# at startup via set_trace_callback(). When no callback is registered (e.g.
# standalone openhop_core use), _trace() is a safe no-op.
#
# Empirical HAL-internal latencies used to backdate TX-phase trace events
# relative to the post-TX TX_ACK arrival moment. Values are typical for the
# WM1303 + lora_pkt_fwd stack on Raspberry Pi; they may vary by a few ms but
# are stable enough for chronological trace visualization.
# ---------------------------------------------------------------------------
_trace_callback = None  # Optional[Callable[..., None]]

# Typical latencies (ms) relative to PULL_RESP sendto completion:
_PRE_NOISEFLOOR_MS = 16   # pull_resp_sent -> [CAD] TX noisefloor FSK read
_PRE_RF_TX_MS      = 88   # pull_resp_sent -> [imme] direct TX sent (rf_tx_start)
_POST_RF_RESTART_MS = 209  # rf_tx_end -> SX1261 'Deferred LoRa RX restart after TX inhibit cleared'
_ACK_ROUNDTRIP_MS   = 42   # sx1261_rx_restart -> post-TX TX_ACK UDP arrival


def set_trace_callback(fn) -> None:
    """Register a packet-trace callback. Called by repeater at startup.

    The callback receives the same args as packet_trace.trace_event:
      (pkt_hash: str, step_name: str, channel='', pkt_type='',
       detail='', status='ok', ts_offset_ms=0)
    """
    global _trace_callback
    _trace_callback = fn


def _trace(pkt_hash, step_name, **kwargs) -> None:
    """Safe no-op wrapper around the registered trace callback."""
    cb = _trace_callback
    if cb is None or not pkt_hash:
        return
    try:
        cb(pkt_hash, step_name, **kwargs)
    except Exception as _e:
        logger.debug('WM1303Backend: _trace callback failed: %s', _e)

# Semtech UDP packet forwarder protocol identifiers
PKT_PUSH_DATA = 0x00
PKT_PUSH_ACK  = 0x01
PKT_PULL_DATA = 0x02
PKT_PULL_RESP = 0x03
PKT_PULL_ACK  = 0x04
PKT_TX_ACK    = 0x05
PROTOCOL_VER  = 0x02

# ---------------------------------------------------------------------------
# Detect PKTFWD_DIR dynamically (supports non-pi users and alternate distros)
# Priority: config.yaml > systemd service User= home > common user homes > /home/pi
# ---------------------------------------------------------------------------
def _detect_pktfwd_dir() -> Path:
    """Resolve the packet forwarder directory at import time."""
    import yaml as _yaml
    # 1. From config.yaml
    try:
        with open(resolve_config_path('config.yaml')) as _f:
            _cfg = _yaml.safe_load(_f) or {}
        _pdir = _cfg.get('wm1303', {}).get('pktfwd_dir', '')
        if _pdir and Path(_pdir).is_dir():
            return Path(_pdir)
    except Exception:
        pass
    # 2. From systemd service file User=
    try:
        import subprocess as _sp
        _svc_user = _sp.check_output(
            ['systemctl', 'show', 'pymc-repeater', '-p', 'User', '--value'],
            text=True, timeout=3
        ).strip()
        if _svc_user and _svc_user != 'root':
            import pwd as _pwd
            _home = Path(_pwd.getpwnam(_svc_user).pw_dir)
            _candidate = _home / 'wm1303_pf'
            if _candidate.is_dir():
                return _candidate
    except Exception:
        pass
    # 3. Scan common user home directories
    try:
        import pwd as _pwd
        for _pw in _pwd.getpwall():
            if _pw.pw_uid >= 1000 and _pw.pw_uid < 65534 and _pw.pw_dir != '/':
                _candidate = Path(_pw.pw_dir) / 'wm1303_pf'
                if _candidate.is_dir():
                    return _candidate
    except Exception:
        pass
    # 4. Fallback to traditional path
    return Path('/home/pi/wm1303_pf')

PKTFWD_DIR     = _detect_pktfwd_dir()
PKTFWD_BIN     = PKTFWD_DIR / 'lora_pkt_fwd'
PKTFWD_RESET   = PKTFWD_DIR / 'reset_lgw.sh'
BRIDGE_CONF    = PKTFWD_DIR / 'bridge_conf.json'
ACTIVE_BRIDGE_CONF = Path('/tmp/pymc_wm1303_bridge_conf.json')
UDP_PORT_UP    = 1730
UDP_PORT_DOWN  = 1730

# Path to the UI config (SSOT for channel definitions)
UI_JSON_PATH = resolve_config_path('wm1303_ui.json')

# Fixed channel-name to IF-chain mapping.
# These NEVER change regardless of channel order or active/inactive status.
CHANNEL_IF_MAP = {
    'ch-1':    0,   # Channel A -> chan_multiSF_0
    'ch-2':    1,   # Channel B -> chan_multiSF_1
    'ch-new':  2,   # Channel D -> chan_multiSF_2
    'ch-notu': 3,   # Channel C -> chan_multiSF_3
}




def _bw_hz_to_str(bw_hz: int) -> str:
    return _queue_bw_hz_to_str(bw_hz)


def _datr_str(sf: int, bw_hz: int) -> str:
    return f'SF{sf}BW{_bw_hz_to_str(bw_hz)}'


def _parse_datr(datr):
    """Parse "SF8BW125" -> (8, 125000). Returns (None, None) for FSK or invalid."""
    if not isinstance(datr, str):
        # FSK packets have integer datr (e.g. 50000 for 50kbps bitrate)
        return None, None
    m = re.fullmatch(r'SF(\d+)BW(62(?:\.5)?|125|250|500)', datr)
    if m and 5 <= int(m.group(1)) <= 12:
        bandwidths = {'62': 62500, '62.5': 62500, '125': 125000,
                      '250': 250000, '500': 500000}
        return int(m.group(1)), bandwidths[m.group(2)]
    return None, None

def _extract_mc_payload(data: bytes) -> bytes:
    """Extract payload bytes after MeshCore header + path data.

    The payload stays constant across hop iterations, making it
    suitable for stable hashing in self-echo and dedup detection.

    MeshCore header layout:
      byte 0: [VER(2) | TYPE(4) | ROUTE(2)]
      TFLOOD/TDIRECT (route 0x00/0x03): bytes 1-4 timestamp, byte 5 path_len
      FLOOD/DIRECT   (route 0x01/0x02): byte 1 path_len
      path_len byte: bits[5:0] = hops, bits[7:6]+1 = hop_size (1-4 bytes)
      Following: hops * hop_size bytes of path data
      After that: the stable message payload
    """
    if not data or len(data) < 2:
        return data
    hdr = data[0]
    rt = hdr & 0x03
    has_tc = rt in (0x00, 0x03)  # TFLOOD or TDIRECT have timestamp
    idx = 5 if has_tc else 1     # path_raw byte offset
    if idx >= len(data):
        return data
    pl_raw = data[idx]
    hops = pl_raw & 0x3F
    hsz = ((pl_raw >> 6) & 0x03) + 1  # hop hash size: 1, 2, 3, or 4 bytes
    pbytes = hops * hsz
    payload_start = idx + 1 + pbytes
    if payload_start >= len(data):
        return data  # malformed, return all for safe hashing
    return data[payload_start:]



# Semtech reference TX gain LUT for SX1250 + SKY66420 FEM (EU868)
# 16 entries from 12 dBm to 27 dBm via RF0
DEFAULT_TX_GAIN_LUT = [
    {"rf_power": 12, "pa_gain": 0, "pwr_idx": 15},
    {"rf_power": 13, "pa_gain": 0, "pwr_idx": 16},
    {"rf_power": 14, "pa_gain": 0, "pwr_idx": 17},
    {"rf_power": 15, "pa_gain": 0, "pwr_idx": 19},
    {"rf_power": 16, "pa_gain": 0, "pwr_idx": 20},
    {"rf_power": 17, "pa_gain": 0, "pwr_idx": 22},
    {"rf_power": 18, "pa_gain": 1, "pwr_idx": 1},
    {"rf_power": 19, "pa_gain": 1, "pwr_idx": 2},
    {"rf_power": 20, "pa_gain": 1, "pwr_idx": 3},
    {"rf_power": 21, "pa_gain": 1, "pwr_idx": 4},
    {"rf_power": 22, "pa_gain": 1, "pwr_idx": 5},
    {"rf_power": 23, "pa_gain": 1, "pwr_idx": 6},
    {"rf_power": 24, "pa_gain": 1, "pwr_idx": 7},
    {"rf_power": 25, "pa_gain": 1, "pwr_idx": 9},
    {"rf_power": 26, "pa_gain": 1, "pwr_idx": 11},
    {"rf_power": 27, "pa_gain": 1, "pwr_idx": 14},
]


def _generate_bridge_conf(channels: dict[str, dict], ui_config: dict | None = None) -> dict:
    """Generate a lora_pkt_fwd global_conf.json with FIXED IF chain assignments.

    Architecture:
      - RF0 (SX1250_0): RX + TX via SKY66420 FEM, fixed IF chains 0-3
      - RF1 (SX1250_1): RX only (no PA), tx_enable=false
      - TX via PULL_RESP routed to RF0 (rfch=0)

    IF chain mapping is FIXED by channel position in the UI list.
    Center frequency covers active A-D and F channels. E has an independent
    receiver. Invalid active settings fail validation instead of silently
    disabling channels behind the UI's back.

    IF offset limit follows the SX1302 HAL constant:
      LGW_RF_RX_BANDWIDTH_125KHZ = 1 600 000 Hz  (±800 kHz from center)
      max usable IF offset = RF_RX_BW/2 − channel_BW/2
    """
    # SX1302 HAL constant: usable RF RX bandwidth for 125 kHz channels
    RF_RX_BANDWIDTH_HZ = 1_600_000  # from loragw_hal.c LGW_RF_RX_BANDWIDTH_125KHZ

    # Read ALL channel definitions from wm1303_ui.json (the SSOT)
    ui_data = ui_config
    if ui_data is None:
        if UI_JSON_PATH.exists():
            ui_data = json.loads(UI_JSON_PATH.read_text())
        else:
            # Legacy YAML-only installs retain fixed A-D positions too.
            ui_data = {'channels': [dict(channels.get(f'channel_{letter}', {}),
                                        active=channels.get(f'channel_{letter}', {}).get('active', True)
                                        and f'channel_{letter}' in channels)
                                    for letter in 'abcd']}
    if not isinstance(ui_data, dict):
        raise ValueError('Radio settings must be an object')
    all_ui_channels = ui_data.get('channels', [])
    if (not isinstance(all_ui_channels, list) or len(all_ui_channels) > 4
            or any(not isinstance(ch, dict) for ch in all_ui_channels)):
        raise ValueError('channels must contain at most four A-D objects')
    _che_d = ui_data.get('channel_e', {})
    _chf_d = ui_data.get('channel_f', {})
    if not isinstance(_che_d, dict) or not isinstance(_chf_d, dict):
        raise ValueError('channel_e and channel_f must be objects')
    # Validate before truth-testing flags or truncating numeric values. This
    # also covers file-loaded settings that never passed through the web API.
    configured_channels = [(f'Channel {chr(65 + idx)}', ch)
                           for idx, ch in enumerate(all_ui_channels)]
    configured_channels.extend((('Channel E', _che_d), ('Channel F', _chf_d)))
    for label, ch in configured_channels:
        for field in ('active', 'enabled', 'tx_enabled', 'tx_enable',
                      'lbt_enabled', 'cad_enabled', 'boosted_rx'):
            if field in ch and not isinstance(ch[field], bool):
                raise ValueError(f'{label}: {field} must be a boolean')
        for field in ('frequency', 'bandwidth', 'spreading_factor',
                      'preamble_length', 'tx_power', 'lbt_threshold', 'lbt_rssi_target'):
            if field not in ch:
                continue
            value = ch[field]
            try:
                if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                    raise ValueError
                integer = int(value)
                if isinstance(value, float) and value != integer:
                    raise ValueError
            except (ValueError, TypeError, OverflowError) as exc:
                raise ValueError(f'{label}: {field} must be an integer') from exc
    active_channels = [(f'Channel {chr(65 + idx)}', ch, (125000,))
                       for idx, ch in enumerate(all_ui_channels) if ch.get('active', False)]
    if _che_d.get('enabled', False):
        active_channels.append(('Channel E', _che_d, (62500, 125000, 250000, 500000)))
    if _chf_d.get('enabled', False):
        active_channels.append(('Channel F', _chf_d, (125000, 250000, 500000)))
    for label, ch, bandwidths in active_channels:
        try:
            if int(ch.get('frequency', 0)) <= 0:
                raise ValueError('frequency must be positive Hz')
            if int(ch.get('bandwidth', 125000)) not in bandwidths:
                raise ValueError(f'bandwidth must be one of {bandwidths} Hz')
            if not 5 <= int(ch.get('spreading_factor', 8)) <= 12:
                raise ValueError('spreading_factor must be 5..12')
            if label == 'Channel E' and int(ch.get('spreading_factor', 8)) < 7:
                raise ValueError('SX1261 Channel E supports spreading_factor 7..12')
            cr = str(ch.get('coding_rate', '4/5'))
            if cr not in ('4/5', '4/6', '4/7', '4/8', '1', '2', '3', '4', '5', '6', '7', '8'):
                raise ValueError('coding_rate must be 4/5, 4/6, 4/7 or 4/8')
            if not 6 <= int(ch.get('preamble_length', 17)) <= 65535:
                raise ValueError('preamble_length must be 6..65535')
            if not -128 <= int(ch.get('lbt_threshold', ch.get('lbt_rssi_target', -80))) <= 127:
                raise ValueError('LBT threshold must be -128..127 dBm')
        except (ValueError, TypeError) as exc:
            raise ValueError(f'{label}: {exc}') from exc
    manual_center_hz = 0  # Manual RF center override from UI (MHz -> Hz)
    region_code = _REGION_DEFAULT  # Regulatory region (default EU868)
    region_custom_min = None       # Optional CUSTOM region tx_freq_min (Hz)
    region_custom_max = None       # Optional CUSTOM region tx_freq_max (Hz)
    # Physical-layer sync word, shared by SX1302 and SX1261. Standard
    # MeshCore uses private (0x12, represented as 0x1424 on SX126x).
    _sync_word_value = 0x1424
    _sync_word_mode = 'private'
    try:
        if ui_data:
            # Check for manually-set RF center frequency (saved by UI)
            _rf_mhz = ui_data.get('rf_center_freq_mhz', 0)
            if _rf_mhz:
                manual_center_hz = int(float(_rf_mhz) * 1_000_000)
            # Regulatory region (EU868, US915, AU915, AS923, IN865, JP920, KR920, CUSTOM)
            _region_field = ui_data.get('region')
            if isinstance(_region_field, dict):
                # Allow region as object: {'code': 'CUSTOM', 'tx_freq_min': 915000000, 'tx_freq_max': 928000000}
                region_code = str(_region_field.get('code', _REGION_DEFAULT)).upper()
                region_custom_min = _region_field.get('tx_freq_min')
                region_custom_max = _region_field.get('tx_freq_max')
            elif isinstance(_region_field, str) and _region_field:
                region_code = _region_field.upper()
            # Device-wide sync_word (top-level; NOT per-channel).
            _sw_field = ui_data.get('sync_word')
            if isinstance(_sw_field, dict):
                try:
                    _sync_word_value = int(_sw_field.get('value', 0x1424)) & 0xFFFF
                except (TypeError, ValueError):
                    _sync_word_value = 0x1424
                _sync_word_mode = str(_sw_field.get('mode', '')).lower() or 'private'
            elif isinstance(_sw_field, int):
                _sync_word_value = int(_sw_field) & 0xFFFF
                _sync_word_mode = ''  # derive below
            # Normalize: only Private/Public are hardware-valid. Anything else
            # (including legacy 'custom' mode from older UI builds) falls back
            # to Private silently.
            if _sync_word_value == 0x3444:
                _sync_word_mode = 'public'
            else:
                _sync_word_value = 0x1424
                _sync_word_mode = 'private'
    except (ValueError, TypeError, OverflowError) as ex:
        raise ValueError(f'Invalid RF center, region or sync-word setting: {ex}') from ex

    _lorawan_public = _sync_word_mode == 'public'
    logger.info(
        '_generate_bridge_conf: sync_word=0x%04X mode=%s lorawan_public=%s',
        _sync_word_value, _sync_word_mode, _lorawan_public,
    )

    # ---- Determine RF center frequency ----
    # Priority order:
    #   1. manual_center_hz (from rf_center_freq_mhz in wm1303_ui.json)
    #   2. Auto-calculate from active channel frequencies
    #   3. Region-based default (from region_config.py)
    #   4. Fallback to EU868 center (869525000 Hz)
    # This ensures the service starts even when ALL channels are disabled
    # (e.g. fresh install where user has not configured channels yet).

    active_freqs = []  # track for logging
    auto_center = 0

    # Collect active frequencies for auto-center calculation
    if all_ui_channels:
        # Compute center from ACTIVE channels only — inactive channels don't
        # need IF chain slots and should not shift the center frequency.
        active_freqs = [int(ch.get('frequency', 0))
                        for ch in all_ui_channels
                        if ch.get('active', False) and ch.get('frequency', 0)]
    # F shares RF0 with A-D; E receives independently on the SX1261.
    if _chf_d.get('enabled', False):
        active_freqs.append(int(_chf_d['frequency']))

    if active_freqs:
        rf_channels = [(ch, 125000) for ch in all_ui_channels if ch.get('active', False)]
        if _chf_d.get('enabled', False):
            rf_channels.append((_chf_d, int(_chf_d.get('bandwidth', 250000))))
        # Intersect each channel's usable RF-center range. A midpoint of the
        # feasible interval also works with asymmetric channel bandwidths.
        lower = max(int(ch['frequency']) - (RF_RX_BANDWIDTH_HZ // 2 - bw // 2 - 7500)
                    for ch, bw in rf_channels)
        upper = min(int(ch['frequency']) + (RF_RX_BANDWIDTH_HZ // 2 - bw // 2 - 7500)
                    for ch, bw in rf_channels)
        if lower > upper:
            raise ValueError('Active A-D/F frequencies do not fit within the concentrator RF bandwidth')
        auto_center = (lower + upper) // 2
        logger.info('_generate_bridge_conf: auto_center=%d Hz from %d active channel(s): %s',
                    auto_center, len(active_freqs), active_freqs)

    # Determine final center frequency
    if manual_center_hz:
        center = manual_center_hz
        logger.info('_generate_bridge_conf: center=%d Hz (MANUAL from rf_center_freq_mhz=%.3f MHz)',
                    center, manual_center_hz / 1_000_000)
    elif auto_center:
        center = auto_center
        logger.info('_generate_bridge_conf: center=%d Hz (AUTO from %d active channels)',
                    center, len(active_freqs))
    else:
        # No channels active AND no manual center set.
        # Use region-based default center frequency so the service can start.
        _region_defaults = {
            'EU868': 869_525_000, 'US915': 915_500_000,
            'AU915': 915_275_000, 'AS923': 923_200_000,
            'IN865': 866_000_000, 'JP920': 923_200_000,
            'KR920': 922_100_000,
        }
        center = _region_defaults.get(region_code, 869_525_000)
        logger.warning(
            '_generate_bridge_conf: no active channels and no rf_center_freq_mhz set. '
            'Using region %s default center=%d Hz. '
            'Radio will idle until channels are configured via the UI.',
            region_code, center,
        )

    # Validate channel frequencies against SX1302 IF range.
    # max_if_offset = (RF_RX_BW / 2) − (channel_BW / 2)
    # For 125 kHz BW: (1600000/2) − (125000/2) = 737500 Hz
    # We use a small safety margin → 730 kHz.
    for ch in (all_ui_channels or []):
        if not ch.get('active', False):
            continue
        f = int(ch.get('frequency', 0))
        bw = int(ch.get('bandwidth', 125000))
        max_if_offset = (RF_RX_BANDWIDTH_HZ // 2) - (bw // 2) - 7500  # 7.5 kHz margin
        if f and abs(f - center) > max_if_offset:
            ch_name = ch.get('name', ch.get('friendly_name', '?'))
            raise ValueError(f'Channel {ch_name} is outside the RF center range; adjust frequencies or RF center')

    # --- HAL-level LBT: DYNAMICALLY GENERATED from UI config ---
    # HAL LBT (AGC-based sx1261_lbt_start) is the PRIMARY LBT mechanism.
    # Per-channel enable/disable and threshold come from wm1303_ui.json.
    # AGC handshake verified working on WM1303 (tested on a test unit).
    #
    # IMPORTANT: When HAL LBT is enabled (lbt.enable=true), the HAL runs LBT
    # on EVERY TX. Any TX frequency NOT listed in lbt.channels[] is rejected
    # with "Cannot start LBT - wrong channel". Therefore we must include ALL
    # active TX frequencies in lbt.channels[]. Channels where the user did
    # NOT enable LBT get a permit-all threshold (+127 dBm, unreachable in
    # practice → LBT always passes, effectively equivalent to no LBT).
    # Channels where the user DID enable LBT get the user-configured threshold.
    #
    # Decision: HAL LBT is only enabled if AT LEAST ONE channel has
    # lbt_enabled=true. If no channels want LBT, lbt.enable is false and
    # the HAL skips LBT entirely (fastest path).
    #
    # The Python software pre-filter was removed; HAL LBT is now the sole
    # RSSI-based TX gate (per-channel rssi_target_dbm, inside lgw_send).
    _lbt_channels = []
    _lbt_seen = set()  # deduplicate by (freq, bw)
    _lbt_thresholds = []  # collect user-enabled thresholds for global fallback
    _lbt_any_enabled = False  # true if at least one channel has lbt_enabled=true

    # A-D, E and F all transmit through the same HAL. Include every active
    # TX channel, since an unlisted frequency is rejected when LBT is enabled.
    tx_channels = [ch for _, ch, _ in active_channels
                   if ch.get('tx_enabled', ch.get('tx_enable', True))]
    _lbt_any_enabled = any(ch.get('lbt_enabled', False) for ch in tx_channels)
    for ch in tx_channels:
        ch_freq = int(ch['frequency'])
        ch_bw = int(ch.get('bandwidth', 125000))
        if _lbt_any_enabled and ch_bw not in (62500, 125000, 250000):
            raise ValueError('HAL LBT cannot be combined with an enabled 500 kHz TX channel')
        ch_lbt_on = bool(ch.get('lbt_enabled', False))
        ch_threshold = int(ch.get('lbt_threshold', ch.get('lbt_rssi_target', -80))) if ch_lbt_on else 127
        if ch_lbt_on:
            _lbt_thresholds.append(ch_threshold)
        lbt_key = (ch_freq, ch_bw)
        if lbt_key in _lbt_seen:
            # Two logical channels may share a frequency/BW. Preserve the
            # stricter setting regardless of which one appears first.
            entry = next(c for c in _lbt_channels if (c['freq_hz'], c['bandwidth']) == lbt_key)
            entry['enable'] = entry['enable'] or ch_lbt_on
            entry['rssi_target'] = min(entry['rssi_target'], ch_threshold)
            continue
        _lbt_channels.append({
            'enable': ch_lbt_on,
            'freq_hz': ch_freq,
            'bandwidth': ch_bw,
            'scan_time_us': 128,
            'transmit_time_ms': 4000,
            'rssi_target': ch_threshold,
        })
        _lbt_seen.add(lbt_key)
    # HAL LBT enabled only if at least one channel has user-requested LBT
    _lbt_enabled = _lbt_any_enabled
    # Global fallback threshold: min of user-enabled (most restrictive), or -80
    _lbt_rssi_target = min(_lbt_thresholds) if _lbt_thresholds else -80

    if _lbt_enabled:
        logger.info('_generate_bridge_conf: HAL LBT ENABLED with %d channels '
                   '(thresholds: %s, global fallback: %d dBm; '
                   'permit-all=127 dBm for channels without lbt_enabled)',
                   len(_lbt_channels),
                   [(c['freq_hz'], c['rssi_target']) for c in _lbt_channels],
                   _lbt_rssi_target)
    else:
        # Clear list so lbt.nb_channel is 0 and HAL does not run LBT
        _lbt_channels = []
        logger.info('_generate_bridge_conf: HAL LBT DISABLED '
                   '(no channels with lbt_enabled=true)')
    # The forwarder reports HAL LBT results in post-TX ACKs. A missing/null
    # result means no confirmed measurement, not a clear or busy channel.

    # --- SX1261: compute spectral_scan dynamically from channel config ---
    # Cover the full RF RX bandwidth: center ± 800kHz
    # Round freq_start down to 200kHz grid, freq_stop up to 200kHz grid
    _scan_start = ((center - 800000) // 200000) * 200000
    _scan_stop = ((center + 800000 + 199999) // 200000) * 200000
    _nb_chan = int((_scan_stop - _scan_start) // 200000)
    # Spectral scan is DISABLED by default (v2.3.2+).
    # Rationale: all operational noise-floor data is already available from:
    #   (a) HAL LBT real-time RSSI reads per TX (primary per-channel measurement)
    #   (b) RX-derived noise floor: rssi - snr from received packets
    #   (c) LBT RSSI samples aggregated into per-channel rolling buffers
    # The sweep added SX1261 state-machine complexity (mode switches between RX,
    # scan, LBT) and hardware-level reliability issues (register 0x0401 accumulator
    # corruption, sweep-thread stalls) without providing data that other sources
    # don't already cover. Disabling the sweep gives the SX1261 a simpler, more
    # stable role: Channel E RX + per-TX LBT only. See release notes v2.3.2 and
    # TODO item for future complete removal (Option B).
    # The UI can enable sweeps explicitly; ordinary operation leaves them off.
    _spectral_scan_conf = {
        'enable': bool(ui_data.get('spectral_scan', {}).get('enabled', False)),
        'freq_start': _scan_start,
        'nb_chan': _nb_chan,  # typically 8-9 channels for 1.6MHz BW
        'nb_scan': 100,
        'pace_s': 300,
    }
    logger.info('_generate_bridge_conf: spectral_scan enabled=%s '
                'freq_start=%d, nb_chan=%d, freq_stop=%d, center=%d',
                _spectral_scan_conf['enable'], _scan_start, _nb_chan, _scan_stop, center)

    # Build chan_multiSF_0 through chan_multiSF_7 with FIXED IF chain mapping
    chan_configs = {}

    if all_ui_channels:
        # INDEX-BASED mapping: channel position in UI list -> IF chain index
        # This is independent of channel names (which can be renamed in UI)
        for idx, ch in enumerate(all_ui_channels):
            if idx > 7:  # max 8 IF chains (chan_multiSF_0 to chan_multiSF_7)
                logger.warning('_generate_bridge_conf: more than 8 channels, '
                              'ignoring channel %d+', idx)
                break
            if_idx = idx  # Position-based: first UI channel -> IF chain 0, etc.
            ch_name = ch.get('name', '')
            ch_freq = int(ch.get('frequency', 0))
            if_offset = ch_freq - center
            is_active = ch.get('active', False)
            chan_configs[f'chan_multiSF_{if_idx}'] = {
                'enable': is_active, 'radio': 0, 'if': if_offset
            }
            logger.info('_generate_bridge_conf: %s (idx=%d) -> chan_multiSF_%d: '
                       'enable=%s, if=%+d',
                       ch_name, idx, if_idx, is_active, if_offset)
    else:
        # No UI channels configured (fresh install or all channels removed).
        # Do NOT enable any chan_multiSF slots — user must configure channels
        # via the WM1303 Manager UI first. The radio will idle on the center
        # frequency until channels are activated.
        logger.info('_generate_bridge_conf: no UI channels configured, '
                    'all chan_multiSF slots will be disabled. '
                    'Configure channels via the WM1303 Manager UI.')

    # Ensure all 8 chan_multiSF slots exist; unused slots are DISABLED
    # to prevent the concentrator from receiving duplicate packets on
    # the center frequency (if=0).  Only channels explicitly defined
    # in the UI channel list are enabled.
    for i in range(8):
        key = f'chan_multiSF_{i}'
        if key not in chan_configs:
            chan_configs[key] = {'enable': False, 'radio': 0, 'if': 0}

    # E uses the independent SX1261 receiver; F uses RF0's standard demodulator.
    _che_cr = str(_che_d.get('coding_rate', '4/5'))
    _che_cr = int(_che_cr.split('/')[-1])
    if _che_cr > 4:
        _che_cr -= 4
    _che_lora_rx = {
        'enable': bool(_che_d.get('enabled', False)),
        'freq_hz': int(_che_d.get('frequency', 869618000)),
        'bandwidth': int(_che_d.get('bandwidth', 62500)),
        'spreading_factor': int(_che_d.get('spreading_factor', 8)),
        'coding_rate': _che_cr,
        'boosted': bool(_che_d.get('boosted_rx', True)),
        'sync_word': _sync_word_value,
    }
    _chf_enabled = bool(_chf_d.get('enabled', False))
    _chf_freq_hz = int(_chf_d.get('frequency', 869525000))
    _chf_bw_hz = int(_chf_d.get('bandwidth', 250000))
    _chf_sf = int(_chf_d.get('spreading_factor', 9))
    if _chf_enabled:
        max_if = RF_RX_BANDWIDTH_HZ // 2 - _chf_bw_hz // 2 - 7500
        if abs(_chf_freq_hz - center) > max_if:
            raise ValueError('Channel F is outside the RF center range; adjust frequencies or RF center')

    _agc_reload_interval_s = int(ui_data.get('hal_advanced', {}).get('agc_reload_interval_s', 300))
    # Resolve regulatory TX frequency bounds based on region (Issue #4)
    # Supports EU868, US915, AU915, AS923, IN865, JP920, KR920, and CUSTOM.
    # Falls back to EU868 bounds when region_config module is unavailable.
    if _REGION_AVAILABLE:
        _tx_freq_min, _tx_freq_max = _region_get_tx_bounds(
            region_code,
            custom_min=region_custom_min,
            custom_max=region_custom_max,
            fallback_channels=active_freqs,
        )
        logger.info(
            '_generate_bridge_conf: region=%s tx_freq_min=%d Hz tx_freq_max=%d Hz',
            region_code, _tx_freq_min, _tx_freq_max,
        )
    else:
        # Backwards-compatible fallback: EU868
        _tx_freq_min, _tx_freq_max = 863_000_000, 870_000_000
        logger.warning(
            '_generate_bridge_conf: region_config not available, '
            'using EU868 fallback tx_freq bounds'
        )

    conf = {
        'SX130x_conf': {
            'com_type': 'SPI',
            'com_path': ui_data.get('spi_devices', {}).get('sx1302_spi_path', '/dev/spidev0.0'),
            # Board-level LoRa sync word selector (HAL v2.10 lgw_conf_board_t).
            # Derived from device-wide sync_word (top-level wm1303_ui.json).
            'lorawan_public': bool(_lorawan_public),
            'clksrc': 0,
            'antenna_gain': 0,
            'full_duplex': False,
            'agc_reload_interval_s': _agc_reload_interval_s,
            'precision_timestamp': {
                'enable': True,
                'max_ts_metrics': 255,
                'nb_symbols': 1,
            },
            # RF0 = RX + TX via SKY66420 FEM (PA + LNA + RF Switch)
            'radio_0': {
                'enable': True,
                'type': 'SX1250',
                'freq': center,
                'rssi_offset': -215.4,
                'rssi_tcomp': {'coeff_a': 0, 'coeff_b': 0, 'coeff_c': 20.41,
                               'coeff_d': 2162.56, 'coeff_e': 0},
                'tx_enable': True,
                'tx_freq_min': _tx_freq_min,
                'tx_freq_max': _tx_freq_max,
                'tx_gain_lut': DEFAULT_TX_GAIN_LUT,
            },
            # RF1 = RX only (no PA, SAW filter path)
            'radio_1': {
                'enable': True,
                'type': 'SX1250',
                'freq': center,
                'rssi_offset': -215.4,
                'rssi_tcomp': {'coeff_a': 0, 'coeff_b': 0, 'coeff_c': 20.41,
                               'coeff_d': 2162.56, 'coeff_e': 0},
                'tx_enable': False,
            },
            **chan_configs,
            # Channel F: chan_Lora_std for BW125/250/500 single-SF on RF0.
            # Runs in PARALLEL with chan_multiSF_0-3 (channels A-D). When enabled,
            # uses the if-offset derived from channel_f.frequency relative to center.
            # sync_word here is the DEVICE-WIDE value (top-level in wm1303_ui.json).
            # NOTE: HAL v2.10 applies the board-level lorawan_public flag to all
            # IF chains including chan_Lora_std; the explicit 'sync_word' field
            # below is reserved for a future HAL extension and is currently
            # ignored by HAL v2.10. With Custom removed from the UI, this value
            # is always 0x1424 (Private) or 0x3444 (Public), matching the board
            # flag, so no divergence is possible.
            'chan_Lora_std':  {
                'enable': bool(_chf_enabled),
                'radio': 0,
                'if': int(_chf_freq_hz - center) if _chf_enabled else 0,
                'bandwidth': int(_chf_bw_hz),
                'spread_factor': int(_chf_sf),
                'sync_word': int(_sync_word_value) & 0xFFFF,
            },
            'chan_FSK':       {'enable': False, 'radio': 0, 'if': 0,
                              'bandwidth': 125000, 'datarate': 50000},
            # SX1261 companion chip for LBT (Listen Before Talk)
            'sx1261_conf': {
                'spi_path': ui_data.get('spi_devices', {}).get('sx1261_spi_path', '/dev/spidev0.1'),
                'rssi_offset': 0,
                'lora_rx': _che_lora_rx,
                'spectral_scan': _spectral_scan_conf,
                'lbt': {
                    'enable': _lbt_enabled,
                    'rssi_target': _lbt_rssi_target,
                    'nb_channel': len(_lbt_channels),
                    'channels': _lbt_channels,
                },
                'custom_lbt': (lambda _clbt_channels: {
                    'enable': False,  # Custom LBT disabled - HAL LBT is primary now
                    'cad_enable': True,  # CAD is MANDATORY before every TX — always enabled
                    'rssi_target': _lbt_rssi_target,
                    'channels': _clbt_channels,
                })([
                    {
                        'freq_hz': int(ch.get('frequency', 0)),
                        'lbt_enabled': False,  # Force off - HAL LBT replaces custom LBT
                        'cad_enabled': bool(ch.get('cad_enabled', False)),
                    }
                    for ch in (all_ui_channels or [])
                    if ch.get('active', False) and int(ch.get('frequency', 0)) > 0
                ] + ([
                    {
                        'freq_hz': int(_che_lora_rx.get('freq_hz', 0)),
                        'lbt_enabled': False,  # Force off - HAL LBT replaces custom LBT
                        'cad_enabled': bool(_che_d.get('cad_enabled', False)),
                    }
                ] if _che_lora_rx.get('enable', False) and int(_che_lora_rx.get('freq_hz', 0)) > 0 else []) + ([
                    {
                        'freq_hz': _chf_freq_hz,
                        'lbt_enabled': False,
                        'cad_enabled': bool(_chf_d.get('cad_enabled', False)),
                    }
                ] if _chf_enabled else [])),
            },
        },
        'gateway_conf': {
            'gateway_ID': 'AA555A0000000000',
            'server_address': '127.0.0.1',
            'serv_port_up':   UDP_PORT_UP,
            'serv_port_down': UDP_PORT_DOWN,
            'keepalive_interval': 10,
            'stat_interval': 30,
            'push_timeout_ms': 100,
            'forward_crc_valid': True,
            'forward_crc_error': True,
            'forward_crc_disabled': True,
        },
    }

    # ── CAPTURE_RAM streaming config (BW62.5 decoder) ──────────────
    # Try loading from PKTFWD_DIR/capture_conf.json first
    _cap_path = str(PKTFWD_DIR / 'capture_conf.json')
    try:
        import json as _json
        with open(_cap_path) as _f:
            _cap_raw = _json.load(_f)
        # Support both flat and nested format
        _cap = _cap_raw.get("capture_conf", _cap_raw)
        if _cap.get("enable", False):
            conf["capture_conf"] = {
                "enable": True,
                "source": _cap.get("source", 2),
                "period": _cap.get("period", 511),
            }
            # Pass through optional capture fields
            for _opt_key in ("udp_port", "num_phases", "phase_delay_ms"):
                if _opt_key in _cap:
                    conf["capture_conf"][_opt_key] = _cap[_opt_key]
            logger.info("_generate_bridge_conf: added capture_conf from %s (source=%d, period=%d)",
                        _cap_path, conf["capture_conf"]["source"], conf["capture_conf"]["period"])
    except Exception as _ex:
        logger.debug("_generate_bridge_conf: no capture_conf.json: %s", _ex)


    return conf


# Module-level reference for direct API access (replaces gc.get_referrers)
_active_backend = None  # type: WM1303Backend | None

class WM1303Backend:
    """WM1303/SX1303 concentrator backend using lora_pkt_fwd.

    RF0-TX Architecture:
    - RF0 (SX1250_0): RX + TX via SKY66420 FEM (PA + LNA + RF Switch)
    - RF1 (SX1250_1): RX only (no PA)
    - IF chains 0-2 on RF0
    - TX via direct PULL_RESP to running pkt_fwd (no stop/start needed)
    - Self-echo detection discards own TX heard back on RX
    """

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self._storage_dir = Path(config.get('storage', {}).get('storage_dir')
                                 or config.get('storage_dir') or '/var/lib/openhop_repeater').expanduser()
        self._db_path = str(self._storage_dir / 'repeater.db')
        self.virtual_radios: dict[str, Any] = {}
        self.channels: dict[str, dict] = {}
        self._proc: subprocess.Popen | None = None
        self._pktfwd_pgid: int | None = None
        self._active_pktfwd_config: str | None = None
        self._sock: socket.socket | None = None
        self._sock_lock = threading.Lock()  # thread-safe socket recreation
        self._pull_addr: tuple | None = None
        self._running = False
        self._stop_event = threading.Event()
        self._pktfwd_lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stdout_thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._tx_lock = asyncio.Lock()
        self._last_tx_end: float = 0.0  # monotonic time when last TX completes

        # TX stats
        self._tx_packets_sent_total = 0

        # Self-echo detection: store hashes of recently transmitted packets
        self._tx_echo_hashes: dict[str, float] = {}  # packet_hash -> confirmed TX time
        self._tx_echo_ttl = 30.0  # seconds to keep TX hashes
        self._tx_echo_detected = 0
        self._tx_mesh_echo_detected = 0  # neighbor-repeater retransmissions of our packets
        self._tx_unknown_echo_detected = 0  # echo matched on hash but path/src didn't confirm origin
        # Local identity for path-based echo classification (set by repeater main on startup)
        # _local_pub_key: full bytes; _local_path_hash_hex: hex string of the first N bytes
        # matching the configured path_hash_size (1, 2, or 3 bytes).
        self._local_pub_key: bytes | None = None
        self._local_path_hash_hex: str | None = None
        self._local_path_hash_size: int = 1
        self._rx_dedup_cache: dict = {}  # multi-demod dedup  # counter for stats

        # Dispatcher RX callback (set by Dispatcher via set_rx_callback)
        self.rx_callback = None
        self._last_pkt_rssi: int = -120
        self._last_pkt_snr: float = 0.0

        # AGC Recovery: reset concentrator after TX to restore RX sensitivity
        self._agc_reset_timer = None
        self._pktfwd_ready_event = threading.Event()  # threading.Timer or None
        self._agc_reset_lock = threading.Lock()
        self._agc_reset_delay = 0.3  # seconds after last TX before reset
        self._agc_resets = 0  # counter for stats
        self._agc_resetting = False  # flag to prevent concurrent resets
        self._current_burst_tx_time = 0.0  # cumulative TX time for current burst

        # TX batch hold: after RX, delay TX dynamically based on queue depth
        self._tx_hold_until = 0.0  # monotonic timestamp; TX held until this time
        # Dynamic hold thresholds (seconds) — design principle: TX ASAP!
        #   0 packets pending → 0ms  (nothing to send)
        #   1 packet  pending → 50ms  (brief dedup window)
        #   2+ packets pending → 0ms  (batch ready, send NOW!)
        self._tx_hold_empty = 0.0       # hold when no packets pending
        self._tx_hold_single = 0.050    # hold when 1 packet pending (brief dedup window)

        # Per-channel TX queues (managed internally)
        self._tx_queue_manager: TXQueueManager | None = None
        self._global_tx_scheduler: GlobalTXScheduler | None = None
        self._runtime_radio_config: dict | None = None

        # Per-channel RX statistics (updated in _dispatch_rx)
        self._channel_rx_stats: dict[str, dict] = {}

        # Per-channel TX timing statistics (updated after each TX)
        self._channel_tx_stats: dict[str, dict] = {}
        self._start_time = time.time()  # for duty cycle calculation

        # Store module-level reference for API access
        global _active_backend
        _active_backend = self

        # Software LBT: config cache with TTL
        self._lbt_config_cache = {}
        self._lbt_config_cache_time = 0
        self._lbt_config_cache_ttl = 5.0  # seconds
        
        # Per-channel noise floor monitor
        self._nf_monitor_thread: threading.Thread | None = None
        self._nf_monitor_running = False
        self._channel_noise_floors: dict[str, float] = {}  # ui_config_name -> noise_floor_dbm
        self._nf_lock = threading.Lock()  # protects _channel_noise_floors and _freq_to_ui_name
        self._freq_to_ui_name: dict[int, str] = {}  # freq_hz -> ui_config_name (e.g. 869461000 -> 'SF8')
        self._ch_id_to_ui_name: dict[str, str] = {}  # channel_a -> ui_config_name (supports same-freq channels)
        self._ui_name_to_ch_id: dict[str, str] = {}  # reverse: ui_config_name -> channel_a
        self._freq_to_ch_id: dict[int, str] = {}  # freq_hz -> channel_id (e.g. 869461000 -> 'channel_a')
        self._nf_interval = 30  # seconds between noise floor updates

        # RX-based noise floor estimation (fallback when spectral scan and LBT unavailable)
        # Stores per-channel_id deques of (timestamp, nf_estimate) tuples
        self._rx_nf_estimates: dict[str, collections.deque] = {}
        self._rx_nf_lock = threading.Lock()
        self._rx_nf_max_age = 300.0  # seconds to keep RX NF estimates
        self._rx_nf_max_samples = 100  # max samples per channel

        # CAD config cache
        self._cad_config_cache: dict[str, bool] = {}  # channel_id -> cad_enabled
        self._cad_config_cache_time: float = 0

        # WM1303 post-TX TX_ACK correlation (Approach 3 hybrid):
        # C code emits a second TX_ACK after lgw_send() with phase="post_tx"
        # carrying CAD/LBT results. We correlate by token — _send_pull_resp
        # registers a Future before sendto(), and the _handle_udp reader
        # thread resolves it when the post-TX ACK arrives.
        # Cache is kept for late arrivals and diagnostic lookups (30s TTL).
        self._pending_tx_acks: dict[int, asyncio.Future] = {}
        self._tx_ack_cache: collections.OrderedDict = collections.OrderedDict()
        self._tx_ack_cache_ttl = 30.0  # seconds
        self._tx_ack_cache_max = 512
        self._tx_ack_lock = threading.Lock()



        # SX1261 for LBT/CAD only (not TX)
        self._sx1261 = None
        self._sx1261_available = False

        # Channel E config cache (avoid reading JSON on every RX packet)
        self._channel_e_freq_cache: int = 0
        self._channel_e_config_cache: dict = {}
        self._channel_e_cache_time: float = 0
        self._channel_e_cache_ttl: float = 5.0  # seconds

        # Channel F config cache (chan_Lora_std on RF0): freq + bw + sf for RX matching
        self._channel_f_enabled_cache: bool = False
        self._channel_f_freq_cache: int = 0
        self._channel_f_bw_cache: int = 0  # Hz (125000/250000/500000)
        self._channel_f_sf_cache: int = 0
        self._channel_f_config_cache: dict = {}
        self._channel_f_cache_time: float = 0
        self._channel_f_cache_ttl: float = 5.0  # seconds

        # RX Watchdog: auto-restart pkt_fwd when concentrator stops receiving
        self._last_rx_timestamp = time.monotonic()
        self._watchdog_timeout = 9999  # 3 minutes without RX triggers restart
        self._watchdog_thread: threading.Thread | None = None
        self._watchdog_running = False

        # pkt_fwd subprocess respawn tracking (Detection Method 4: process exit)
        # When C-level L2 watchdog triggers exit_sig, pkt_fwd exits cleanly but
        # the Python backend must respawn it. Rate-limited to prevent crash loops.
        self._respawn_times: list[float] = []  # monotonic timestamps of recent respawns
        self._respawn_max_per_hour = 30        # hard cap to prevent infinite crash loops
        self._respawn_total = 0                # cumulative count since backend start
        self._consecutive_ack_timeouts = 0      # Layer 2: consecutive post-TX ACK timeouts
        self._consecutive_crash_count = 0          # Layer 2 SX1261 escalation: consecutive pkt_fwd crashes
        self._sx1261_escalation_threshold = 2       # after N consecutive crashes, use extended power-off
        self._l2_ack_timeout_threshold = 3      # trigger restart after N consecutive timeouts

        # Issue #11.1: auto-escalate non-crash watchdog restarts to deep_reset
        # when they recur back-to-back. Covers hardware-level stalls where
        # pkt_fwd does NOT crash but lgw_receive() returns no packets for
        # extended periods (rssi_monitor / stat_monitor / timeout triggers).
        # Without this, repeated standard power_cycles can leave the SX1261
        # latched and only the extended deep_reset (>=10s drain) clears it.
        self._consecutive_watchdog_restarts = 0
        self._watchdog_escalation_threshold = 2  # after N consecutive non-crash restarts, escalate


        # Early detection: PUSH_DATA stat monitoring (Detection Method 1)
        self._zero_rx_stat_count = 0   # consecutive stat windows with rxnb=0
        self._last_stat_rxnb = -1      # last rxnb value from PUSH_DATA stats
        self._last_stat_txnb = 0       # last txnb value from PUSH_DATA stats

        # Early detection: Channel E RSSI vs SX1302 RX comparison (Detection Method 2)
        self._rssi_spike_count = 0             # strong RSSI detections by Channel E
        self._rssi_spike_window_start = time.monotonic()  # window start for spike counting
        # Hourly summary counters (reset every 60 min)
        self._hourly_rx_total = 0
        self._hourly_rx_crc_ok = 0
        self._hourly_rx_crc_err = 0
        self._hourly_tx_ok = 0
        self._hourly_tx_fail = 0
        self._hourly_tx_lbt_block = 0
        self._hourly_fsk_skipped = 0
        self._hourly_scan_sweeps = 0
        self._hourly_timer: threading.Timer | None = None
        self._hourly_lock = threading.Lock()

        # Per-channel CRC error rate counters (read+reset by external recorder every 60s)
        self._crc_rate_lock = threading.Lock()
        self._crc_rate_counters: dict[str, dict[str, int]] = {}  # {channel_id: {"crc_error": N, "crc_disabled": N}}



    def set_local_identity(self, pub_key: bytes, path_hash_size: int = 1) -> None:
        """Register the local repeater identity for path-based echo classification.

        The path-hash that this repeater appends to a packet when it forwards
        it is the first ``path_hash_size`` bytes of its public key (uppercase
        hex). We use this to distinguish:

        * ``self_echo``    - own TX heard back via RF coupling (our path_hash
          is the LAST entry in the packet's path, OR the packet was
          originated by us and the path is empty).
        * ``mesh_echo``    - a neighbour repeater retransmitted a packet that
          we had previously transmitted (our path_hash is in the path but
          not last, OR our identity is the source and the path is non-empty).
        * ``unknown_echo`` - content hash matched a recent TX but neither the
          path nor the source confirmed our involvement (rare collision).
        """
        if not pub_key:
            return
        try:
            sz = int(path_hash_size)
        except Exception:
            sz = 1
        if sz not in (1, 2, 3):
            sz = 1
        self._local_pub_key = bytes(pub_key)
        self._local_path_hash_size = sz
        self._local_path_hash_hex = self._local_pub_key[:sz].hex().upper()
        logger.info('WM1303Backend: local identity registered for echo classification: '
                    'path_hash=%s (size=%d byte%s)',
                    self._local_path_hash_hex, sz, 's' if sz != 1 else '')

    def _classify_echo(self, payload: bytes) -> str:
        """Classify a content-hash-matched echo as 'self_echo', 'mesh_echo', or 'unknown_echo'.

        The classification is **path-based** rather than time-based, so the
        decision is independent of mesh latency and works correctly on any
        repeater because each one has its own unique identity hash.

        Falls back to 'unknown_echo' when the local identity is not registered
        yet or when the packet is too short to parse the path.
        """
        if not self._local_pub_key or not payload or len(payload) < 2:
            return 'unknown_echo'
        try:
            hdr = payload[0]
            rt = hdr & 0x03
            pt = (hdr >> 2) & 0x0F
            has_tc = rt in (0x00, 0x03)  # TFLOOD/TDIRECT have transport codes
            idx = 5 if has_tc else 1  # offset to path_raw byte
            if idx >= len(payload):
                return 'unknown_echo'
            path_raw = payload[idx]
            hops = path_raw & 0x3F
            hsz = ((path_raw >> 6) & 0x03) + 1  # 1..3 byte path hashes
            # Use the encoded path-hash size from the packet (matches what the
            # network is actually using) so we compare on the right number of bytes.
            local_hex = self._local_pub_key[:hsz].hex().upper()
            path_hashes = []
            for i in range(hops):
                start = idx + 1 + i * hsz
                end = start + hsz
                if end <= len(payload):
                    path_hashes.append(payload[start:end].hex().upper())
            # Determine src_hash for self-originated detection (REQ/RESP/TXT/PATH/ADVERT)
            src_hash_hex = None
            pbytes = hops * hsz
            payload_start = idx + 1 + pbytes
            if payload_start < len(payload):
                inner = payload[payload_start:]
                if pt in (0x00, 0x01, 0x02, 0x08) and len(inner) >= 2:
                    src_hash_hex = f'{inner[1]:02X}'
                elif pt == 0x04 and len(inner) >= 1:
                    src_hash_hex = f'{inner[0]:02X}'
            local_first_byte_hex = self._local_pub_key[:1].hex().upper()
            # --- Classification rules ---
            if path_hashes:
                # Our hash is the LAST entry -> we transmitted this packet last;
                # hearing it back means RF self-coupling (true self_echo).
                if path_hashes[-1] == local_hex:
                    return 'self_echo'
                # Our hash is somewhere earlier in the path -> a neighbour took
                # our forwarded packet and forwarded it onward (mesh_echo).
                if local_hex in path_hashes:
                    return 'mesh_echo'
                # Our identity is the source but path was added by relays ->
                # neighbour retransmitted our originated packet (mesh_echo).
                if src_hash_hex and src_hash_hex == local_first_byte_hex:
                    return 'mesh_echo'
                # Hash matched a recent TX but neither path nor src confirms
                # our involvement -> rare collision; classify as unknown.
                return 'unknown_echo'
            # No path entries at all
            if src_hash_hex and src_hash_hex == local_first_byte_hex:
                # Originated by us, no relays yet -> direct RF self-echo.
                return 'self_echo'
            # No path entries AND src_hash not parsable/not matching.
            # Since this packet matched our recent TX hash (the echo detection
            # prerequisite), and the path contains no relays, the most likely
            # explanation is RF self-coupling on a packet type whose src_hash
            # we cannot easily extract (e.g. GROUP_TXT, RAW_CUSTOM, TRACE).
            # Mesh-echoes would have left a path entry from the relaying
            # neighbour. Classify as self_echo to provide actionable info
            # instead of opaque 'unknown'.
            return 'self_echo'
        except Exception:
            return 'unknown_echo'

    def _start_hourly_timer(self) -> None:
        """Start the recurring hourly summary timer."""
        if not self._running:
            return
        self._stop_hourly_timer()
        self._hourly_timer = threading.Timer(3600.0, self._log_hourly_summary)
        self._hourly_timer.daemon = True
        self._hourly_timer.start()
        logger.info('WM1303Backend: hourly summary timer started')

    def _stop_hourly_timer(self) -> None:
        """Cancel the hourly summary timer."""
        if self._hourly_timer:
            self._hourly_timer.cancel()
            self._hourly_timer = None

    def _log_hourly_summary(self) -> None:
        """Log an hourly summary of RX/TX/bridge activity and reset counters."""
        with self._hourly_lock:
            rx_total = self._hourly_rx_total
            rx_ok = self._hourly_rx_crc_ok
            rx_err = self._hourly_rx_crc_err
            tx_ok = self._hourly_tx_ok
            tx_fail = self._hourly_tx_fail
            tx_lbt = self._hourly_tx_lbt_block
            fsk = self._hourly_fsk_skipped
            scans = self._hourly_scan_sweeps
            noise_disc = getattr(self, '_hourly_noise_discarded', 0)
            sf_mismatch = getattr(self, '_hourly_sf_mismatch', 0)
            # Reset counters
            self._hourly_rx_total = 0
            self._hourly_rx_crc_ok = 0
            self._hourly_rx_crc_err = 0
            self._hourly_tx_ok = 0
            self._hourly_tx_fail = 0
            self._hourly_tx_lbt_block = 0
            self._hourly_fsk_skipped = 0
            self._hourly_scan_sweeps = 0
            self._hourly_noise_discarded = 0
            self._hourly_sf_mismatch = 0

        # Get bridge stats if available
        bridge_fwd = 0
        dedup_hits = 0
        try:
            from repeater.bridge_engine import _active_bridge
            if _active_bridge:
                bridge_fwd = _active_bridge.forwarded_packets
                dedup_hits = _active_bridge.dropped_duplicate
        except Exception:
            pass

        logger.info('[HOURLY] RX: %d (CRC_OK=%d, CRC_ERR=%d, FSK_SKIP=%d, SF_MISMATCH=%d, NOISE=%d) | '
                    'TX: %d (OK=%d, FAIL=%d, LBT_BLOCK=%d) | '
                    'Bridge: %d fwd, %d dedup | Scan: %d sweeps',
                    rx_total, rx_ok, rx_err, fsk, sf_mismatch, noise_disc,
                    tx_ok + tx_fail + tx_lbt, tx_ok, tx_fail, tx_lbt,
                    bridge_fwd, dedup_hits, scans)

        # Restart timer for next hour
        self._start_hourly_timer()



    def get_and_reset_crc_rate_counters(self) -> dict:
        """Return per-channel CRC error/disabled counts and reset to zero.

        Called by the external crc_error_rate recorder every 60 seconds.
        Returns dict: {channel_id: {"crc_error": N, "crc_disabled": N}}
        """
        with self._crc_rate_lock:
            snapshot = {}
            for ch_id, counts in self._crc_rate_counters.items():
                snapshot[ch_id] = dict(counts)
            self._crc_rate_counters.clear()
        return snapshot

    def register_virtual_radio(self, radio) -> None:
        self.virtual_radios[radio.channel_id] = radio
        self.channels[radio.channel_id] = radio.channel_config

    def get_radios(self) -> list:
        """Create and register VirtualLoRaRadio instances from SSOT + config.

        PRIMARY source: wm1303_ui.json (SSOT) — the user-facing channel config.
        FALLBACK: config.yaml wm1303.channels — only when the SSOT is absent.

        Each active SSOT channel becomes a VirtualLoRaRadio that self-registers
        via register_virtual_radio() in __init__.

        Returns:
            List of VirtualLoRaRadio instances (one per active channel).
        """
        from .virtual_radio import VirtualLoRaRadio
        import json as _json

        # If radios already registered, return them
        if self.virtual_radios:
            logger.info('WM1303Backend.get_radios(): returning %d existing radios',
                       len(self.virtual_radios))
            return list(self.virtual_radios.values())

        _CHANNEL_ID_BY_INDEX = ['channel_a', 'channel_b', 'channel_c', 'channel_d']

        # ---- PRIMARY: read channels from SSOT (wm1303_ui.json) ----
        ui_path = resolve_config_path('wm1303_ui.json')
        ssot_channels = []
        ssot_present = ui_path.exists()
        try:
            if ui_path.exists():
                ui = _json.loads(ui_path.read_text())
                ssot_channels = ui.get('channels', [])
                if not isinstance(ssot_channels, list):
                    raise ValueError('channels must be a list')
        except Exception as e:
            raise ValueError(f'Cannot load radio settings from {ui_path}: {e}') from e

        # Filter to active-only SSOT channels
        active_ssot = [ch for ch in ssot_channels if ch.get('active', False)]

        if ssot_present:
            logger.info('WM1303Backend.get_radios(): SSOT has %d active channels '
                       '(of %d total)', len(active_ssot), len(ssot_channels))
            radios = []
            for idx, ssot_ch in enumerate(ssot_channels):
                if idx >= len(_CHANNEL_ID_BY_INDEX):
                    logger.warning('WM1303Backend.get_radios(): max %d channels '
                                  'supported, ignoring extra', len(_CHANNEL_ID_BY_INDEX))
                    break
                if not ssot_ch.get('active', False):
                    continue
                channel_id = _CHANNEL_ID_BY_INDEX[idx]
                channel_config = {
                    'frequency': int(ssot_ch.get('frequency', 0)),
                    'spreading_factor': int(ssot_ch.get('spreading_factor', 7)),
                    'bandwidth': int(ssot_ch.get('bandwidth', 125000)),
                    'coding_rate': ssot_ch.get('coding_rate', '4/5'),
                    'preamble_length': int(ssot_ch.get('preamble_length', 17)),
                    'tx_power': int(ssot_ch.get('tx_power', 14)),
                    'tx_enable': ssot_ch.get('tx_enabled', True),
                    'active': True,
                    'name': ssot_ch.get('name', f'ch_{idx}'),
                    'friendly_name': ssot_ch.get('friendly_name', f'Channel {idx}'),
                }
                logger.info('WM1303Backend.get_radios(): SSOT -> %s freq=%d SF%d (%s)',
                           channel_id, channel_config['frequency'],
                           channel_config['spreading_factor'],
                           channel_config['friendly_name'])
                radio = VirtualLoRaRadio(self, channel_id, channel_config)
                radios.append(radio)


            logger.info('WM1303Backend.get_radios(): created %d radios from SSOT: %s',
                       len(radios), [r.channel_id for r in radios])
            return radios

        # ---- FALLBACK: read from config.yaml ----
        logger.warning('WM1303Backend.get_radios(): no SSOT file, '
                      'falling back to config.yaml')
        wm1303_cfg = self.config.get('wm1303', {})
        channels_cfg = wm1303_cfg.get('channels', {})
        if not channels_cfg:
            logger.warning('WM1303Backend.get_radios(): no channels in config.yaml either')
            return []

        radios = []
        for channel_id, cfg in channels_cfg.items():
            if not cfg.get('active', True):
                continue
            channel_config = dict(cfg)
            ch_freq = int(cfg.get('frequency', 0))
            logger.info('WM1303Backend.get_radios(): config.yaml -> %s freq=%d SF%s',
                       channel_id, ch_freq, channel_config.get('spreading_factor', '?'))
            radio = VirtualLoRaRadio(self, channel_id, channel_config)
            radios.append(radio)

        logger.info('WM1303Backend.get_radios(): created %d radios from config.yaml: %s',
                   len(radios), [r.channel_id for r in radios])
        return radios

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    # ----------------------------------------------------------------
    # LoRaRadio interface methods required by dev-branch Dispatcher
    # ----------------------------------------------------------------

    def set_rx_callback(self, callback):
        """Set a callback to be called with each received packet (bytes, rssi, snr)."""
        self.rx_callback = callback
        logger.info("WM1303Backend: RX callback registered: %s", callback)

    def get_last_rssi(self) -> int:
        """Return last received RSSI in dBm."""
        return self._last_pkt_rssi

    def get_last_snr(self) -> float:
        """Return last received SNR in dB."""
        return self._last_pkt_snr

    def sleep(self):
        """Put the radio into low-power mode (no-op for WM1303 concentrator)."""
        pass

    async def wait_for_rx(self) -> bytes:
        """Wait for a packet (not used - WM1303 uses callback-based RX via UDP)."""
        raise NotImplementedError(
            "WM1303Backend uses callback-based RX via set_rx_callback(), "
            "not polling via wait_for_rx()"
        )

    def begin(self) -> bool:
        if self._running:
            return True
        if self._global_tx_scheduler is not None:
            raise RuntimeError('Cannot restart backend while its TX scheduler is still owned')
        if self._proc is not None:
            raise RuntimeError('Cannot restart backend: previous packet forwarder is still owned')
        for name in ('_thread', '_stdout_thread', '_watchdog_thread',
                     '_snapshot_thread', '_nf_monitor_thread'):
            thread = getattr(self, name, None)
            if thread is not None and thread.is_alive():
                raise RuntimeError(f'Cannot restart backend while {name} is still stopping')
        self._stop_event.clear()

        # Auto-create virtual radios if none registered yet
        if not self.virtual_radios:
            logger.info('WM1303Backend.begin(): no virtual radios registered, calling get_radios()')
            self.get_radios()

        try:
            self._loop = asyncio.get_event_loop()
        except RuntimeError:
            self._loop = None

        # One snapshot for HAL configuration, startup gating and TX queues.
        # Re-reading a changing/missing UI file here can enable a different
        # set of channels in Python than in the packet-forwarder process.
        ui = self._load_radio_settings()
        self._write_pktfwd_config(ui_config=ui)

        # Check if ANY channel is active before starting pkt_fwd.
        # If no channels are configured (fresh install), run in IDLE mode:
        # webserver + API are available so the user can configure channels,
        # but pkt_fwd is NOT started (it crashes without active channels).
        _has_active_channels = (
            any(ch.get('active', False) for ch in ui.get('channels', [])) or
            ui.get('channel_e', {}).get('enabled', False) or
            ui.get('channel_f', {}).get('enabled', False)
        )

        if not _has_active_channels:
            logger.warning(
                'WM1303Backend: NO active channels configured. '
                'Running in IDLE mode (web UI available, radio not started). '
                'Configure channels via http://<ip>:8000/wm1303.html then restart the service.'
            )
            self._idle_mode = True
            self._runtime_radio_config = ui
            self._running = True
            # main owns READY=1 after required HTTP startup succeeds. Keep
            # feeding WatchdogSec while idle so initial setup remains usable.
            self._watchdog_running = True
            self._watchdog_thread = threading.Thread(
                target=self._idle_keepalive_loop, daemon=True,
                name='idle-keepalive')
            self._watchdog_thread.start()
            return True

        self._idle_mode = False
        # Validate queue settings before opening resources or resetting hardware.
        self._init_tx_queues(ui_config=ui)
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._sock.bind(('0.0.0.0', UDP_PORT_UP))
            self._sock.settimeout(2.0)
            logger.info('WM1303Backend: UDP server listening on port %d', UDP_PORT_UP)

            # PULL_DATA is the startup handshake: its reader must be running
            # before _start_pktfwd waits for it.
            self._running = True
            self._thread = threading.Thread(target=self._udp_loop, daemon=True)
            self._thread.start()
            self._start_pktfwd()
            self._init_sx1261_lbt()

            self._snapshot_running = True
            self._snapshot_thread = threading.Thread(
                target=self._channel_stats_snapshot_loop, daemon=True)
            self._snapshot_thread.start()

            self._watchdog_running = True
            self._last_rx_timestamp = time.monotonic()
            self._watchdog_thread = threading.Thread(
                target=self._watchdog_loop, daemon=True, name='rx-watchdog')
            self._watchdog_thread.start()
        except Exception:
            self.stop()
            raise
        logger.info('WM1303Backend: backend started with %d virtual channels '
                    '(RF0-TX direct PULL_RESP architecture)', len(self.channels))

        # Start periodic heartbeat reset (full concentrator restart every 3 min)
        # self._start_heartbeat_reset()  # DISABLED: breaks UDP pipeline
        return True


    @staticmethod
    def _write_pktfwd_file(destination: Path, content: str) -> None:
        """Atomically publish a packet-forwarder configuration file."""
        destination = destination.resolve()
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                                             dir=destination.parent, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
                os.fchmod(stream.fileno(), destination.stat().st_mode & 0o777
                          if destination.exists() else 0o644)
            os.replace(temporary, destination)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _write_pktfwd_config(self, ui_config: dict | None = None) -> None:
        """Write bridge_conf.json from _generate_bridge_conf (SSOT).

        bridge_conf.json is AUTHORITATIVE. global_conf.json is always
        a copy of bridge_conf.json after generation.
        """
        conf = _generate_bridge_conf(self.channels, ui_config=ui_config)
        content = json.dumps(conf, indent=2)
        for destination in (BRIDGE_CONF, PKTFWD_DIR / 'global_conf.json', ACTIVE_BRIDGE_CONF):
            self._write_pktfwd_file(destination, content)
        self._active_pktfwd_config = content
        # Propagate errors: starting with stale or partial settings is unsafe.
        logger.info('WM1303Backend: wrote packet-forwarder configuration')

    def _ensure_active_pktfwd_config(self) -> None:
        """Restore a missing/damaged runtime file, never newly saved settings."""
        if self._active_pktfwd_config is None:
            raise RuntimeError('No owned packet-forwarder configuration snapshot')
        try:
            if ACTIVE_BRIDGE_CONF.read_text() == self._active_pktfwd_config:
                return
        except (OSError, UnicodeError):
            pass
        self._write_pktfwd_file(ACTIVE_BRIDGE_CONF, self._active_pktfwd_config)

    def _load_radio_settings(self) -> dict:
        """Read desired settings once; reject malformed files before startup."""
        path = resolve_config_path('wm1303_ui.json')
        if path.exists():
            ui = json.loads(path.read_text())
        else:
            ui = {'channels': [dict(self.channels.get(f'channel_{letter}', {}),
                                   active=self.channels.get(f'channel_{letter}', {}).get('active', True)
                                   and f'channel_{letter}' in self.channels)
                               for letter in 'abcd']}
        if not isinstance(ui, dict):
            raise ValueError('Radio settings must be an object')
        channels = ui.get('channels', [])
        if not isinstance(channels, list) or any(not isinstance(ch, dict) for ch in channels):
            raise ValueError('Radio channels must be a list of objects')
        for name in ('channel_e', 'channel_f', 'adv_config'):
            if not isinstance(ui.get(name, {}), dict):
                raise ValueError(f'{name} must be an object')
        return ui

    def _init_tx_queues(self, ui_config: dict | None = None) -> None:
        """Create queues for enabled TX endpoints using their exact radio settings."""
        ui = self._load_radio_settings() if ui_config is None else ui_config
        self._apply_ssot_channel_freqs(ui)

        channel_configs = list(self.channels.items())
        for channel_id, defaults in (
                ('channel_e', {'frequency': 869618000, 'bandwidth': 62500,
                               'spreading_factor': 8, 'tx_power': 27}),
                ('channel_f', {'frequency': 869525000, 'bandwidth': 250000,
                               'spreading_factor': 9, 'tx_power': 14})):
            settings = ui.get(channel_id, {})
            if settings.get('enabled', False) and settings.get('tx_enabled', True):
                channel_configs.append((channel_id, {**defaults, **settings}))

        manager = TXQueueManager()
        queue_config = self.config.get('wm1303', {}).get('tx_queue', {})
        advanced = ui.get('adv_config', {})
        for channel_id, cfg in channel_configs:
            if not cfg.get('active', True) or not cfg.get('tx_enabled', cfg.get('tx_enable', True)):
                logger.info('WM1303Backend: TX disabled for %s', channel_id)
                continue
            cr = cfg.get('coding_rate', '4/5')
            cr = int(cr.split('/')[-1]) if isinstance(cr, str) else int(cr)
            manager.add_channel(
                channel_id=channel_id,
                freq_hz=int(cfg.get('frequency', 869462500)),
                bw_khz=int(cfg.get('bandwidth', 125000)) / 1000.0,
                sf=int(cfg.get('spreading_factor', 8)),
                cr=cr,
                preamble=int(cfg.get('preamble_length', 17)),
                tx_power=int(cfg.get('tx_power', 14)),
                queue_size=int(queue_config.get('queue_size', 15)),
                ttl_seconds=float(advanced.get('tx_packet_ttl_seconds', 60)),
                overflow_policy=advanced.get('tx_overflow_policy', 'drop_oldest'),
            )
        self._tx_queue_manager = manager
        # Saving new UI settings must not retune Python packet routing while
        # the HAL still runs its startup configuration.
        self._runtime_radio_config = ui
        self._channel_e_cache_time = self._channel_f_cache_time = 0
        self._lbt_config_cache = {}
        self._lbt_config_cache_time = 0

    def _read_active_ui(self) -> dict:
        """Read startup settings; consult desired settings only before begin."""
        if self._runtime_radio_config is not None:
            return self._runtime_radio_config
        path = resolve_config_path('wm1303_ui.json')
        return json.loads(path.read_text()) if path.exists() else {}

    def _load_channel_e_cache(self) -> int:
        """Return Channel E's active settings; use UI only before startup."""
        now = time.monotonic()
        if (now - self._channel_e_cache_time) < self._channel_e_cache_ttl and self._channel_e_freq_cache:
            return self._channel_e_freq_cache
        try:
            _ui = self._read_active_ui().get('channel_e', {})
            self._channel_e_config_cache = _ui
            self._channel_e_freq_cache = int(_ui.get("frequency", 0))
            self._channel_e_cache_time = now
        except Exception as e:
            logger.debug("WM1303Backend: channel_e cache refresh error: %s", e)
            # Keep stale cache if available
            if not self._channel_e_freq_cache:
                self._channel_e_freq_cache = 0
        return self._channel_e_freq_cache

    def _load_channel_f_cache(self) -> tuple:
        """Return active Channel F (enabled, frequency, bandwidth, SF)."""
        now = time.monotonic()
        if (now - self._channel_f_cache_time) < self._channel_f_cache_ttl and self._channel_f_freq_cache:
            return (self._channel_f_enabled_cache, self._channel_f_freq_cache,
                    self._channel_f_bw_cache, self._channel_f_sf_cache)
        try:
            _ui = self._read_active_ui().get('channel_f', {})
            self._channel_f_config_cache = _ui
            self._channel_f_enabled_cache = bool(_ui.get("enabled", False))
            self._channel_f_freq_cache = int(_ui.get("frequency", 0))
            _bw_hz = int(_ui.get("bandwidth", 0))
            if _bw_hz not in (125000, 250000, 500000):
                _bw_hz = 0
            self._channel_f_bw_cache = _bw_hz
            _sf = int(_ui.get("spreading_factor", 0))
            if _sf < 5 or _sf > 12:
                _sf = 0
            self._channel_f_sf_cache = _sf
            self._channel_f_cache_time = now
        except Exception as e:
            logger.debug("WM1303Backend: channel_f cache refresh error: %s", e)
        return (self._channel_f_enabled_cache, self._channel_f_freq_cache,
                self._channel_f_bw_cache, self._channel_f_sf_cache)

    def _apply_ssot_channel_freqs(self, ui: dict) -> None:
        """Apply UI settings by the fixed A-D identity, including disabled slots."""
        try:
            ui_channels = ui.get('channels')
            if not isinstance(ui_channels, list):
                return
            fixed_ids = ('channel_a', 'channel_b', 'channel_c', 'channel_d')
            for channel_id, cfg in self.channels.items():
                if channel_id in fixed_ids:
                    index = fixed_ids.index(channel_id)
                    if index >= len(ui_channels):
                        cfg['active'] = False
                        continue
                    settings = ui_channels[index]
                else:
                    name = cfg.get('name') or channel_id
                    settings = next((ch for ch in ui_channels if ch.get('name') == name), None)
                    if settings is None:
                        continue
                for key in ('frequency', 'spreading_factor', 'bandwidth', 'coding_rate',
                            'preamble_length', 'tx_power', 'name', 'friendly_name'):
                    if key in settings:
                        cfg[key] = settings[key]
                cfg['active'] = bool(settings.get('active', False))
                cfg['tx_enabled'] = bool(settings.get('tx_enabled', settings.get('tx_enable', True)))
        except (OSError, ValueError, TypeError) as exc:
            logger.warning('WM1303Backend: SSOT channel settings could not be applied: %s', exc)

    def _get_total_tx_pending(self) -> int:
        """Return total number of pending packets across all TX channel queues."""
        if not self._tx_queue_manager:
            return 0
        total = 0
        for queue in self._tx_queue_manager.queues.values():
            total += queue.queue.qsize()
        return total

    def _calculate_dynamic_tx_hold(self) -> float:
        """Calculate TX hold duration based on current TX queue depth.

        Design principle: TX must be sent ASAP. Hold is ONLY for brief dedup
        window when a single packet arrives (waiting for its dedup partner).
        Once a batch is formed (2+ pending), hold is ZERO — send immediately!

        - 0 packets pending → no hold (nothing to send)
        - 1 packet  pending → 50ms  (brief dedup window)
        - 2+ packets pending → 0ms (batch ready, TX ASAP!)
        """
        pending = self._get_total_tx_pending()
        if pending <= 1:
            return self._tx_hold_single if pending == 1 else self._tx_hold_empty
        return 0.0  # batch already formed → send NOW

    def _init_sx1261_lbt(self) -> None:
        """Initialize Channel E status reporting (managed by HAL)."""
        self._sx1261 = None
        self._sx1261_available = False
        self._sx1261_managed_by_hal = True
        self._sx1261_hal_config = {}

        try:
            import yaml
            cfg_path = resolve_config_path('config.yaml')
            if cfg_path.exists():
                with open(cfg_path) as f:
                    cfg = yaml.safe_load(f) or {}
                self._sx1261_hal_config = cfg.get('sx1261_lbt', {})
                logger.info('WM1303Backend: Channel E is managed by lora_pkt_fwd HAL '
                           '(SPI conflict prevents independent access). '
                           'Config: role=%s', self._sx1261_hal_config.get('role', 'unknown'))
            else:
                logger.info('WM1303Backend: config.yaml not found, Channel E status unknown')
        except Exception as e:
            logger.warning('WM1303Backend: Could not read Channel E config: %s', e)



    # ------------------------------------------------------------------
    # Software LBT (Listen Before Talk) via spectral scan
    # ------------------------------------------------------------------

    def _read_spectral_scan(self, max_age: float = 30.0) -> dict:
        """Read latest spectral scan results from JSON file written by pkt_fwd.

        The C spectral scan thread writes /tmp/pymc_spectral_results.json with:
        {
            "timestamp": <unix_ts>,
            "channels": {
                "<freq_hz>": {"rssi_avg": ..., "rssi_min": ..., "rssi_max": ..., "samples": ...},
                ...
            }
        }

        Returns {freq_mhz: rssi_dbm} for compatibility with noise floor logic.

        Args:
            max_age: Maximum age in seconds for scan data (default 30 for LBT).
                     Use larger values for noise floor estimation.
        """
        try:
            scan_path = Path('/tmp/pymc_spectral_results.json')
            if not scan_path.exists():
                return {}
            data = json.loads(scan_path.read_text())
            ts = data.get('timestamp', 0)
            age = time.time() - ts
            if age > max_age:
                logger.debug('WM1303Backend: spectral scan data stale (%.0fs old, max=%.0fs)',
                            age, max_age)
                return {}
            result = {}
            # New format: {channels: {freq_hz_str: {rssi_avg, rssi_min, rssi_max, samples}}}
            channels = data.get('channels', {})
            for freq_hz_str, stats in channels.items():
                try:
                    freq_mhz = int(freq_hz_str) / 1e6
                    samples = stats.get('samples', 0)
                    rssi_avg = stats.get('rssi_avg', -120.0)
                    # Skip entries with no samples (sweep disabled/stale) or
                    # sentinel values (SX1261 returns -127 when not scanning)
                    if samples == 0 or rssi_avg <= -126.0:
                        continue
                    result[freq_mhz] = rssi_avg
                except (ValueError, TypeError):
                    continue
            # Legacy format fallback (scan_points list)
            if not result:
                for pt in data.get('scan_points', []):
                    result[pt['freq_mhz']] = pt['rssi_dbm']
            return result
        except Exception as e:
            logger.debug('WM1303Backend: spectral scan read error: %s', e)
            return {}

    def _read_spectrum_from_db(self, max_age: float = 3600.0) -> dict:
        """Fallback: read recent spectrum data from spectrum_history.db.

        Returns {freq_mhz: rssi_dbm} from most recent scan entries.
        """
        try:
            import sqlite3
            db_path = str(self._storage_dir / 'spectrum_history.db')
            if not os.path.exists(db_path):
                return {}
            since_ts = time.time() - max_age
            with _db_conn(db_path) as conn:
                rows = conn.execute(
                    'SELECT freq_mhz, AVG(rssi_dbm) '
                    'FROM spectrum_scans WHERE timestamp > ? '
                    'GROUP BY freq_mhz ORDER BY freq_mhz',
                    (since_ts,)
                ).fetchall()
            if not rows:
                return {}
            result = {r[0]: round(r[1], 1) for r in rows}
            logger.debug('NoiseFloorMonitor: got %d freq points from spectrum_history.db', len(result))
            return result
        except Exception as e:
            logger.debug('NoiseFloorMonitor: spectrum DB read error: %s', e)
            return {}


    def _get_channel_lbt_config(self, channel_id: str) -> dict:
        """Get active LBT settings using fixed A-D/E/F identities."""
        now = time.monotonic()
        if now - self._lbt_config_cache_time < self._lbt_config_cache_ttl:
            cached = self._lbt_config_cache.get(channel_id)
            if cached is not None:
                return cached
        try:
            ui = self._read_active_ui()
            channels = [(f'channel_{letter}', settings)
                        for letter, settings in zip('abcd', ui.get('channels', []))]
            channels.extend((cid, ui.get(cid, {})) for cid in ('channel_e', 'channel_f'))
            new_cache = {}
            for cid, settings in channels:
                enabled = settings.get('active', settings.get('enabled', False))
                value = {
                    'lbt_enabled': bool(enabled and settings.get('lbt_enabled', False)),
                    'lbt_rssi_target': settings.get('lbt_threshold', settings.get('lbt_rssi_target', -80)),
                }
                new_cache[cid] = value
                if settings.get('name'):
                    new_cache[settings['name']] = value
                if settings.get('friendly_name'):
                    new_cache[settings['friendly_name'].lower().replace(' ', '_')] = value
            self._lbt_config_cache = new_cache
            self._lbt_config_cache_time = now
            return new_cache.get(channel_id, {'lbt_enabled': False})
        except Exception:
            return {'lbt_enabled': False}


    # ------------------------------------------------------------------
    # Per-Channel Noise Floor Monitor
    # ------------------------------------------------------------------

    def _start_noise_floor_monitor(self) -> None:
        """Start the background noise floor monitoring thread."""
        if self._nf_monitor_running:
            return
        self._nf_monitor_running = True
        self._nf_monitor_thread = threading.Thread(
            target=self._noise_floor_monitor_loop, daemon=True, name='NoiseFloorMonitor')
        self._nf_monitor_thread.start()
        logger.info('WM1303Backend: NoiseFloorMonitor started (interval=%ds)', self._nf_interval)

    def _stop_noise_floor_monitor(self) -> None:
        """Stop the noise floor monitoring thread."""
        self._nf_monitor_running = False
        if (self._nf_monitor_thread
                and self._nf_monitor_thread is not threading.current_thread()):
            if self._nf_monitor_thread.is_alive():
                self._nf_monitor_thread.join()
            if not self._nf_monitor_thread.is_alive():
                self._nf_monitor_thread = None
        logger.info('WM1303Backend: NoiseFloorMonitor stopped')

    def _noise_floor_monitor_loop(self) -> None:
        """Background loop: measure per-channel noise floor every N seconds.

        Reads spectral scan results written by the pkt_fwd C process
        (via /tmp/pymc_spectral_results.json).  The C spectral scan thread
        handles TX conflicts itself with a retry loop, so no TX hold is
        needed from the Python side.
        """
        # Wait 10s for system to stabilize before first measurement
        for _ in range(20):
            if not self._nf_monitor_running:
                return
            self._stop_event.wait(0.5)

        while self._nf_monitor_running:
            try:
                # Read spectral data and compute noise floors
                self._update_channel_noise_floors()
            except Exception as e:
                logger.error('NoiseFloorMonitor error: %s', e)
            # Sleep in small increments for responsive shutdown
            for _ in range(self._nf_interval * 2):
                if not self._nf_monitor_running:
                    return
                self._stop_event.wait(0.5)


    def _update_channel_noise_floors(self) -> None:
        """Compute per-channel noise floor from spectral scan data and store in DB."""
        # Load active channels from UI config
        try:
            ui = self._read_active_ui()
            channels = [ch for ch in ui.get('channels', []) if ch.get('active', False)]
            # Include Channel E (SX1261) in noise floor processing
            che_cfg = ui.get('channel_e', {})
            if che_cfg.get('enabled', False):
                channels.append({
                    'name': che_cfg.get('friendly_name', 'Channel E'),
                    'frequency': int(che_cfg.get('frequency', 0)),
                    'bandwidth': int(che_cfg.get('bandwidth', 62500)),
                    'active': True
                })
            # Include Channel F (chan_Lora_std on SX1302 RF0) in noise floor processing
            chf_cfg = ui.get('channel_f', {})
            if chf_cfg.get('enabled', False):
                channels.append({
                    'name': chf_cfg.get('friendly_name', 'Channel F'),
                    'frequency': int(chf_cfg.get('frequency', 0)),
                    'bandwidth': int(chf_cfg.get('bandwidth', 250000)),
                    'active': True
                })
            # Build/refresh freq_hz -> channel_id mapping
            new_freq_map = {}
            # Build/refresh freq_hz -> ui_config_name mapping
            new_map = {}
            for ch in ui.get('channels', []):
                f = ch.get('frequency', 0)
                n = ch.get('name', '')
                if f and n:
                    new_map[int(f)] = n
            # Build channel_id -> ui_config_name mapping (supports same-frequency channels)
            _CHID = ['channel_a', 'channel_b', 'channel_c', 'channel_d']
            new_id_map = {}
            new_ui_to_ch_id = {}  # reverse: ui_name -> channel_id
            for idx, ch in enumerate(ui.get('channels', [])):
                if ch.get('active', False) and idx < len(_CHID):
                    ch_name = ch.get('name', '')
                    new_id_map[_CHID[idx]] = ch_name
                    new_ui_to_ch_id[ch_name] = _CHID[idx]
                    f = ch.get('frequency', 0)
                    if f:
                        new_freq_map[int(f)] = _CHID[idx]
            # --- Channel E (SX1261) ---
            che = ui.get('channel_e', {})
            if che.get('enabled', False):
                che_name = che.get('friendly_name', 'Channel E')
                che_freq = int(che.get('frequency', 0))
                new_id_map['channel_e'] = che_name
                new_ui_to_ch_id[che_name] = 'channel_e'
                if che_freq:
                    new_map[che_freq] = che_name
                    new_freq_map[che_freq] = 'channel_e'
            # --- Channel F (chan_Lora_std on SX1302 RF0) ---
            chf = ui.get('channel_f', {})
            if chf.get('enabled', False):
                chf_name = chf.get('friendly_name', 'Channel F')
                chf_freq = int(chf.get('frequency', 0))
                new_id_map['channel_f'] = chf_name
                new_ui_to_ch_id[chf_name] = 'channel_f'
                if chf_freq:
                    # Note: channel_f may share frequency with another channel; last write wins.
                    # The RX classifier in _dispatch_rx falls back to BW+SF matching for channel_f
                    # so freq-only collisions are handled correctly.
                    new_map[chf_freq] = chf_name
                    new_freq_map[chf_freq] = 'channel_f'
            with self._nf_lock:
                self._freq_to_ui_name = new_map
                self._ch_id_to_ui_name = new_id_map
                self._ui_name_to_ch_id = new_ui_to_ch_id
                self._freq_to_ch_id = new_freq_map
        except Exception as e:
            logger.debug('NoiseFloorMonitor: cannot read UI config: %s', e)
            return

        if not channels:
            return

        # Read spectral scan data
        # Read spectral scan data (use long max_age for noise floor, fallback to DB)
        scan_data = self._read_spectral_scan(max_age=300.0)  # 5 min max — stale data from before restart is discarded
        if not scan_data:
            scan_data = self._read_spectrum_from_db(max_age=86400.0)
        if not scan_data:
            # Fallback: use LBT RSSI from TX queues as noise floor estimate
            self._update_noise_floors_from_lbt(channels)
            return

        now = time.time()
        db_path = self._db_path

        for ch in channels:
            ch_name = ch.get('name', '')
            freq_hz = ch.get('frequency', 0)
            bw_hz = ch.get('bandwidth', 125000)
            if not freq_hz:
                continue

            freq_mhz = freq_hz / 1e6
            bw_mhz = bw_hz / 1e6
            half_bw = bw_mhz / 2

            # Collect scan points within ±BW/2 of channel center
            nearby_rssi = []
            for scan_freq, rssi_dbm in scan_data.items():
                if abs(scan_freq - freq_mhz) <= half_bw:
                    nearby_rssi.append(rssi_dbm)

            if not nearby_rssi:
                # Try wider tolerance (±1 MHz) if no points within BW
                for scan_freq, rssi_dbm in scan_data.items():
                    if abs(scan_freq - freq_mhz) <= 1.0:
                        nearby_rssi.append(rssi_dbm)

            if not nearby_rssi:
                continue

            samples_collected = len(nearby_rssi)

            # Get current noise floor for this channel (for filtering)
            with self._nf_lock:
                current_nf = self._channel_noise_floors.get(ch_name, -120.0)

            # Filter: only accept samples where sample < current_nf + 14 dB
            # This removes strong signals, keeping only noise-level samples
            filter_threshold = current_nf + 14.0
            accepted = [r for r in nearby_rssi if r < filter_threshold]

            # Detection Method 2: Count strong RSSI spikes (signal > noise_floor + 20 dB)
            # These indicate LoRa packets in the air that the SX1302 should be demodulating
            spike_threshold = current_nf + 20.0
            spikes_this_scan = sum(1 for r in nearby_rssi if r > spike_threshold)
            if spikes_this_scan > 0:
                self._rssi_spike_count += spikes_this_scan
                logger.debug('NoiseFloorMonitor: %s: %d RSSI spikes above %.1f dBm '
                            '(nf=%.1f, total_spikes=%d)',
                            ch_name, spikes_this_scan, spike_threshold,
                            current_nf, self._rssi_spike_count)


            if not accepted:
                # If all samples above threshold, use the minimum as a rough estimate
                accepted = [min(nearby_rssi)]

            samples_accepted = len(accepted)
            new_nf = sum(accepted) / len(accepted)

            # Clamp to valid range
            new_nf = max(-130.0, min(-50.0, new_nf))

            min_rssi = min(nearby_rssi)
            max_rssi = max(nearby_rssi)

            # Update in-memory cache
            with self._nf_lock:
                self._channel_noise_floors[ch_name] = round(new_nf, 1)

            # Store in SQLite (use position-based channel_id, not display name)
            with self._nf_lock:
                db_ch_id = self._ui_name_to_ch_id.get(ch_name, ch_name)
            try:
                with _db_conn(db_path) as conn:
                    conn.execute(
                        """INSERT INTO noise_floor_history
                        (timestamp, channel_id, noise_floor_dbm,
                         samples_collected, samples_accepted, min_rssi, max_rssi)
                        VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (now, db_ch_id, round(new_nf, 1),
                         samples_collected, samples_accepted,
                         round(min_rssi, 1), round(max_rssi, 1)))
                    conn.commit()
            except Exception as e:
                logger.debug('NoiseFloorMonitor: DB store error for %s: %s', db_ch_id, e)

        logger.debug('NoiseFloorMonitor: updated noise floors for %d channels: %s',
                    len(channels),
                    {ch['name']: self._channel_noise_floors.get(ch['name'])
                     for ch in channels})

    def _update_noise_floors_from_lbt(self, channels: list) -> None:
        """Fallback: derive per-channel noise floor from TX queue LBT RSSI buffers.

        Called when the HAL spectral scan produces no data (e.g. because TX
        activity on RF chain 0 prevents the SX1261 from scanning).  The TX
        queues may still contain RSSI measurements from actual LBT checks
        performed before each transmission.
        """
        if not self._tx_queue_manager:
            logger.debug('NoiseFloorMonitor: no TX queue manager, cannot use LBT fallback')
            return

        db_path = self._db_path
        now = time.time()
        updated = {}

        with self._nf_lock:
            ch_id_to_ui_name = dict(self._ch_id_to_ui_name)

        for ch_id, queue in self._tx_queue_manager.queues.items():
            lbt_avg = queue.stats.get('noise_floor_lbt_avg')
            if lbt_avg is None:
                continue

            # Channel identity is stable even when channels share a frequency.
            ui_name = ch_id_to_ui_name.get(ch_id)
            if ui_name is None:
                continue

            # Use LBT avg as noise floor estimate
            new_nf = round(lbt_avg, 1)
            with self._nf_lock:
                current_nf = self._channel_noise_floors.get(ui_name)
                if current_nf is None or abs(current_nf - new_nf) > 0.5:
                    self._channel_noise_floors[ui_name] = new_nf
                    updated[ui_name] = new_nf

            # Write under the same stable channel ID as the source queue.
            lbt_min = queue.stats.get('noise_floor_lbt_min')
            try:
                with _db_conn(db_path) as conn:
                    conn.execute(
                        """INSERT INTO noise_floor_history
                        (timestamp, channel_id, noise_floor_dbm,
                         samples_collected, samples_accepted, min_rssi, max_rssi)
                        VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (now, ch_id, new_nf,
                         len(queue._lbt_rssi_buffer),
                         len(queue._lbt_rssi_buffer),
                         lbt_min, lbt_avg))
                    conn.commit()
            except Exception as e:
                logger.debug('NoiseFloorMonitor: failed to write LBT noise floor to DB: %s', e)

        if updated:
            logger.info('NoiseFloorMonitor: noise floor from LBT fallback: %s', updated)
        else:
            # No LBT data available, try RX-based estimation
            self._update_noise_floors_from_rx(channels)


    def _update_noise_floors_from_rx(self, channels: list) -> None:
        """Fallback: estimate per-channel noise floor from RX packet RSSI/SNR.

        For each received LoRa packet: noise_floor ≈ RSSI - SNR (when SNR > 0).
        This provides a reasonable estimate when both spectral scan and LBT
        RSSI data are unavailable (e.g., heavy TX activity blocks the SX1261
        spectral scan and LBT is disabled).
        """
        db_path = self._db_path
        now = time.time()
        cutoff = now - self._rx_nf_max_age
        updated = {}

        # The input list is already filtered to active channels. Re-indexing
        # it would rename B/D as A/B whenever earlier channels are disabled.
        with self._nf_lock:
            ch_id_to_ui_name = dict(self._ch_id_to_ui_name)

        with self._rx_nf_lock:
            for ch_id, estimates in self._rx_nf_estimates.items():
                # Filter out old samples
                recent = [(ts, nf) for ts, nf in estimates if ts > cutoff]
                if not recent:
                    continue

                ui_name = ch_id_to_ui_name.get(ch_id)
                if not ui_name:
                    continue

                # Use 10th percentile of NF estimates (lower values = quieter = true noise floor)
                nf_values = sorted([nf for _, nf in recent])
                p10_idx = max(0, int(len(nf_values) * 0.1))
                new_nf = round(nf_values[p10_idx], 1)

                with self._nf_lock:
                    current_nf = self._channel_noise_floors.get(ui_name)
                    if current_nf is None or abs(current_nf - new_nf) > 0.5:
                        self._channel_noise_floors[ui_name] = new_nf
                        updated[ui_name] = new_nf

                # Write to noise_floor_history DB (use position-based channel_id directly)
                try:
                    with _db_conn(db_path) as conn:
                        conn.execute(
                            """INSERT INTO noise_floor_history
                            (timestamp, channel_id, noise_floor_dbm,
                             samples_collected, samples_accepted, min_rssi, max_rssi)
                            VALUES (?, ?, ?, ?, ?, ?, ?)""",
                            (now, ch_id, new_nf,
                             len(recent), len(recent),
                             nf_values[0], nf_values[-1]))
                        conn.commit()
                except Exception as e:
                    logger.debug('NoiseFloorMonitor: failed to write RX noise floor to DB: %s', e)

        if updated:
            logger.info('NoiseFloorMonitor: noise floor from RX estimation: %s', updated)
        else:
            logger.debug('NoiseFloorMonitor: no RX RSSI data available for noise floor estimation')

    def get_channel_noise_floors(self) -> dict:
        """Get current per-channel noise floor values (thread-safe)."""
        with self._nf_lock:
            return dict(self._channel_noise_floors)

    # ------------------------------------------------------------------
    # Hardware CAD via SX1261 (interleaved with spectral scan in pkt_fwd)
    # ------------------------------------------------------------------

    def _write_cad_config_json(self) -> None:
        """Write /tmp/pymc_cad_config.json for the C HAL spectral scan thread.

        This file tells lora_pkt_fwd which channels to CAD-scan after each
        spectral sweep.  It is re-read by the C code at each sweep so
        runtime changes are picked up automatically.
        """
        bw_map = {125000: 0, 250000: 1, 500000: 2}
        try:
            ui = self._read_active_ui()
            cad_channels = []
            for ch in ui.get('channels', []):
                if not ch.get('cad_enabled', False):
                    continue
                freq = ch.get('frequency', 0)
                sf = ch.get('spreading_factor', 7)
                bw_hz = ch.get('bandwidth', 125000)
                ch_id = ch.get('name', '')
                if freq and sf:
                    cad_channels.append({
                        'id': ch_id,
                        'freq_hz': int(freq),
                        'sf': int(sf),
                        'bw': bw_map.get(int(bw_hz), 0),
                    })
            config = {'channels': cad_channels}
            tmp_path = '/tmp/pymc_cad_config.json.tmp'
            final_path = '/tmp/pymc_cad_config.json'
            with open(tmp_path, 'w') as f:
                json.dump(config, f)
            os.replace(tmp_path, final_path)
            logger.debug('WM1303Backend: wrote CAD config (%d channels) to %s',
                        len(cad_channels), final_path)
        except Exception as e:
            logger.warning('WM1303Backend: failed to write CAD config: %s', e)

    def _read_hardware_cad_results(self, max_age: float = 30.0) -> dict:
        """Read hardware CAD results from the spectral scan JSON.

        The C spectral scan thread appends a 'cad' section to the JSON:
        {
            "cad": {
                "<channel_id>": {
                    "freq_hz": 869461000,
                    "sf": 8,
                    "detected": true/false,
                    "rssi": -85,
                    "status": 0  (0=ok, 1=timeout, 2=skipped)
                }
            }
        }

        Returns dict keyed by channel_id, or empty dict if no results.
        """
        try:
            scan_path = Path('/tmp/pymc_spectral_results.json')
            if not scan_path.exists():
                return {}
            data = json.loads(scan_path.read_text())
            ts = data.get('timestamp', 0)
            age = time.time() - ts
            if age > max_age:
                return {}
            return data.get('cad', {})
        except Exception:
            return {}

    def _get_channel_cad_config(self, channel_id: str) -> bool:
        """Check if CAD is enabled for a channel.

        CAD is MANDATORY before every TX — always returns True.
        The per-channel UI toggle is kept for future use (e.g. to control
        the Python-side software pre-filter sensitivity) but does not
        disable the hardware CAD scan in pkt_fwd.
        """
        return True

    # --- Removed: _cad_check, _pre_tx_check, _lbt_check alias ---
    # The Python pre-TX CAD/LBT filter was eliminated together with the
    # custom LBT code in pkt_fwd. HAL LBT (per-channel rssi_target_dbm,
    # handled inside lgw_send) is now the sole RSSI gate; HW-CAD still
    # runs unconditionally in the HAL C code. _store_cad_event remains
    # below for post-TX CAD event logging from TX_ACK parsing.

    def _store_cad_event(self, channel_id: str, result: str,
                         rssi: float = None, context: str = 'lbt_check') -> None:
        """Store a CAD event in the database (fire-and-forget)."""
        try:
            with _db_conn(self._db_path) as conn:
                conn.execute(
                    "INSERT INTO cad_events (timestamp, channel_id, result, rssi_at_time, context) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (time.time(), channel_id, result, rssi, context))
                conn.commit()
        except Exception:
            pass  # Non-critical, don't block TX

    async def ensure_tx_queues_started(self) -> None:
        """Start the GlobalTXScheduler for round-robin TX across all queues."""
        if not self._running:
            return
        if self._global_tx_scheduler and self._global_tx_scheduler._running:
            return
        self._loop = asyncio.get_running_loop()
        if self._tx_queue_manager and self._tx_queue_manager.queues:
            self._global_tx_scheduler = GlobalTXScheduler(
                send_func=self._send_for_scheduler,
                queues=self._tx_queue_manager.queues,
                post_tx_callback=self._update_tx_stats,
                # Pre-TX check removed: HAL LBT inside lgw_send() handles
                # RSSI-based blocking; HW-CAD still runs unconditionally.
                tx_hold_getter=lambda: self._tx_hold_until,
                inter_packet_delay_ms=float(self.config.get('wm1303', {})
                                            .get('tx_queue', {}).get('tx_delay_ms', 0)),
            )
            await self._global_tx_scheduler.start()

            logger.info('WM1303Backend: GlobalTXScheduler started with %d queues',
                        len(self._tx_queue_manager.queues))
            # Start per-channel noise floor monitor
            self._start_noise_floor_monitor()
        else:
            logger.warning('WM1303Backend: No TX queues to schedule')

    def stop(self) -> None:
        """Stop owned producers and finish their DB work; call off-loop."""
        scheduler = self._global_tx_scheduler
        manager = self._tx_queue_manager
        tx_loop = self._loop
        if scheduler is not None:
            if scheduler._task is not None:
                tx_loop = scheduler._task.get_loop()
            try:
                caller_loop = asyncio.get_running_loop()
            except RuntimeError:
                caller_loop = None
            # A blocking join cannot run on the loop it needs to drain. Leave
            # ownership/resources intact when the caller violates this contract.
            if tx_loop is None or not tx_loop.is_running() or caller_loop is tx_loop:
                raise RuntimeError('Stop the WM1303 TX scheduler off-loop while its loop is running')
        self._running = False
        self._stop_event.set()

        # Stop RX watchdog
        self._watchdog_running = False


        # Cancel any pending AGC reset timer
        _agc_timer = getattr(self, "_agc_reset_timer", None)
        if _agc_timer:
            _agc_timer.cancel()
            self._agc_reset_timer = None

        # Cancel any pending heartbeat reset timer
        _hb_timer = getattr(self, "_heartbeat_timer", None)
        if _hb_timer:
            _hb_timer.cancel()
            self._heartbeat_timer = None
        self._snapshot_running = False

        # Stop noise floor monitor
        self._stop_noise_floor_monitor()


        stop_error = None
        async def stop_transmitter():
            if manager:
                manager.stop_all()
            await scheduler.stop()
        if scheduler is not None:
            # scheduler.stop() awaits the actual async send task, including its
            # ACK-future/lock cleanup, and resolves queued callers. Do not close
            # its socket/process dependencies or drop ownership before it ends.
            shutdown = stop_transmitter()
            try:
                completion = asyncio.run_coroutine_threadsafe(shutdown, tx_loop)
            except Exception:
                shutdown.close()
                raise
            try:
                completion.result()
            except Exception as exc:
                stop_error = exc
            else:
                if self._global_tx_scheduler is scheduler:
                    self._global_tx_scheduler = None
        elif manager:
            manager.stop_all()
        if self._sx1261:
            try:
                self._sx1261.close()
            except Exception:
                pass
        process_stopped = True
        try:
            self._stop_pktfwd_process()
        except Exception as exc:
            process_stopped = False
            if stop_error is not None:
                logger.error('WM1303Backend: TX scheduler shutdown also failed: %s', stop_error)
            stop_error = exc

        with self._sock_lock:
            if self._sock:
                self._sock.close()
                self._sock = None
        if self._thread and self._thread is not threading.current_thread():
            if self._thread.is_alive():
                self._thread.join()
            if not self._thread.is_alive():
                self._thread = None
        # These joins happen outside the process/socket locks. In-flight DB
        # writes may exceed a short join timeout; storage must remain open
        # until the producers actually finish. A failed process stop retains
        # ownership and must not wait forever for its still-open stdout pipe.
        producers = ['_watchdog_thread', '_snapshot_thread']
        if process_stopped:
            producers.append('_stdout_thread')
        for name in producers:
            thread = getattr(self, name, None)
            if thread and thread is not threading.current_thread():
                if thread.is_alive():
                    thread.join()
                if not thread.is_alive():
                    setattr(self, name, None)
        if stop_error is not None:
            raise stop_error

    def _signal_pktfwd_group(self, process, signo: int) -> None:
        """Signal only the private group created for this owned forwarder.

        The service is unprivileged but the forwarder runs through sudo.
        SIGKILL sent to sudo alone cannot be relayed to its root-owned child.
        """
        pgid = self._pktfwd_pgid
        if (process is not self._proc or not isinstance(pgid, int)
                or pgid <= 1 or pgid != process.pid or pgid == os.getpgrp()):
            raise RuntimeError('Refusing to signal an unowned packet-forwarder process group')
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return
        except PermissionError:
            pass  # expected for the root-owned radio child
        try:
            if os.geteuid() == 0:
                os.killpg(pgid, signo)
            else:
                result = subprocess.run(
                    ['sudo', '-n', '/bin/kill', f'-{int(signo)}', '--', f'-{pgid}'],
                    stdin=subprocess.DEVNULL, capture_output=True, timeout=3)
                if result.returncode:
                    # Exiting between the existence check and kill is normal.
                    try:
                        os.killpg(pgid, 0)
                    except ProcessLookupError:
                        return
                    except PermissionError:
                        pass
                    raise RuntimeError(f'Unable to signal packet-forwarder group {pgid}')
        except ProcessLookupError:
            pass

    def _stop_pktfwd_process(self) -> None:
        """Stop the lora_pkt_fwd process cleanly."""
        with self._pktfwd_lock:
            process = self._proc
            if process:
                self._signal_pktfwd_group(process, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._signal_pktfwd_group(process, signal.SIGKILL)
                    process.wait(timeout=3)
                else:
                    # A wrapper may exit before its child; do not leave an
                    # inherited stdout pipe or privileged radio process alive.
                    self._signal_pktfwd_group(process, signal.SIGKILL)
                # Keep ownership if signalling/reaping failed, so recovery
                # cannot start a second process against the same hardware.
                self._proc = None
                self._pktfwd_pgid = None
        # Only stop the process we created. A failed bind/start must not kill
        # another service's packet forwarder.
        if self._stdout_thread:
            if self._stdout_thread.is_alive():
                self._stdout_thread.join(timeout=3)
            if not self._stdout_thread.is_alive():
                self._stdout_thread = None
        self._pull_addr = None
        self._stop_hourly_timer()
        logger.info('WM1303Backend: lora_pkt_fwd stopped')

    def _start_pktfwd(self) -> None:
        self._ensure_active_pktfwd_config()
        # Run GPIO reset (standard reset_lgw.sh only)
        try:
            logger.info('WM1303Backend: running reset_lgw.sh for initial reset')
            r = subprocess.run(
                ['sudo', str(PKTFWD_RESET), 'start'],
                cwd=str(PKTFWD_DIR), capture_output=True, timeout=15
            )
            logger.info('WM1303Backend: reset stdout: %s',
                       r.stdout.decode(errors='replace')[:200])
            if r.returncode != 0:
                logger.warning('WM1303Backend: reset returned %d', r.returncode)
            time.sleep(0.5)  # TCXO warmup (optimized for AGC recovery)
        except Exception as e:
            logger.error('WM1303Backend: GPIO reset failed: %s', e)

        # Start lora_pkt_fwd with bridge config
        try:
            cmd = ['sudo', '-n', str(PKTFWD_BIN), '-c', str(ACTIVE_BRIDGE_CONF)]
            logger.info('WM1303Backend: starting lora_pkt_fwd '
                       '(RF0-TX, direct PULL_RESP): %s', ' '.join(cmd))
            self._pktfwd_ready_event.clear()
            with self._pktfwd_lock:
                if not self._running:
                    return  # shutdown may have arrived during the GPIO reset
                if self._proc is not None:
                    raise RuntimeError('Previous packet forwarder must be stopped before starting another')
                self._proc = subprocess.Popen(
                    cmd,
                    cwd=str(PKTFWD_DIR),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    text=True,
                    start_new_session=True,
                )
                self._pktfwd_pgid = self._proc.pid
                process = self._proc
            # Drain stdout immediately; startup logging can fill the pipe
            # before the process reaches its first PULL_DATA handshake.
            self._stdout_thread = threading.Thread(
                target=self._pktfwd_stdout_reader, daemon=True,
                name='pktfwd-stdout')
            self._stdout_thread.start()
            # Wait for pkt_fwd to signal ready via PULL_DATA (max 5s)
            _t = time.monotonic()
            _ready = self._pktfwd_ready_event.wait(timeout=5.0)
            _wait_ms = round((time.monotonic() - _t) * 1000)
            logger.info("WM1303Backend: pktfwd ready in %dms (timeout=%s)", _wait_ms, not _ready)
            if not self._running:
                return
            if process.poll() is not None:
                raise RuntimeError('lora_pkt_fwd exited prematurely')
            logger.info('WM1303Backend: lora_pkt_fwd started (pid %d) '
                       '[RF0-TX, direct PULL_RESP]', process.pid)
            # Write CAD config for the HAL spectral scan thread
            self._write_cad_config_json()

            # Start hourly summary timer
            self._start_hourly_timer()

        except Exception as e:
            logger.error('WM1303Backend: failed to start lora_pkt_fwd: %s', e)
            raise

    def _restart_pkt_fwd(self) -> None:
        """Restart lora_pkt_fwd subprocess (called by watchdog or manually)."""
        if not self._running:
            return
        self._ensure_active_pktfwd_config()
        logger.warning('WM1303Backend: _restart_pkt_fwd - stopping current process')
        self._stop_pktfwd_process()
        # Hardware settle handled by _start_pktfwd (GPIO reset + TCXO warmup)
        logger.info('WM1303Backend: _restart_pkt_fwd - starting new process')
        try:
            if self._running:
                self._start_pktfwd()
            logger.info('WM1303Backend: _restart_pkt_fwd - pkt_fwd restarted successfully')
        except Exception as e:
            logger.error('WM1303Backend: _restart_pkt_fwd start error: %s', e)
            raise

    def _idle_keepalive_loop(self) -> None:
        """Feed the systemd watchdog while in IDLE mode (no channels configured).

        IDLE mode starts no radio and therefore no RX watchdog thread, but the
        service unit runs with Type=notify + WatchdogSec=60. After READY=1 is
        sent, systemd expects WATCHDOG=1 keepalives; without them it would
        restart the service every WatchdogSec, killing the web UI needed to
        configure the first channels. 5s interval gives ~12x safety margin.
        No-op outside systemd (_sd_notify returns False).
        """
        while self._watchdog_running and self._idle_mode:
            _sd_notify('WATCHDOG=1')
            self._stop_event.wait(5)

    def _watchdog_loop(self) -> None:
        """Monitor RX activity with 4 detection methods and restart pkt_fwd if stuck.

        Detection 1 (STAT): PUSH_DATA stats show rxnb=0 for 2+ consecutive windows (~60s)
        Detection 2 (RSSI): Channel E sees strong RF but SX1302 not receiving (60s)
        Detection 3 (TIMEOUT): Original fallback - no RX for full timeout (180s)
        Detection 4 (PROCESS_EXIT): pkt_fwd subprocess has exited (e.g. C-level L2 watchdog)
        """
        logger.info('WM1303Backend: RX watchdog started (timeout=%ds, '
                    'stat_detect=2 windows, rssi_detect=5 spikes/60s)',
                    self._watchdog_timeout)
        # main announces readiness after the whole service (including HTTP)
        # starts; this loop only supplies watchdog keepalives.
        _cycle_num = 0
        while self._watchdog_running:
            _cycle_num += 1
            logger.info('WM1303Backend: WATCHDOG_DIAG pre-sleep cycle=%d', _cycle_num)
            self._stop_event.wait(5)  # check every 5 seconds; wake immediately at shutdown
            logger.info('WM1303Backend: WATCHDOG_DIAG post-sleep cycle=%d', _cycle_num)
            # Feed the systemd service-level watchdog each cycle (WatchdogSec=60s).
            # The 5s loop gives ~12x safety margin; if this loop hangs, systemd
            # restarts the service.
            _sd_notify('WATCHDOG=1')
            if not self._watchdog_running:
                break

            try:
                elapsed = time.monotonic() - self._last_rx_timestamp
            except Exception as _e:
                logger.error('WM1303Backend: WATCHDOG_DIAG elapsed calc failed: %s', _e)
                continue

            # DIAGNOSTIC: log watchdog cycle state at INFO level
            try:
                _proc_state = 'None' if self._proc is None else 'pid=%s,poll=%s' % (self._proc.pid, self._proc.poll())
            except Exception as _e:
                _proc_state = 'ERR(%s)' % _e
            logger.info('WM1303Backend: WATCHDOG_DIAG cycle=%d proc=%s elapsed=%.0fs '
                        'stat_zero=%d rssi_spikes=%d respawn_total=%d',
                        _cycle_num, _proc_state, elapsed, self._zero_rx_stat_count,
                        self._rssi_spike_count, self._respawn_total)

            # Detection 4: pkt_fwd subprocess has exited (C-level L2 watchdog
            # triggered exit_sig, or pkt_fwd crashed). Must respawn so RX resumes.
            if self._proc is not None and self._proc.poll() is not None:
                exit_code = self._proc.returncode
                now_m = time.monotonic()
                # Drop respawn timestamps older than 1 hour
                self._respawn_times = [t for t in self._respawn_times if now_m - t < 3600]
                if len(self._respawn_times) >= self._respawn_max_per_hour:
                    logger.error(
                        'WM1303Backend: WATCHDOG [PROCESS_EXIT] - pkt_fwd exited '
                        '(code=%s) but respawn rate limit reached (%d/hour); '
                        'NOT restarting to prevent crash loop — manual intervention needed',
                        exit_code, self._respawn_max_per_hour
                    )
                    self._stop_event.wait(60)  # cooldown, interruptible at shutdown
                    continue
                self._respawn_times.append(now_m)
                self._respawn_total += 1
                self._consecutive_crash_count += 1
                _escalate = self._consecutive_crash_count >= self._sx1261_escalation_threshold
                logger.warning(
                    'WM1303Backend: WATCHDOG [PROCESS_EXIT] - pkt_fwd has exited '
                    '(code=%s), respawning (count=%d/%d in last hour, total=%d since start, '
                    'consecutive=%d, escalate=%s)',
                    exit_code, len(self._respawn_times),
                    self._respawn_max_per_hour, self._respawn_total,
                    self._consecutive_crash_count, _escalate
                )
                self._do_watchdog_restart('process_exit', escalate=_escalate)
                continue



            # Detection 5 (ACK_TIMEOUT): consecutive post-TX ACK timeouts indicate
            # C-level JIT thread is stuck (SX1302 stall). Layer 1 correlator reinit
            # runs in C, but if it fails after 3 attempts, Layer 2 restarts pkt_fwd.
            if self._consecutive_ack_timeouts >= self._l2_ack_timeout_threshold:
                logger.warning(
                    'WM1303Backend: WATCHDOG [ACK_TIMEOUT] - %d consecutive post-TX ACK '
                    'timeouts (threshold=%d), restarting pkt_fwd',
                    self._consecutive_ack_timeouts, self._l2_ack_timeout_threshold
                )
                self._consecutive_ack_timeouts = 0
                self._do_watchdog_restart('ack_timeout_l2')
                continue

            # Detection 1: PUSH_DATA stats show no RX for 2+ windows (~60s)
            if self._zero_rx_stat_count >= 999:
                logger.warning(
                    'WM1303Backend: WATCHDOG [STAT] - rxnb=0 for %d stat windows '
                    'with TX active, restarting pkt_fwd',
                    self._zero_rx_stat_count
                )
                self._do_watchdog_restart('stat_monitor')
                continue

            # Detection 2: Channel E sees RF but SX1302 not receiving (60s)
            if elapsed > 60 and self._rssi_spike_count >= 999:
                logger.warning(
                    'WM1303Backend: WATCHDOG [RSSI] - %d RSSI spikes detected by Channel E '
                    'but no RX for %.0fs, restarting pkt_fwd',
                    self._rssi_spike_count, elapsed
                )
                self._do_watchdog_restart('rssi_monitor')
                continue

            # Detection 3: Original fallback - no RX for full timeout (180s)
            if elapsed > self._watchdog_timeout:
                logger.warning(
                    'WM1303Backend: WATCHDOG [TIMEOUT] - No RX for %.0fs '
                    '(timeout=%ds), restarting pkt_fwd',
                    elapsed, self._watchdog_timeout
                )
                self._do_watchdog_restart('timeout')
                continue

            # Periodic RSSI spike window reset (every 120s) to avoid stale counts
            spike_window_age = time.monotonic() - self._rssi_spike_window_start
            if spike_window_age > 120:
                if self._rssi_spike_count > 0:
                    logger.debug('WM1303Backend: WATCHDOG - resetting RSSI spike count '
                                '(%d spikes over %.0fs window)',
                                self._rssi_spike_count, spike_window_age)
                self._rssi_spike_count = 0
                self._rssi_spike_window_start = time.monotonic()

            logger.debug('WM1303Backend: WATCHDOG OK - last_rx=%.0fs ago, '
                        'stat_zero=%d, rssi_spikes=%d',
                        elapsed, self._zero_rx_stat_count, self._rssi_spike_count)
        logger.info('WM1303Backend: RX watchdog stopped')

    def _do_watchdog_restart(self, trigger_reason: str, escalate: bool = False) -> None:
        """Common restart logic for all watchdog triggers.

        Performs a full hardware power-cycle (GPIO drain reset) before
        restarting pkt_fwd to ensure clean SX1302/SX1250/SX1261 state.

        If *escalate* is True (SX1261 hard-stuck after consecutive crashes),
        uses an extended 15s power-off drain via reset_lgw.sh deep_reset
        instead of the standard 3s power_cycle_lgw.sh.

        Issue #11.1: in addition to the explicit escalation path triggered by
        consecutive pkt_fwd CRASHES, this method also auto-escalates when the
        watchdog has just restarted back-to-back without an intervening valid
        RX. This covers hardware-level stalls where pkt_fwd does NOT crash
        (rssi_monitor / stat_monitor / timeout triggers).
        ``_consecutive_watchdog_restarts`` is reset to 0 in the RX handler
        when a valid LoRa packet is received.
        """
        # Issue #11.1: track and auto-escalate non-crash watchdog restarts.
        if not self._running:
            return
        self._consecutive_watchdog_restarts += 1
        if (not escalate
                and self._consecutive_watchdog_restarts >= self._watchdog_escalation_threshold):
            logger.warning(
                'WM1303Backend: WATCHDOG auto-escalation - %d consecutive restarts '
                'without RX recovery, switching to deep_reset (trigger=%s)',
                self._consecutive_watchdog_restarts, trigger_reason
            )
            escalate = True

        logger.warning('WM1303Backend: WATCHDOG restart triggered by: %s (escalate=%s, '
                      'consec_restarts=%d)',
                      trigger_reason, escalate, self._consecutive_watchdog_restarts)
        try:
            self._ensure_active_pktfwd_config()
            # Stop access to SPI before powering down/resetting the hardware.
            self._stop_pktfwd_process()
            if not self._running:
                return
            if escalate:
                # Extended power-off drain to recover SX1261 from hard-stuck state.
                # The SX1261 can latch into status 0x00 after repeated failed starts;
                # only a prolonged power-off (>=10s) clears this condition.
                _reset_script = PKTFWD_DIR / 'reset_lgw.sh'
                if _reset_script.exists():
                    logger.warning('WM1303Backend: WATCHDOG - ESCALATED: running extended '
                                  'deep_reset (15s drain) to recover SX1261')
                    try:
                        _r = subprocess.run(
                            ['sudo', 'bash', str(_reset_script), 'deep_reset', '15'],
                            cwd=str(PKTFWD_DIR), capture_output=True, timeout=60
                        )
                        logger.info('WM1303Backend: WATCHDOG - deep_reset done (rc=%d)', _r.returncode)
                    except Exception as _e:
                        logger.warning('WM1303Backend: WATCHDOG - deep_reset failed: %s', _e)
                else:
                    logger.warning('WM1303Backend: WATCHDOG - reset_lgw.sh not found for escalation')
            else:
                # Standard power-cycle (3s drain) for normal restarts.
                _power_cycle = PKTFWD_DIR / 'power_cycle_lgw.sh'
                if _power_cycle.exists():
                    logger.info('WM1303Backend: WATCHDOG - running hardware power cycle before restart')
                    try:
                        _r = subprocess.run(
                            ['sudo', 'bash', str(_power_cycle)],
                            cwd=str(PKTFWD_DIR), capture_output=True, timeout=30
                        )
                        logger.info('WM1303Backend: WATCHDOG - power cycle done (rc=%d)', _r.returncode)
                    except Exception as _e:
                        logger.warning('WM1303Backend: WATCHDOG - power cycle failed: %s', _e)
                else:
                    logger.info('WM1303Backend: WATCHDOG - power_cycle_lgw.sh not found, using standard reset')

            # Restart pkt_fwd (includes standard GPIO reset + TCXO warmup)
            self._restart_pkt_fwd()
            self._last_rx_timestamp = time.monotonic()
            self._zero_rx_stat_count = 0
            self._rssi_spike_count = 0
            self._rssi_spike_window_start = time.monotonic()
            logger.info('WM1303Backend: WATCHDOG - pkt_fwd restart complete (trigger=%s, escalate=%s)',
                       trigger_reason, escalate)
        except Exception as e:
            logger.error('WM1303Backend: WATCHDOG restart failed (trigger=%s): %s',
                        trigger_reason, e)


    # ------------------------------------------------------------------
    # SX1261 health events (parsed from pkt_fwd stdout, stored in sqlite)
    # ------------------------------------------------------------------

    # Compiled regexes (class-level — compiled once on first use)
    _RE_CAD_TIMEOUT = re.compile(
        r'CAD timeout after\s+(?P<dur>\d+)\s*ms.*?freq=(?P<freq>\d+).*?SF(?P<sf>\d+)',
        re.IGNORECASE,
    )
    _RE_CAD_FORCE = re.compile(
        r'CAD still active after\s+(?P<retries>\d+)\s+retries.*?FORCING\s+TX',
        re.IGNORECASE,
    )
    _RE_LBT_BUSY = re.compile(
        r'LBT RSSI[^A-Za-z]*BUSY.*?freq=(?P<freq>\d+)[^0-9-]*.*?RSSI=(?P<rssi>-?\d+).*?'
        r'(?:thr(?:eshold)?(?:_dbm)?\s*[:=]\s*)(?P<thr>-?\d+)',
        re.IGNORECASE,
    )
    # Fallback LBT regex: HAL variant 'Custom LBT RSSI check BUSY on rf_chain N (freq=X Hz, RSSI=Y dBm, threshold=Z dBm)'
    _RE_LBT_BUSY_ALT = re.compile(
        r'Custom LBT RSSI check BUSY.*?freq=(?P<freq>\d+).*?RSSI=(?P<rssi>-?\d+).*?threshold=(?P<thr>-?\d+)',
        re.IGNORECASE,
    )

    def _record_sx1261_health_event(
        self,
        event_type: str,
        freq_hz: int | None = None,
        rssi_dbm: float | None = None,
        threshold_dbm: float | None = None,
        sf: int | None = None,
        duration_ms: float | None = None,
        details: str | None = None,
    ) -> None:
        """Insert a single row into the sx1261_health_events table.

        Best-effort: never raises. Used by the pkt_fwd stdout parser.
        """
        try:
            _db = self._db_path
            with _db_conn(_db, timeout=2.0) as conn:
                conn.execute(
                    "INSERT INTO sx1261_health_events "
                    "(timestamp, event_type, freq_hz, rssi_dbm, threshold_dbm, sf, duration_ms, details) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (time.time(), event_type, freq_hz, rssi_dbm, threshold_dbm,
                     sf, duration_ms, details),
                )
                conn.commit()
        except Exception as _e:
            logger.debug('WM1303Backend: _record_sx1261_health_event failed: %s', _e)

    def _parse_pkt_fwd_health(self, line_str: str) -> None:
        """Scan a pkt_fwd stdout line for SX1261 health events and persist them.

        Event types recognised:
          - spectral_scan_timeout
          - spectral_scan_status_unexpected
          - sx1261_recovery
          - cad_timeout
          - cad_force_tx
          - lbt_rssi_busy
        """
        try:
            # Cheap pre-filter: most lines won't match any event
            _lower = line_str.lower()
            if not any(k in _lower for k in (
                'spectral scan', 'sx1261 recovery', 'cad timeout',
                'cad still active', 'lbt rssi',
            )):
                return

            # 1) spectral_scan_timeout
            if 'timeout on spectral scan' in _lower:
                self._record_sx1261_health_event(
                    'spectral_scan_timeout', details=line_str[:500])
                return

            # 2) spectral_scan_status_unexpected
            if 'spectral scan status unexpected' in _lower:
                self._record_sx1261_health_event(
                    'spectral_scan_status_unexpected', details=line_str[:500])
                return

            # 3) sx1261_recovery (full or partial)
            if 'performing full sx1261 recovery' in _lower or \
               'performing sx1261 recovery' in _lower:
                self._record_sx1261_health_event(
                    'sx1261_recovery',
                    details=('full' if 'full' in _lower else 'partial'))
                return

            # 4) cad_timeout
            _m = self._RE_CAD_TIMEOUT.search(line_str)
            if _m:
                self._record_sx1261_health_event(
                    'cad_timeout',
                    freq_hz=int(_m.group('freq')),
                    sf=int(_m.group('sf')),
                    duration_ms=float(_m.group('dur')),
                    details=line_str[:500],
                )
                return

            # 5) cad_force_tx
            _m = self._RE_CAD_FORCE.search(line_str)
            if _m:
                self._record_sx1261_health_event(
                    'cad_force_tx',
                    duration_ms=None,
                    details=f"retries={_m.group('retries')}; " + line_str[:400],
                )
                return

            # 6) lbt_rssi_busy (two variants)
            if 'lbt rssi' in _lower and 'busy' in _lower:
                _m = (self._RE_LBT_BUSY.search(line_str) or
                      self._RE_LBT_BUSY_ALT.search(line_str))
                if _m:
                    self._record_sx1261_health_event(
                        'lbt_rssi_busy',
                        freq_hz=int(_m.group('freq')),
                        rssi_dbm=float(_m.group('rssi')),
                        threshold_dbm=float(_m.group('thr')),
                        details=line_str[:500],
                    )
                else:
                    # Log the raw line if we couldn't parse numbers
                    self._record_sx1261_health_event(
                        'lbt_rssi_busy', details=line_str[:500])
                return
        except Exception as _e:
            logger.debug('WM1303Backend: _parse_pkt_fwd_health error: %s', _e)

    def _pktfwd_stdout_reader(self) -> None:
        """Drain lora_pkt_fwd stdout to prevent pipe buffer blocking.

        Stats lines (30s periodic reports) are throttled to every 5 minutes
        at INFO level unless they contain actual TX/RX activity. All other
        stats output goes to DEBUG to reduce log noise (~10k lines/6h).
        """
        logger.info('WM1303Backend: stdout reader thread started')
        _stats_interval = 300  # log stats at INFO every 5 minutes
        _last_stats_info = 0.0
        _stats_keywords = ('##### ', 'JSON up:', 'JSON down:', 'REPORT', 'report',
                           'rxnb', 'txnb', 'ackr', 'dwnb')
        _activity_keywords = ('TX ', 'ERROR', 'WARNING', 'rejec', 'too late',
                              'collision', 'BEACON', 'agc_periodic', 'agc_reload',
                              'L1 recovery', 'L1.5', 'L2', 'correlator', 'stall')
        process = self._proc
        try:
            while process and process.stdout:
                line = process.stdout.readline()
                if not line:
                    break
                if line:
                    line_str = line.strip() if isinstance(line, str) else line.decode(errors='replace').strip()
                    if line_str:
                        # Scan for SX1261 health events and persist to sqlite
                        # (spectral_scan_timeout, cad_timeout, lbt_rssi_busy, ...)
                        self._parse_pkt_fwd_health(line_str)
                        # Priority 1: Lines starting with '# ' are stats summary
                        # lines (e.g. '# TX errors: 0', '# BEACON queued: 0').
                        # These must be throttled BEFORE checking activity keywords
                        # because they can match 'TX ' or 'BEACON' falsely.
                        if line_str.startswith('# '):
                            _now = time.monotonic()
                            if _now - _last_stats_info >= _stats_interval:
                                _last_stats_info = _now
                                logger.info('pkt_fwd: %s', line_str)
                            else:
                                logger.debug('pkt_fwd: %s', line_str)
                        # Priority 2: always log errors, TX events, warnings at INFO
                        elif any(kw in line_str for kw in _activity_keywords):
                            logger.info('pkt_fwd: %s', line_str)
                        # Priority 3: Stats lines: throttle to every 5 min at INFO
                        elif any(kw in line_str for kw in _stats_keywords):
                            _now = time.monotonic()
                            if _now - _last_stats_info >= _stats_interval:
                                _last_stats_info = _now
                                logger.info('pkt_fwd: %s', line_str)
                            else:
                                logger.debug('pkt_fwd: %s', line_str)
                        # Priority 4: PULL/PUSH protocol messages: log at DEBUG (high volume)
                        elif any(kw in line_str for kw in ('PULL', 'PUSH', 'INFO')):
                            logger.debug('pkt_fwd: %s', line_str)
                        else:
                            logger.debug('pkt_fwd: %s', line_str)
        except Exception as e:
            logger.debug('WM1303Backend: stdout reader ended: %s', e)
        logger.info('WM1303Backend: stdout reader thread stopped')


    # ------------------------------------------------------------------
    # UDP listener loop (runs in thread)
    # ------------------------------------------------------------------

    def _recreate_socket(self) -> bool:
        """Recreate the UDP socket after a Bad file descriptor or other OSError.

        Thread-safe via _sock_lock.  Returns True if socket was successfully
        recreated, False otherwise.
        """
        with self._sock_lock:
            if not self._running:
                return False
            logger.warning('WM1303Backend: recreating UDP socket (previous socket broken)')
            # Close old socket if it still exists
            if self._sock:
                try:
                    self._sock.close()
                except Exception:
                    pass
            try:
                self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                self._sock.bind(('0.0.0.0', UDP_PORT_UP))
                self._sock.settimeout(2.0)
                # Note: do NOT reset _pull_addr — pkt_fwd address is still valid
                # pkt_fwd will send PULL_DATA again which will update it if needed
                logger.info('WM1303Backend: UDP socket recreated successfully on port %d',
                           UDP_PORT_UP)
                return True
            except Exception as e:
                logger.error('WM1303Backend: failed to recreate UDP socket: %s', e)
                if self._sock:
                    self._sock.close()
                self._sock = None
                return False

    def _udp_loop(self) -> None:
        logger.info('WM1303Backend: UDP listener thread started')
        while self._running:
            try:
                sock = self._sock
                if sock is None:
                    raise OSError('UDP socket unavailable')
                data, addr = sock.recvfrom(65536)
            except socket.timeout:
                continue
            except OSError as e:
                logger.error('WM1303Backend: UDP socket error in _udp_loop: %s — '
                            'attempting socket recovery', e)
                if not self._running:
                    break
                if self._recreate_socket():
                    logger.info('WM1303Backend: UDP socket recovered, resuming listener')
                    continue
                else:
                    logger.error('WM1303Backend: UDP socket recovery FAILED, '
                                'retrying in 5s')
                    self._stop_event.wait(5)
                    continue
            try:
                self._handle_udp(data, addr)
            except Exception as e:
                logger.exception('WM1303Backend: UDP handler error: %s', e)
        logger.info('WM1303Backend: UDP listener thread exiting')

    def _handle_udp(self, data: bytes, addr: tuple) -> None:
        if len(data) < 4:
            return
        ver, tok_h, tok_l, pkt_type = data[0], data[1], data[2], data[3]
        token = (tok_h << 8) | tok_l

        if pkt_type == PKT_PUSH_DATA:    # RX packet(s)
            self._send_ack(addr, token, PKT_PUSH_ACK)
            if len(data) > 12:
                try:
                    payload_json = json.loads(data[12:].decode())
                    rxpk_list = payload_json.get('rxpk', [])
                    if rxpk_list:
                        logger.info('WM1303Backend: PUSH_DATA with %d rxpk packets', len(rxpk_list))
                    for rxpk in rxpk_list:
                        self._dispatch_rx(rxpk)

                    # Detection Method 1: Parse stat object for rxnb/txnb monitoring
                    stat_obj = payload_json.get('stat')
                    if stat_obj and isinstance(stat_obj, dict):
                        rxnb = stat_obj.get('rxnb', 0)
                        txnb = stat_obj.get('txnb', 0)
                        self._last_stat_rxnb = rxnb
                        self._last_stat_txnb = txnb
                        # Track consecutive zero-RX stat windows
                        # Only count when TX is active (txnb > 0 or recent TX activity)
                        has_tx_activity = txnb > 0 or (time.monotonic() - self._last_tx_end < 60)
                        if rxnb == 0 and has_tx_activity:
                            self._zero_rx_stat_count += 1
                            logger.info('WM1303Backend: PUSH_DATA stat rxnb=0, txnb=%d '
                                       '(zero_count=%d, tx_active=%s)',
                                       txnb, self._zero_rx_stat_count, has_tx_activity)
                        elif rxnb > 0:
                            if self._zero_rx_stat_count > 0:
                                logger.info('WM1303Backend: PUSH_DATA stat rxnb=%d - '
                                           'resetting zero_rx_stat_count from %d',
                                           rxnb, self._zero_rx_stat_count)
                            self._zero_rx_stat_count = 0
                        else:
                            logger.debug('WM1303Backend: PUSH_DATA stat rxnb=%d txnb=%d '
                                        '(no TX activity, not counting)', rxnb, txnb)
                except Exception as e:
                    logger.warning('WM1303Backend: failed to parse PUSH_DATA: %s', e)

        elif pkt_type == PKT_PULL_DATA:  # TX poll
            self._pull_addr = addr
            self._pktfwd_ready_event.set()
            logger.info('WM1303Backend: PULL_DATA received from pkt_fwd at %s', addr)
            self._send_ack(addr, token, PKT_PULL_ACK)

        elif pkt_type == PKT_TX_ACK:     # TX result
            ack_info: dict = {}
            is_post_tx = False
            err = 'NONE'
            if len(data) > 4:
                raw_payload = data[4:]
                # WM1303: the HAL emits TX_ACK with a 12-byte header
                # (4-byte Semtech proto + 8-byte gateway MAC) before the
                # optional JSON body. Older code assumed 4-byte header and
                # silently failed to parse JSON. Find the first '{' to
                # locate the JSON body robustly.
                _brace_idx = raw_payload.find(b'{')
                _json_bytes = raw_payload[_brace_idx:] if _brace_idx >= 0 else b''
                try:
                    if not _json_bytes:
                        raise json.JSONDecodeError('no JSON body', '', 0)
                    ack = json.loads(_json_bytes.decode())
                    txpk_ack = ack.get('txpk_ack', {}) if isinstance(ack, dict) else {}
                    err = txpk_ack.get('error', 'NONE')
                    is_post_tx = (txpk_ack.get('phase') == 'post_tx')
                    if is_post_tx:
                        # WM1303 post-TX ack: carries CAD/LBT outcomes
                        ack_info = {
                            'ok': (err == 'NONE' and txpk_ack.get('tx_result') == 'sent'),
                            'error': err,
                            'tx_result': txpk_ack.get('tx_result', 'unknown'),
                            'phase': 'post_tx',
                            'cad': txpk_ack.get('cad', {}) or {},
                            'lbt': txpk_ack.get('lbt', {}) or {},
                        }
                        # Flatten convenience fields for consumers
                        cad = ack_info['cad']
                        lbt = ack_info['lbt']
                        ack_info.update({
                            'cad_enabled': bool(cad.get('enabled', False)),
                            'cad_detected': bool(cad.get('detected', False)),
                            'cad_retries': int(cad.get('retries', 0)),
                            'tx_noisefloor_dbm': int(cad.get('tx_noisefloor_dbm', 0)) if cad.get('tx_noisefloor_dbm') is not None else None,
                            'cad_reason': cad.get('reason', ''),
                            'lbt_enabled': bool(lbt.get('enabled', False)),
                            'lbt_pass': lbt.get('pass') if isinstance(lbt.get('pass'), bool) else None,
                            'lbt_rssi_dbm': int(lbt.get('rssi_dbm', 0)) if lbt.get('rssi_dbm') is not None else None,
                            'lbt_threshold_dbm': int(lbt.get('threshold_dbm', 0)) if lbt.get('threshold_dbm') is not None else None,
                            'lbt_retries': int(lbt.get('retries', 0)),
                        })
                        logger.info('WM1303Backend: TX_ACK post-TX token=0x%04x '
                                    'tx_result=%s cad[en=%s det=%s r=%d tx_nf=%s reason=%s] '
                                    'lbt[en=%s pass=%s rssi=%s thr=%s r=%d]',
                                    token, ack_info['tx_result'],
                                    ack_info['cad_enabled'], ack_info['cad_detected'],
                                    ack_info['cad_retries'], ack_info['tx_noisefloor_dbm'],
                                    ack_info['cad_reason'],
                                    ack_info['lbt_enabled'], ack_info['lbt_pass'],
                                    ack_info['lbt_rssi_dbm'], ack_info['lbt_threshold_dbm'],
                                    ack_info['lbt_retries'])
                    elif err != 'NONE':
                        logger.warning('WM1303Backend: TX_ACK error: %s (full: %s)', err, ack)
                        # WM1303: For TOO_LATE/COLLISION errors, resolve the pending
                        # future immediately so the caller doesn't wait for a full
                        # timeout. The packet was dropped by JIT, not transmitted.
                        if err:
                            ack_info = {
                                'ok': False,
                                'error': err,
                                'tx_result': ('dropped' if err in ('TOO_LATE', 'COLLISION_PACKET', 'COLLISION_BEACON') else 'error'),
                                'phase': 'post_tx',
                                'cad': {}, 'lbt': {},
                                'cad_enabled': False, 'cad_detected': False,
                                'cad_retries': 0, 'tx_noisefloor_dbm': None,
                                'cad_reason': '', 'lbt_enabled': False,
                                'lbt_pass': None, 'lbt_rssi_dbm': None,
                                'lbt_threshold_dbm': None, 'lbt_retries': 0,
                            }
                            is_post_tx = True  # ensure future is resolved below
                    else:
                        logger.info('WM1303Backend: TX_ACK OK (json)')
                except (UnicodeDecodeError, json.JSONDecodeError):
                    raw_hex = raw_payload.hex()
                    if len(raw_payload) >= 2 and raw_payload[-1] in (0x00, 0x01):
                        logger.info('WM1303Backend: TX_ACK OK (binary: %s)', raw_hex)
                    else:
                        logger.warning('WM1303Backend: TX_ACK unknown binary: %s', raw_hex)
            else:
                logger.info('WM1303Backend: TX_ACK (minimal, %d bytes)', len(data))

            # WM1303: if this is the post-TX ack, resolve the pending future
            # (registered by _send_pull_resp) and/or stash into the short-TTL cache.
            if is_post_tx:
                self._tx_ack_store(token, ack_info)



    # ------------------------------------------------------------------
    # WM1303 post-TX TX_ACK helpers (Approach 3 hybrid)
    # ------------------------------------------------------------------

    def _tx_ack_register_future(self, token: int) -> asyncio.Future:
        """Register a pending-future for a given 16-bit TX_ACK token.

        Must be called from the asyncio loop. Returns a Future that will be
        resolved when the post-TX ACK arrives (from the reader thread) or
        cancelled on timeout by the caller.
        """
        loop = self._loop if self._loop is not None else asyncio.get_event_loop()
        fut = loop.create_future()
        with self._tx_ack_lock:
            self._pending_tx_acks[token & 0xFFFF] = fut
        return fut

    def _tx_ack_unregister_future(self, token: int) -> None:
        """Remove a pending-future registration (called on timeout/cleanup)."""
        with self._tx_ack_lock:
            self._pending_tx_acks.pop(token & 0xFFFF, None)

    def _tx_ack_store(self, token: int, ack_info: dict) -> None:
        """Store a post-TX ack in the short-TTL cache and resolve any pending
        future for the same token.

        Called from the UDP reader thread; uses loop.call_soon_threadsafe
        to safely resolve asyncio futures owned by the backend event loop.
        """
        tok = token & 0xFFFF
        now = time.monotonic()
        fut: asyncio.Future | None = None
        with self._tx_ack_lock:
            # Cache: prune stale entries, enforce max size
            for k in list(self._tx_ack_cache.keys()):
                ts, _ = self._tx_ack_cache[k]
                if now - ts > self._tx_ack_cache_ttl:
                    self._tx_ack_cache.pop(k, None)
                else:
                    break  # OrderedDict is insertion-ordered; older entries are earlier
            while len(self._tx_ack_cache) >= self._tx_ack_cache_max:
                self._tx_ack_cache.popitem(last=False)
            self._tx_ack_cache[tok] = (now, ack_info)
            self._tx_ack_cache.move_to_end(tok)
            # Pending future resolution
            fut = self._pending_tx_acks.pop(tok, None)
        if fut is not None and not fut.done():
            loop = fut.get_loop()
            try:
                loop.call_soon_threadsafe(
                    lambda f=fut, a=ack_info: (not f.done()) and f.set_result(a))
            except RuntimeError:
                # Loop might be closed at shutdown — ignore
                pass

    def tx_ack_get_cached(self, token: int) -> dict | None:
        """Retrieve a cached post-TX ack by token, or None if missing/expired."""
        tok = token & 0xFFFF
        with self._tx_ack_lock:
            entry = self._tx_ack_cache.get(tok)
            if entry is None:
                return None
            ts, info = entry
            if time.monotonic() - ts > self._tx_ack_cache_ttl:
                self._tx_ack_cache.pop(tok, None)
                return None
            return info

    def _send_ack(self, addr: tuple, token: int, ack_type: int) -> None:
        pkt = bytes([PROTOCOL_VER, (token >> 8) & 0xFF, token & 0xFF, ack_type])
        try:
            self._sock.sendto(pkt, addr)
        except Exception as e:
            logger.debug('WM1303Backend: sendto failed: %s', e)

    def _dispatch_rx(self, rxpk: dict) -> None:
        """Route a received packet to the matching VirtualLoRaRadio."""
        _rx_freq = rxpk.get('freq', 0)
        _rx_datr = rxpk.get('datr', '?')
        _rx_rssi = rxpk.get('rssi', '?')
        _rx_stat = rxpk.get('stat', -1)
        # CRC error = -1, CRC disabled = 0, CRC OK = 1
        _crc_label = {1: 'CRC_OK', 0: 'CRC_DISABLED', -1: 'CRC_ERROR'}.get(_rx_stat, f'CRC_{_rx_stat}')
        # Hourly summary counters
        with self._hourly_lock:
            self._hourly_rx_total += 1
            if _rx_stat == 1:
                self._hourly_rx_crc_ok += 1
            elif _rx_stat == -1:
                self._hourly_rx_crc_err += 1
        _rx_lsnr = rxpk.get('lsnr', '?')
        _raw_data = rxpk.get('data', '')
        _raw_size = len(_raw_data)
        # Per-channel CRC error rate counter (ALL errors/disabled, no filtering)
        if _rx_stat != 1:
            _rate_freq_hz = int(float(_rx_freq) * 1e6)
            _rate_ch = self._freq_to_ch_id.get(_rate_freq_hz)
            if _rate_ch is None:
                for _rk, _rv in self._freq_to_ch_id.items():
                    if abs(_rk - _rate_freq_hz) <= 1000:
                        _rate_ch = _rv
                        break
            _rate_ch = _rate_ch or 'unknown'
            _rate_key = 'crc_error' if _rx_stat == -1 else 'crc_disabled'
            with self._crc_rate_lock:
                if _rate_ch not in self._crc_rate_counters:
                    self._crc_rate_counters[_rate_ch] = {'crc_error': 0, 'crc_disabled': 0}
                self._crc_rate_counters[_rate_ch][_rate_key] += 1
        if _rx_stat != 1:  # Not CRC OK - log as WARNING with hex for analysis
            try:
                import base64 as _b64
                _raw_bytes = _b64.b64decode(_raw_data) if _raw_data else b''
                _hex_preview = _raw_bytes[:16].hex() if _raw_bytes else ''
            except Exception:
                _hex_preview = '?'
            logger.warning('WM1303Backend: RX %s freq=%.3f datr=%s rssi=%s snr=%s size=%d hex=%s',
                          _crc_label, _rx_freq, _rx_datr, _rx_rssi, _rx_lsnr, _raw_size, _hex_preview)
            # Store CRC_ERROR / CRC_DISABLED as a packet_metric row so the
            # Spectrum tab can compute error ratios per channel over time.
            # Skip tiny fragments to avoid polluting charts with noise.
            try:
                # Compute RSSI filter: exclude spectrum-scan noise and sub-threshold detections.
                # Real RX CRC errors have RSSI above the receiver sensitivity threshold (~-120 dBm).
                # Below -115 dBm is almost certainly scan-mode noise, not a real RX attempt.
                try:
                    _rssi_check = float(_rx_rssi) if _rx_rssi not in (None, '?') else None
                except (ValueError, TypeError):
                    _rssi_check = None
                _rssi_ok_for_crc = (_rssi_check is None) or (_rssi_check >= -115.0)
                if _rx_stat == -1 and _raw_size >= 5 and _rssi_ok_for_crc:
                    _freq_hz_key = int(float(_rx_freq) * 1e6)
                    _crc_ch = self._freq_to_ch_id.get(_freq_hz_key)
                    if _crc_ch is None:
                        # Round to nearest kHz to tolerate tiny drift
                        for _k, _v in self._freq_to_ch_id.items():
                            if abs(_k - _freq_hz_key) <= 1000:
                                _crc_ch = _v
                                break
                    if _crc_ch:
                        from repeater.bridge_engine import _active_bridge as _ab
                        _h = getattr(_ab, '_sqlite_handler', None) if _ab else None
                        if _h is not None:
                            try:
                                _rssi_f = float(_rx_rssi) if _rx_rssi not in (None, '?') else None
                            except Exception:
                                _rssi_f = None
                            try:
                                _snr_f = float(_rx_lsnr) if _rx_lsnr not in (None, '?') else None
                            except Exception:
                                _snr_f = None
                            _h.store_packet_metric({
                                'timestamp': time.time(),
                                'channel_id': str(_crc_ch),
                                'direction': 'rx',
                                'length': int(_raw_size),
                                'hop_count': None,
                                'crc_ok': False,
                                'rssi': _rssi_f,
                                'snr': _snr_f,
                            })
            except Exception as _pm_crc_e:
                logger.debug('packet_metric CRC store failed: %s', _pm_crc_e)
        else:
            logger.info('WM1303Backend: RX %s freq=%.3f datr=%s rssi=%s snr=%s size=%d',
                        _crc_label, _rx_freq, _rx_datr, _rx_rssi, _rx_lsnr, _raw_size)
        # AGC settling noise filter: CRC_DISABLED with SNR < -5 dB = settling artifact
        if _rx_stat == 0:  # CRC_DISABLED
            try:
                _snr_check = float(_rx_lsnr) if _rx_lsnr != '?' else -99.0
            except (ValueError, TypeError):
                _snr_check = -99.0
            if _snr_check < -5.0:
                logger.debug('WM1303Backend: CRC_DISABLED noise filtered snr=%.1f (AGC settling)', _snr_check)
                return
        try:
            freq_hz = int(float(rxpk.get('freq', 0)) * 1e6)
            datr = rxpk.get('datr', 'SF7BW125')
            rx_sf, rx_bw = _parse_datr(datr)
            if rx_sf is None:
                # FSK packet or unrecognized modulation - log and skip LoRa processing
                with self._hourly_lock:
                    self._hourly_fsk_skipped += 1
                logger.debug('WM1303Backend: non-LoRa RX (datr=%s) freq=%.3f - skipping channel dispatch', datr, _rx_freq)
                return
            payload_b64 = rxpk.get('data', '')
            if not payload_b64:
                return
            payload = base64.b64decode(payload_b64)

            # ── Minimum packet size filter ─────────────────────────────────
            # Real MeshCore packets are ≥ 5 bytes (header + path_raw + payload).
            # Tiny CRC_ERROR fragments (2-4 bytes) from noise/interference flood
            # the bridge and cause feedback loops.  Discard them early.
            _MIN_MC_PACKET = 5
            if len(payload) < _MIN_MC_PACKET:
                logger.debug('WM1303Backend: noise filter: discarding %d-byte packet '
                             '(min=%d) freq=%.3f rssi=%s snr=%s',
                             len(payload), _MIN_MC_PACKET, _rx_freq, _rx_rssi, _rx_lsnr)
                with self._hourly_lock:
                    self._hourly_noise_discarded = getattr(self, '_hourly_noise_discarded', 0) + 1
                return

            # ── Layer 2 — central MeshCore protocol validator (pyMC_WM1303) ──
            # Drops malformed packets (reserved hash_size=4 spammer, invalid
            # route_type, truncated, length-implausible, etc.) BEFORE any
            # consumer (raw_rx_callbacks, rx_callback/Dispatcher, virtual_radios,
            # MQTT publish). Prevents mc-radar fantom-source accumulation.
            # Forensics persisted to invalid_packets table via registered cb.
            # Fire-and-forget: validator error NEVER blocks the RX path
            # (project design principle: RX availability is #1).
            #
            # Channel resolution: match freq+SF against virtual_radios (A-D) and
            # channel_e cache to record the friendly channel_id (e.g. "channel_a",
            # "channel_e"). Fallback: "<freq>MHz <datr>" for unmatched noise.
            _l2_channel = None
            try:
                for _l2_cid, _l2_radio in self.virtual_radios.items():
                    _l2_ccfg = _l2_radio.channel_config
                    _l2_ch_freq = int(_l2_ccfg.get('frequency', 0))
                    _l2_ch_sf = int(_l2_ccfg.get('spreading_factor', 0))
                    if abs(freq_hz - _l2_ch_freq) <= 50000 and (_l2_ch_sf == 0 or rx_sf == _l2_ch_sf):
                        _l2_channel = _l2_cid
                        break
                if _l2_channel is None and hasattr(self, '_load_channel_e_cache'):
                    _l2_ce_freq = self._load_channel_e_cache()
                    if _l2_ce_freq and abs(freq_hz - _l2_ce_freq) <= 100000:
                        _l2_channel = 'channel_e'
            except Exception:
                _l2_channel = None
            if _l2_channel is None:
                _l2_channel = '%.3fMHz %s' % (float(_rx_freq), datr)
            try:
                from repeater.protocol_validator import validate_and_record
                _l2_rssi = float(_rx_rssi) if isinstance(_rx_rssi, (int, float)) else None
                _l2_snr = float(_rx_lsnr) if isinstance(_rx_lsnr, (int, float)) else None
                _l2_result = validate_and_record(
                    payload,
                    channel=_l2_channel,
                    rssi=_l2_rssi,
                    snr=_l2_snr)
                if not _l2_result.is_valid:
                    self._hourly_protocol_violation = getattr(
                        self, '_hourly_protocol_violation', 0) + 1
                    logger.warning(
                        'WM1303Backend: dropping invalid RX (%d bytes, ch=%s, freq=%s) '
                        '\u2014 protocol violation: %s',
                        len(payload), _l2_channel, _rx_freq, _l2_result.reason)
                    return  # block before any downstream consumer
            except Exception as _l2_err:
                logger.debug(
                    'WM1303Backend: protocol_validator skipped (non-fatal): %s',
                    _l2_err)

            # ── Pre-filter raw RX push to companion frame servers ──────────
            # Fire the BridgeEngine's raw RX callbacks BEFORE echo/dedup
            # filtering so companion apps receive ALL RF packets, including
            # TX echoes (= "Heard Repeats") and mesh echoes (= neighbour
            # retransmissions of our packets = node discovery).
            # This is purely informational (0x88 PUSH_CODE_LOG_RX_DATA) and
            # does not interfere with the bridge forwarding logic.
            try:
                from repeater.bridge_engine import _active_bridge
                if _active_bridge and hasattr(_active_bridge, '_fire_raw_rx_callbacks'):
                    _be_rssi = float(_rx_rssi) if _rx_rssi is not None else -120.0
                    _be_snr = float(_rx_lsnr) if _rx_lsnr is not None else 0.0
                    _active_bridge._fire_raw_rx_callbacks(
                        payload, getattr(self, 'channel_id', 'channel_e'),
                        _be_rssi, _be_snr)
            except Exception as _pre_cb_err:
                logger.debug('WM1303Backend: pre-filter raw RX callback error: %s', _pre_cb_err)

            # Self-echo detection: check if this RX matches a recent TX
            # Use stable payload hash (excludes path data that changes per hop)
            _rx_echo_hash = packet_hash(payload)
            _now_mono = time.monotonic()
            if _rx_echo_hash in self._tx_echo_hashes:
                _tx_time = self._tx_echo_hashes[_rx_echo_hash]
                _age = _now_mono - _tx_time
                if _age < self._tx_echo_ttl:
                    # Path-based echo classification (v2.5.x):
                    # Distinguish RF self-echo from neighbour mesh-echo by inspecting
                    # the packet's path field, NOT by time. Each repeater has a unique
                    # path_hash derived from its public key, so this works correctly
                    # on any repeater regardless of mesh latency.
                    _echo_kind = self._classify_echo(payload)
                    if _echo_kind == 'self_echo':
                        self._tx_echo_detected += 1
                        _echo_label = 'Self-echo'
                        _echo_desc = 'RF feedback / own TX heard back'
                        _echo_count = self._tx_echo_detected
                    elif _echo_kind == 'mesh_echo':
                        if not hasattr(self, '_tx_mesh_echo_detected'):
                            self._tx_mesh_echo_detected = 0
                        self._tx_mesh_echo_detected += 1
                        _echo_label = 'Mesh-echo'
                        _echo_desc = 'neighbour repeater retransmitted our packet'
                        _echo_count = self._tx_mesh_echo_detected
                    else:  # 'unknown_echo'
                        if not hasattr(self, '_tx_unknown_echo_detected'):
                            self._tx_unknown_echo_detected = 0
                        self._tx_unknown_echo_detected += 1
                        _echo_label = 'Unknown-echo'
                        _echo_desc = 'content hash matched recent TX but path/src did not confirm origin'
                        _echo_count = self._tx_unknown_echo_detected
                    # Decide action: self_echo is true RF feedback and must
                    # be discarded.  mesh_echo / unknown_echo are packets
                    # retransmitted by neighbour repeaters — they MUST be
                    # forwarded so companion servers receive 'repeat heard'
                    # confirmations and the bridge engine can deliver them.
                    _discard = (_echo_kind == 'self_echo')
                    _action_label = 'DISCARDING' if _discard else 'FORWARDING'
                    logger.warning('WM1303Backend: %s detected! hash=%s age=%.1fs rssi=%s '
                                   'freq=%.3f (total_%ses=%d) - %s',
                                   _echo_label, _rx_echo_hash, _age, _rx_rssi, _rx_freq,
                                   _echo_kind, _echo_count, _action_label)
                    # Emit trace step so the UI shows the classification.
                    # Use distinct step names so the UI can render them differently.
                    try:
                        from repeater.web.packet_trace import trace_event as _trace_ev
                        _trace_ev(_rx_echo_hash[:8], _echo_kind,
                                  channel=str(_rx_freq),
                                  detail='%s (%s, age=%.1fs, rssi=%s, freq=%.3f) - %s' % (_echo_label, _echo_desc, _age, _rx_rssi, _rx_freq, _action_label),
                                  status='ok')
                    except Exception:
                        pass
                    # Record for dedup visualization (separate kinds)
                    try:
                        from repeater.bridge_engine import _active_bridge
                        if _active_bridge:
                            if _echo_kind == 'self_echo':
                                _dd_kind = 'hal_tx_echo'
                            elif _echo_kind == 'mesh_echo':
                                _dd_kind = 'hal_mesh_echo'
                            else:
                                _dd_kind = 'hal_unknown_echo'
                            _active_bridge._record_dedup_event(_dd_kind, 'HAL',
                                                               _rx_echo_hash, len(payload),
                                                               self._get_packet_type_name(payload) if hasattr(self, '_get_packet_type_name') else '')
                    except Exception:
                        pass
                    if _discard:
                        return
                    # mesh_echo / unknown_echo: fall through to normal RX
                    # processing so the bridge engine and companion servers
                    # receive the retransmitted packet.
                else:
                    del self._tx_echo_hashes[_rx_echo_hash]
            # Multi-demod dedup: prevent 8x TX for same packet (stable hash)
            _dd_hash = packet_hash(payload)
            _dd_now = time.monotonic()
            if _dd_hash in self._rx_dedup_cache:
                if _dd_now - self._rx_dedup_cache[_dd_hash] < 2.0:
                    logger.debug('WM1303Backend: multi-demod dup %s', _dd_hash)
                    # Emit trace step so the UI shows why no further processing happens
                    try:
                        from repeater.web.packet_trace import trace_event as _trace_ev
                        _trace_ev(_dd_hash[:8], 'echo_dedup',
                                  detail='Multi-demod duplicate - DISCARDED',
                                  status='ok')
                    except Exception:
                        pass
                    # Record for dedup visualization
                    try:
                        from repeater.bridge_engine import _active_bridge
                        if _active_bridge:
                            _active_bridge._record_dedup_event('multi_demod', 'HAL',
                                                               _dd_hash, len(payload),
                                                               self._get_packet_type_name(payload) if hasattr(self, '_get_packet_type_name') else '')
                    except Exception:
                        pass
                    return
            self._rx_dedup_cache[_dd_hash] = _dd_now
            if len(self._rx_dedup_cache) > 100:
                self._rx_dedup_cache = {k:v for k,v in self._rx_dedup_cache.items() if _dd_now-v < 5.0}
            rssi = float(rxpk.get('rssi', -99))
            snr = float(rxpk.get('lsnr', 0))
        except Exception as e:
            logger.warning('WM1303Backend: rxpk parse error: %s', e)
            return

        # --- Dispatcher RX callback (dev-branch interface) ---
        self._last_pkt_rssi = int(rssi)
        self._last_pkt_snr = float(snr)
        if self.rx_callback is not None and self._loop is not None:
            try:
                self._loop.call_soon_threadsafe(self.rx_callback, payload, int(rssi), float(snr))
            except Exception as _cb_err:
                logger.warning("WM1303Backend: rx_callback error: %s", _cb_err)

        matched = False
        freq_only_match = None
        for cid, radio in self.virtual_radios.items():
            ccfg = radio.channel_config
            ch_freq = int(ccfg.get('frequency', 0))
            ch_sf   = int(ccfg.get('spreading_factor', 0))
            ch_bw = int(ccfg.get('bandwidth', 125000))
            freq_delta = abs(freq_hz - ch_freq)
            if (ccfg.get('active', True) and freq_delta <= 1000
                    and rx_bw == ch_bw and (ch_sf == 0 or rx_sf == ch_sf)):
                logger.info('WM1303Backend: RX->%s freq=%d SF%d %d bytes rssi=%.1f snr=%.1f',
                             cid, freq_hz, rx_sf, len(payload), rssi, snr)
                radio.enqueue_rx(payload, rssi=int(rssi), snr=snr)
                self._last_rx_timestamp = time.monotonic()  # watchdog: RX activity
                self._zero_rx_stat_count = 0  # reset stat monitor on valid RX
                self._rssi_spike_count = 0    # reset RSSI spike monitor on valid RX
                self._consecutive_crash_count = 0  # reset SX1261 escalation on valid RX
                self._consecutive_watchdog_restarts = 0  # Issue #11.1: reset auto-escalation on valid RX
                self._update_rx_stats(cid, freq_hz, rssi, snr)
                # TX batch hold: dynamic delay based on queue depth
                _dynamic_hold = self._calculate_dynamic_tx_hold()
                if _dynamic_hold > 0:
                    _hold_until = time.monotonic() + _dynamic_hold
                    if _hold_until > self._tx_hold_until:
                        self._tx_hold_until = _hold_until
                        logger.info('WM1303Backend: TX hold set for %.0fms '
                                   '(dynamic, %d pending)',
                                   _dynamic_hold * 1000,
                                   self._get_total_tx_pending())
                else:
                    # No hold needed (queue empty) - clear any existing hold
                    self._tx_hold_until = 0.0
                matched = True
                break
            elif freq_delta <= 1000 and rx_bw == ch_bw and freq_only_match is None:
                freq_only_match = (cid, radio, ch_sf)
            else:
                logger.debug('WM1303Backend: no match %s: ch_freq=%d(delta=%d) ch_sf=%d rx_sf=%s',
                            cid, ch_freq, freq_delta, ch_sf, rx_sf)

        if not matched and freq_only_match:
            cid, radio, ch_sf = freq_only_match
            logger.info('WM1303Backend: RX SF-MISMATCH on %s (ch_sf=SF%d, rx_sf=SF%s) '
                        'freq=%d %d bytes rssi=%.1f - NOT routed to bridge (SF filter)',
                        cid, ch_sf, rx_sf, freq_hz, len(payload), rssi)
            # Count SF-mismatched packets for hourly stats
            with self._hourly_lock:
                self._hourly_sf_mismatch = getattr(self, '_hourly_sf_mismatch', 0) + 1
            # Still update RX timestamp for watchdog (radio IS receiving)
            self._last_rx_timestamp = time.monotonic()
            self._zero_rx_stat_count = 0

        # --- Channel E (channel_e) RX injection ---
        if not matched and hasattr(self, "_channel_e_rx_callback") and self._channel_e_rx_callback is not None:
            _channel_e_freq = self._load_channel_e_cache()
            _che = self._channel_e_config_cache
            if (_che.get('enabled', True) and _channel_e_freq
                    and abs(freq_hz - _channel_e_freq) <= 1000
                    and rx_bw == int(_che.get('bandwidth', 62500))
                    and rx_sf == int(_che.get('spreading_factor', 8))):
                try:
                    self._channel_e_rx_callback(payload, rssi=int(rssi), snr=snr)
                    self._update_rx_stats("channel_e", freq_hz, rssi, snr)
                    logger.info("WM1303Backend: RX->channel_e freq=%d %d bytes rssi=%.1f snr=%.1f",
                                freq_hz, len(payload), rssi, snr)
                    self._last_rx_timestamp = time.monotonic()
                    self._zero_rx_stat_count = 0
                    matched = True
                except Exception as _channel_e_err:
                    logger.warning("WM1303Backend: channel_e rx_callback error: %s", _channel_e_err)
        # --- End Channel E injection ---

        # --- Channel F (chan_Lora_std on RF0) RX injection ---
        # Channel F shares the HAL pkt_fwd UDP port with channels A-D, so we
        # match by frequency + bandwidth + spreading factor (all three must
        # match the channel_f config in wm1303_ui.json). This avoids false
        # positives where an A-D BW125 packet on the same frequency would
        # otherwise leak into the channel_f bridge.
        if not matched and hasattr(self, "_channel_f_rx_callback") and self._channel_f_rx_callback is not None:
            _chf_enabled, _chf_freq, _chf_bw, _chf_sf = self._load_channel_f_cache()
            try:
                _rx_bw_int = int(rx_bw) if rx_bw else 0
            except Exception:
                _rx_bw_int = 0
            if (_chf_enabled and _chf_freq and _chf_bw and _chf_sf
                    and abs(freq_hz - _chf_freq) <= 1000
                    and _rx_bw_int == _chf_bw
                    and (rx_sf == 0 or rx_sf == _chf_sf)):
                try:
                    self._channel_f_rx_callback(payload, rssi=int(rssi), snr=snr)
                    self._update_rx_stats("channel_f", freq_hz, rssi, snr)
                    logger.info("WM1303Backend: RX->channel_f freq=%d SF%d BW%dkHz %d bytes rssi=%.1f snr=%.1f",
                                freq_hz, rx_sf or _chf_sf, _chf_bw // 1000,
                                len(payload), rssi, snr)
                    self._last_rx_timestamp = time.monotonic()
                    self._zero_rx_stat_count = 0
                    matched = True
                except Exception as _channel_f_err:
                    logger.warning("WM1303Backend: channel_f rx_callback error: %s", _channel_f_err)
        # --- End Channel F injection ---

        if not matched:
            logger.info('WM1303Backend: no channel match for freq=%d SF%s '
                        '(channels: %s)',
                        freq_hz, rx_sf,
                        {c: (int(r.channel_config.get('frequency',0)),
                             int(r.channel_config.get('spreading_factor',0)))
                         for c, r in self.virtual_radios.items()})

    # ------------------------------------------------------------------
    # TX via direct PULL_RESP (through RF0)
    # ------------------------------------------------------------------

    async def send(self, channel_id_or_data, data: bytes = None, tx_power: int = None,
                   trace_hash: str = None) -> dict | None:
        """Send a packet on the specified channel via GlobalTXScheduler.

        Supports two calling conventions:
        - send(channel_id: str, data: bytes, tx_power=None)  # channel-based (bridge engine)
        - send(data: bytes)  # base LoRaRadio interface (Dispatcher)

        Enqueues to the per-channel TXQueue. The GlobalTXScheduler
        handles actual transmission in round-robin order.
        """
        # Detect calling convention: if first arg is bytes, it's the Dispatcher interface
        dispatcher_call = isinstance(channel_id_or_data, (bytes, bytearray))
        if not self._running:
            return None if dispatcher_call else {'ok': False, 'error': 'backend_stopped'}
        if dispatcher_call:
            data = channel_id_or_data
            # Pick the first active channel as default
            channel_id = None
            for cid, cfg in self.channels.items():
                if cfg.get('active', True) and cfg.get('tx_enabled', cfg.get('tx_enable', True)):
                    channel_id = cid
                    break
            if channel_id is None:
                # Dedicated E/F queues may be the only active RF endpoints.
                queues = self._tx_queue_manager.queues if self._tx_queue_manager else {}
                channel_id = next(iter(queues), None)
            if channel_id is None:
                logger.warning('WM1303Backend: send() called but no channels configured')
                return None
            logger.debug('WM1303Backend: Dispatcher send() -> channel %s', channel_id)
        else:
            channel_id = channel_id_or_data

        queues = self._tx_queue_manager.queues if self._tx_queue_manager else {}
        if channel_id not in self.channels and channel_id not in queues:
            raise ValueError(f'Unknown channel: {channel_id}')
        cfg = self.channels.get(channel_id, {})
        if not cfg.get('active', True) or not cfg.get('tx_enabled', cfg.get('tx_enable', True)):
            result = {'ok': False, 'error': 'channel_disabled'}
            return None if dispatcher_call else result
        if tx_power is None:
            tx_power = queues[channel_id].tx_power if channel_id in queues else int(cfg.get('tx_power', 14))

        # Enqueue to the per-channel TXQueue (GlobalTXScheduler handles sending)
        if self._tx_queue_manager:
            result = await self._tx_queue_manager.enqueue(channel_id, data, tx_power,
                                                           trace_hash=trace_hash)
        else:
            logger.warning('WM1303Backend: No TX queue manager, sending directly')
            txpk = self._build_txpk(cfg, data, tx_power)
            result = await self._send_pull_resp(txpk, channel_id=channel_id, trace_hash=trace_hash)
            self._record_tx_outcome(result)

        if result.get('ok'):
            logger.info('WM1303Backend: TX on %s (%d bytes) via GlobalTXScheduler',
                       channel_id, len(data))
        else:
            logger.warning('WM1303Backend: TX failed on %s: %s', channel_id, result)

        return None if dispatcher_call and not result.get('ok') else result

    def _record_tx_outcome(self, result: dict) -> None:
        """Count radio attempts once, including E/F's direct queue submissions."""
        with self._hourly_lock:
            if result.get('ok'):
                self._tx_packets_sent_total += 1
                self._hourly_tx_ok += 1
            elif result.get('tx_result') == 'blocked' and (
                result.get('cad_enabled') or
                (result.get('lbt_enabled') and result.get('lbt_pass') is False)
            ):
                self._hourly_tx_lbt_block += 1
            else:
                self._hourly_tx_fail += 1

    async def _send_for_scheduler(self, txpk: dict, channel_id: str,
                                  trace_hash: str = None) -> dict:
        """Send a single txpk via PULL_RESP. Called by GlobalTXScheduler.

        After the send completes and the post-TX TX_ACK has been merged into
        the result (see _send_pull_resp), forward any HW CAD outcome to the
        TX queue manager so per-channel stats (cad_clear, cad_hw_clear, etc.)
        reflect hardware CAD activity — even when SW LBT/CAD is disabled.
        """
        result = await self._send_pull_resp(txpk, channel_id=channel_id,
                                            trace_hash=trace_hash)
        self._record_tx_outcome(result)
        # Fix (Bug 1 / HW CAD counters): when the HAL C code ran HW CAD before
        # this TX, the post-TX TX_ACK carries its outcome. Flow it into the
        # per-channel queue stats so cad_events recorder and the UI see it.
        try:
            if self._tx_queue_manager and isinstance(result, dict) and 'cad_enabled' in result:
                self._tx_queue_manager.record_hw_cad_result(channel_id, {
                    'enabled': bool(result.get('cad_enabled', False)),
                    'detected': bool(result.get('cad_detected', False)),
                    'reason': result.get('cad_reason', ''),
                })
        except Exception as _e:
            logger.debug('WM1303Backend: record_hw_cad_result error: %s', _e)
        # Fix (Bug d / LBT counters): forward LBT outcome from post-TX TX_ACK
        # into per-channel queue stats (lbt_blocked/lbt_passed/lbt_skipped).
        # Without this hook, channel_stats.json shows zeros even though the
        # HAL C code is actively blocking TX on LBT busy.
        try:
            if self._tx_queue_manager and isinstance(result, dict) and 'lbt_enabled' in result:
                self._tx_queue_manager.record_lbt_result(channel_id, {
                    'enabled': bool(result.get('lbt_enabled', False)),
                    'pass': result.get('lbt_pass'),
                    'rssi_dbm': result.get('lbt_rssi_dbm'),
                    'threshold_dbm': result.get('lbt_threshold_dbm'),
                })
        except Exception as _e:
            logger.debug('WM1303Backend: record_lbt_result error: %s', _e)
        return result

    def _update_tx_stats(self, channel_id: str, send_ms: float,
                         airtime_ms: float, wait_ms: float,
                         payload_len: int) -> None:
        """Update per-channel TX timing statistics."""
        if channel_id not in self._channel_tx_stats:
            self._channel_tx_stats[channel_id] = {
                "tx_packets": 0,
                "tx_bytes": 0,
                "total_airtime_ms": 0.0,
                "total_send_ms": 0.0,
                "total_wait_ms": 0.0,
                "last_airtime_ms": 0.0,
                "last_send_ms": 0.0,
                "last_wait_ms": 0.0,
                "avg_airtime_ms": 0.0,
                "avg_send_ms": 0.0,
                "avg_wait_ms": 0.0,
                "last_tx": None,
                "_airtime_history": [],
                "_send_history": [],
                "_wait_history": [],
            }
        s = self._channel_tx_stats[channel_id]
        s["tx_packets"] += 1
        s["tx_bytes"] += payload_len
        s["total_airtime_ms"] = round(s["total_airtime_ms"] + airtime_ms, 1)
        s["total_send_ms"] = round(s["total_send_ms"] + send_ms, 1)
        s["total_wait_ms"] = round(s["total_wait_ms"] + wait_ms, 1)
        s["last_airtime_ms"] = round(airtime_ms, 1)
        s["last_send_ms"] = round(send_ms, 1)
        s["last_wait_ms"] = round(wait_ms, 1)
        s["last_tx"] = time.time()
        # Rolling averages (last 50)
        for key, val, hist_key in [
            ("avg_airtime_ms", airtime_ms, "_airtime_history"),
            ("avg_send_ms", send_ms, "_send_history"),
            ("avg_wait_ms", wait_ms, "_wait_history"),
        ]:
            s[hist_key].append(val)
            if len(s[hist_key]) > 50:
                s[hist_key] = s[hist_key][-50:]
            s[key] = round(sum(s[hist_key]) / len(s[hist_key]), 1)

    def _build_txpk(self, cfg: dict, data: bytes,
                    tx_power: int = 14) -> dict:
        """Build a txpk JSON object for PULL_RESP."""
        freq_mhz  = int(cfg.get('frequency', 869462500)) / 1e6
        sf        = int(cfg.get('spreading_factor', 8))
        bw_hz     = int(cfg.get('bandwidth', 125000))
        cr_raw    = cfg.get('coding_rate', '4/5')
        cr = int(cr_raw.split('/')[-1]) if isinstance(cr_raw, str) else int(cr_raw)
        cr = cr + 4 if 1 <= cr <= 4 else cr
        if cr not in (5, 6, 7, 8):
            raise ValueError(f'Unsupported coding rate: {cr_raw!r}')
        cr_str = f'4/{cr}'
        preamble  = int(cfg.get('preamble_length', 17))
        datr      = _datr_str(sf, bw_hz)
        payload_b64 = base64.b64encode(data).decode()

        return {
            'imme': True,
            'freq': round(freq_mhz, 6),
            'rfch': 0,  # RF0 = TX chain (SKY66420 FEM with PA)
            'powe': tx_power,
            'modu': 'LORA',
            'datr': datr,
            'codr': cr_str,
            'ipol': False,
            'size': len(data),
            'data': payload_b64,
            'prea': preamble,
            'ncrc': False,
        }

    @staticmethod
    def _lora_airtime_s(sf: int, bw_hz: int, payload_len: int,
                        preamble: int = 17, cr: int = 5,
                        explicit_header: bool = True, crc: bool = True) -> float:
        """Calculate LoRa time-on-air in seconds."""
        return estimate_lora_airtime_ms(
            payload_len, sf=sf, bw_hz=bw_hz, cr=cr, preamble=preamble,
            explicit_header=explicit_header, crc_on=crc) / 1000.0




    async def _send_pull_resp(self, txpk: dict, channel_id: str = '',
                              trace_hash: str = None) -> dict:
        """Send a PULL_RESP packet to lora_pkt_fwd (async, cancellable).

        Uses asyncio.Lock and asyncio.sleep so that cancellation (e.g. from
        asyncio.wait_for timeout) immediately releases resources — no zombie
        threads, no stuck locks.

        Includes socket auto-recovery: if sendto fails with OSError (e.g. bad
        file descriptor after pkt_fwd restart), the socket is recreated and
        the send is retried once.

        Returns dict with:
          ok: bool
          send_ms: wall-clock time of UDP sendto only (NOT including airtime wait)
          airtime_ms: calculated LoRa time-on-air
        """
        if not self._running:
            return {'ok': False, 'error': 'backend_stopped', 'send_ms': 0}
        if self._pull_addr is None:
            logger.warning('WM1303Backend: PULL_RESP BLOCKED: _pull_addr is None — '
                          'lora_pkt_fwd has not sent PULL_DATA yet!')
            return {'error': 'no_pull_addr', 'ok': False, 'send_ms': 0}
        async with self._tx_lock:
            # Wait for previous TX to finish (airtime-based delay)
            now = time.monotonic()
            _airtime_wait_ms = 0.0
            if self._last_tx_end > now:
                wait_s = self._last_tx_end - now
                _airtime_wait_ms = round(wait_s * 1000, 1)
                logger.info('WM1303Backend: waiting %.1fms for previous TX airtime to clear',
                           _airtime_wait_ms)
                # Emit rf_guard trace event if wait is noticeable (>20ms).
                # Shows the user why this packet was delayed.
                if trace_hash and _airtime_wait_ms > 20:
                    _trace(trace_hash, 'rf_guard', channel=channel_id,
                           detail='Waiting for RF chain\n  Remaining: %.1f ms'
                                  % _airtime_wait_ms,
                           status='ok')
                await asyncio.sleep(wait_s)

            if not self._running or self._pull_addr is None:
                return {'ok': False, 'error': 'backend_stopped', 'send_ms': 0}
            token = random.randint(0, 0xFFFF)
            # WM1303: register future BEFORE sendto to avoid race where the
            # post-TX ack arrives before we register (reader thread is fast).
            body  = json.dumps({'txpk': txpk}).encode()
            _ack_future = self._tx_ack_register_future(token)
            pkt   = bytes([PROTOCOL_VER, (token >> 8) & 0xFF,
                          token & 0xFF, PKT_PULL_RESP]) + body

            # Send with auto-recovery: retry once after socket recreation on OSError
            _send_ms = 0.0
            _pull_resp_sent_mono = 0.0  # captured on successful sendto
            for _attempt in range(2):
                try:
                    _send_start = time.monotonic()
                    sock = self._sock
                    if sock is None:
                        raise OSError('UDP socket unavailable')
                    sock.sendto(pkt, self._pull_addr)
                    _send_ms = round((time.monotonic() - _send_start) * 1000, 2)
                    # Capture reference moment for TX-phase trace backdating
                    _pull_resp_sent_mono = time.monotonic()
                    logger.info('WM1303Backend: PULL_RESP sent to %s '
                                '(freq=%.6f, datr=%s, rfch=0, udp_send=%.1fms, airwait=%.1fms)',
                                self._pull_addr, txpk['freq'], txpk['datr'],
                                _send_ms, _airtime_wait_ms)
                    break  # success — exit retry loop
                except OSError as e:
                    if _attempt == 0:
                        logger.error('WM1303Backend: PULL_RESP sendto OSError (attempt 1): '
                                    '%s — attempting socket recovery', e)
                        self._recreate_socket()
                        continue  # retry with new socket
                    else:
                        logger.error('WM1303Backend: PULL_RESP sendto failed after '
                                    'socket recovery: %s', e)
                        self._tx_ack_unregister_future(token)
                        _ack_future.cancel()
                        return {'error': str(e), 'ok': False, 'send_ms': 0}
                except Exception as e:
                    logger.error('WM1303Backend: PULL_RESP send failed: %s', e)
                    self._tx_ack_unregister_future(token)
                    _ack_future.cancel()
                    return {'error': str(e), 'ok': False, 'send_ms': 0}
            else:
                # for/else: loop completed without break = all attempts failed
                self._tx_ack_unregister_future(token)
                _ack_future.cancel()
                return {'error': 'sendto failed after retry', 'ok': False, 'send_ms': 0}

            # --- Success path: sendto completed ---

            # Calculate LoRa airtime for logging and conservative fallback.
            # The actual _last_tx_end is set AFTER receiving the post-TX ACK
            # with a small 50ms safety margin (TX is already complete at that point).
            _airtime_ms_val = 0.0
            _airtime_s = 0.0
            _datr = txpk.get("datr", "SF8BW125")
            _sf, _bw = _parse_datr(_datr)
            if _sf is not None:
                _cr = int(str(txpk.get('codr', '4/5')).split('/')[-1])
                _airtime_s = self._lora_airtime_s(
                    _sf, _bw, txpk.get("size", 0), txpk.get("prea", 17),
                    cr=_cr, crc=not txpk.get('ncrc', False))
                _airtime_ms_val = round(_airtime_s * 1000, 1)
                # Set a CONSERVATIVE _last_tx_end in case ACK never arrives.
                # This will be overwritten with now + 50ms after ACK (TX already done).
                self._last_tx_end = time.monotonic() + _airtime_s + 2.0
                logger.info('WM1303Backend: TX airtime %.1fms (SF%d BW%d %d bytes)',
                           _airtime_ms_val, _sf, _bw, txpk.get('size', 0))
            # AGC recovery DISABLED: causes ~96s deaf (6s restart + 90s self-recovery)
            # SX1302 self-recovers in ~90s without restart - no benefit to restarting
            # self._schedule_agc_reset()

            # WM1303 Approach 3: await post-TX ack for CAD/LBT data.
            # The post-TX ACK arrives AFTER the TX is complete (the C code
            # polls TX_STATUS until TX_FREE before sending the ACK).
            # Since TX is already done when we receive the ACK, we set
            # _last_tx_end = now + 50ms (small safety margin), overwriting
            # the conservative fallback set earlier.
            _result = {'ok': False, 'error': 'ack_timeout',
                       'freq': txpk['freq'], 'datr': txpk['datr'],
                       'airtime_ms': _airtime_ms_val, 'send_ms': _send_ms,
                       'tx_token': token, 'ack_received': False}
            _ack_timeout_s = max((_airtime_ms_val + 2000.0) / 1000.0, 3.0)
            try:
                _post_ack = await asyncio.wait_for(_ack_future, timeout=_ack_timeout_s)
                if isinstance(_post_ack, dict):
                    _result['ack_received'] = True
                    _result['ok'] = bool(_post_ack.get('ok'))
                    _error = _post_ack.get('error')
                    _result['error'] = None if _result['ok'] else (
                        _error if _error and _error != 'NONE' else _post_ack.get('tx_result', 'unknown'))
                    self._consecutive_ack_timeouts = 0  # Layer 2: reset on successful ACK
                    # Merge cad/lbt fields into result
                    for _k in ('tx_result', 'phase', 'cad', 'lbt',
                               'cad_enabled', 'cad_detected', 'cad_retries',
                               'tx_noisefloor_dbm', 'cad_reason',
                               'lbt_enabled', 'lbt_pass', 'lbt_rssi_dbm',
                               'lbt_threshold_dbm', 'lbt_retries'):
                        if _k in _post_ack:
                            _result[_k] = _post_ack[_k]
                    if _post_ack.get('tx_result') == 'dropped':
                        # Packet was dropped by JIT (TOO_LATE/COLLISION),
                        # never transmitted. Reset _last_tx_end to now —
                        # no RF airtime to wait for.
                        self._last_tx_end = time.monotonic()
                        logger.warning('WM1303Backend: post-TX ACK: packet DROPPED '
                                       '(token=0x%04x, error=%s) — _last_tx_end reset to now',
                                       token, _post_ack.get('error', 'unknown'))
                        _result['ok'] = False
                        _result['tx_result'] = 'dropped'
                        _result['tx_blocked'] = True
                    elif _post_ack.get('tx_result') and _post_ack['tx_result'] != 'sent':
                        # Other non-sent results
                        self._last_tx_end = time.monotonic() + 0.05  # 50ms safety margin; TX already complete
                        logger.info('WM1303Backend: post-TX ACK received — _last_tx_end = now + 50ms '
                                    '(TX complete, non-sent result)')
                        _result['tx_blocked'] = True
                    else:
                        # ── ACK received: TX has FINISHED on RF ──
                        # Set _last_tx_end to now + 50ms safety margin.
                        # TX is already complete when the ACK arrives.
                        self._last_tx_end = time.monotonic() + 0.05  # 50ms safety margin; TX already complete
                        logger.info('WM1303Backend: post-TX ACK received — _last_tx_end = now + 50ms '
                                    '(TX complete, airtime was %.1fms)', _airtime_ms_val)
            except asyncio.TimeoutError:
                # Graceful degradation: conservative _last_tx_end stays
                # (set earlier to now + airtime + 2s). No precise guard.
                self._tx_ack_unregister_future(token)
                self._consecutive_ack_timeouts += 1
                logger.warning('WM1303Backend: post-TX ack timeout for token=0x%04x '
                               '— using conservative guard (airtime + 2s), '
                               'consecutive_timeouts=%d/%d',
                               token, self._consecutive_ack_timeouts,
                               self._l2_ack_timeout_threshold)
            except Exception as _e:
                _result['error'] = str(_e)
                logger.debug('WM1303Backend: post-TX ack await error: %s', _e)
            finally:
                self._tx_ack_unregister_future(token)
                if not _ack_future.done():
                    _ack_future.cancel()
            if _result['ok']:
                # A queued packet or successful UDP send is not yet RF TX.
                # Only confirmed transmissions may suppress subsequent echoes.
                _tx_payload = base64.b64decode(txpk.get('data', ''))
                _now_m = time.monotonic()
                self._tx_echo_hashes = {
                    key: ts for key, ts in self._tx_echo_hashes.copy().items()
                    if _now_m - ts < self._tx_echo_ttl
                }
                self._tx_echo_hashes[packet_hash(_tx_payload)] = _now_m
                self._current_burst_tx_time += _airtime_s
            _result['last_tx_end'] = self._last_tx_end

            # Emit chronologically-correct TX-phase trace events now that
            # ACK has arrived (or timed out). Back-dates events using the
            # captured _pull_resp_sent_mono reference moment.
            if trace_hash and _pull_resp_sent_mono > 0:
                self._emit_tx_phase_trace(trace_hash, channel_id,
                                          _pull_resp_sent_mono, _result)
            return _result

    def _emit_tx_phase_trace(self, trace_hash: str, channel_id: str,
                             pull_resp_sent_mono: float, result: dict) -> None:
        """Emit chronologically-correct TX-phase trace events after TX_ACK.

        Events emitted (relative to post-TX ACK emission time NOW):
          - tx_noisefloor   : PULL_RESP + ~16ms (FSK noisefloor read)
          - rf_tx_start     : PULL_RESP + ~88ms (after CAD/LBT/restart-defer)
          - rf_tx_end       : rf_tx_start + airtime_ms
          - sx1261_rx_restart : rf_tx_end + ~209ms (TX inhibit cooldown)
          - tx_ack          : NOW (no back-dating)

        All back-dating uses packet_trace's ts_offset_ms (milliseconds to
        subtract from current time). Events are emitted in chronological
        order, and packet_trace.trace_event() preserves insertion order for
        events with identical or nearly-identical timestamps.

        Safe no-op when trace callback is not registered.
        """
        if not trace_hash:
            return
        try:
            _now_mono = time.monotonic()
            _elapsed_total_ms = (_now_mono - pull_resp_sent_mono) * 1000.0
            _airtime_ms = float(result.get('airtime_ms', 0) or 0)
            _ack_received = bool(result.get('ack_received', False))
            _tx_result = result.get('tx_result', 'unknown')
            _tx_nf = result.get('tx_noisefloor_dbm')
            _cad_retries = int(result.get('cad_retries', 0) or 0)
            _freq_mhz = result.get('freq', 0)
            _datr = result.get('datr', '')
            _cad_enabled = bool(result.get('cad_enabled', False))
            _lbt_enabled = bool(result.get('lbt_enabled', False))

            # Whether we have a successful TX whose hardware timing we can
            # use to anchor the scan + TX-phase events.
            _tx_sent = _ack_received and _tx_result == 'sent'

            # Edge-B guard: the Option-3 timing model requires at least
            # _ACK_ROUNDTRIP_MS + _FEM_SETTLE_MS + 1 ms of elapsed time
            # between pull_resp_sent and NOW, otherwise the back-dating
            # anchors collide and ordering cannot be preserved. This is
            # physically unreachable for a real TX (min chain ~100 ms+),
            # so firing this guard indicates a degenerate measurement.
            if _tx_sent and _elapsed_total_ms < 48.0:
                logger.warning(
                    'WM1303Backend: _emit_tx_phase_trace: '
                    '_elapsed_total_ms=%.1f ms < 48 ms, TX-phase timing '
                    'model degenerate; skipping rf_tx_start/end/restart '
                    'emission (scan events still use pull_resp_sent anchor)',
                    _elapsed_total_ms)
                _tx_sent = False  # fall through to the failure-path formulas

            _FEM_SETTLE_MS = 5.0

            # Compute hardware-grounded TX anchors (bottom-up from the
            # Option D post-TX restart measurement) when TX succeeded.
            if _tx_sent:
                _offset_restart = float(_ACK_ROUNDTRIP_MS)
                _offset_end = _offset_restart + _FEM_SETTLE_MS
                _offset_start = _offset_end + _airtime_ms
                # Cap rf_tx_start to not precede pull_resp_sent.
                _offset_start = min(_offset_start,
                                    max(0.0, _elapsed_total_ms - 1.0))
                # Re-clamp rf_tx_end to stay strictly between
                # rf_tx_start and sx1261_rx_restart.
                _offset_end = min(_offset_end,
                                  max(_offset_restart + 1.0,
                                      _offset_start - 1.0))
                # Scan events anchored to the hardware-grounded rf_tx_start
                # so they appear immediately before TX (physically correct:
                # HAL performs LBT/CAD right before firing the TX).
                # Use actual HAL-measured CAD duration when available;
                # fall back to the estimated constant if not reported.
                _hal_cad_dur = result.get('cad', {}).get('duration_ms')
                if _hal_cad_dur and float(_hal_cad_dur) > 0:
                    _pre_rf_span_ms = float(_hal_cad_dur)
                else:
                    _pre_rf_span_ms = float(_PRE_RF_TX_MS - _PRE_NOISEFLOOR_MS)
                _offset_scan_start = _offset_start + _pre_rf_span_ms
                _offset_scan_result = _offset_start + 1.0
                # Cap scan offsets so they never predate pull_resp_sent.
                # This matters in the "short-wait" case where the HAL fires
                # TX almost immediately after pull_resp and the actual scan
                # duration would otherwise back-date cad_start before
                # pull_resp_sent and break chronological ordering with
                # upstream RX/enqueue steps.
                _scan_cap = max(0.0, _elapsed_total_ms - 1.0)
                _offset_scan_start = min(_offset_scan_start, _scan_cap)
                _offset_scan_result = min(_offset_scan_result,
                                          _offset_scan_start)
                _offset_nf = _offset_scan_start
            else:
                # Failure path or degenerate-elapsed guard: no rf_tx_start
                # anchor available, fall back to pull_resp_sent-relative
                # formulas (the historical behaviour).
                _offset_scan_start = max(0.0,
                                         _elapsed_total_ms - _PRE_NOISEFLOOR_MS)
                _offset_nf = _offset_scan_start
                _offset_scan_result = max(0.0,
                                          _elapsed_total_ms - _PRE_RF_TX_MS)

            # ---- cad_start (HAL CAD scan begins, ~same moment as noisefloor read) ----
            if _cad_enabled or (_lbt_enabled and isinstance(result.get('lbt_pass'), bool)):
                _scan_detail_parts = ['CAD + LBT scan started']
                if _lbt_enabled:
                    _lbt_thr = result.get('lbt_threshold_dbm')
                    if _lbt_thr is not None:
                        _scan_detail_parts.append('  LBT threshold: %s dBm' % _lbt_thr)
                _trace(trace_hash, 'cad_start', channel=channel_id,
                       detail='\n'.join(_scan_detail_parts),
                       status='ok', ts_offset_ms=_offset_scan_start)

            # ---- tx_noisefloor (pre-CAD FSK noise floor read) ----
            if _tx_nf is not None:
                _trace(trace_hash, 'tx_noisefloor', channel=channel_id,
                       detail='Noise floor: %s dBm (FSK-RX)' % _tx_nf,
                       status='ok', ts_offset_ms=_offset_nf)

            # ---- lbt_check + cad_check (at scan completion, just before rf_tx_start) ----
            if _lbt_enabled:
                _lbt_pass = result.get('lbt_pass')
                _lbt_rssi = result.get('lbt_rssi_dbm')
                _lbt_thr = result.get('lbt_threshold_dbm')
                _lbt_retries = int(result.get('lbt_retries', 0) or 0)
                _lbt_header = ('LBT PASS' if _lbt_pass is True else
                               'LBT BLOCKED' if _lbt_pass is False else 'LBT RESULT UNAVAILABLE')
                _lbt_parts = [_lbt_header]
                if _lbt_rssi is not None:
                    _lbt_parts.append('  RSSI: %s dBm' % _lbt_rssi)
                if _lbt_thr is not None:
                    _lbt_parts.append('  Threshold: %s dBm' % _lbt_thr)
                _lbt_parts.append('  Retries: %d' % _lbt_retries)
                _lbt_status = ('ok' if _lbt_pass is True and _lbt_retries == 0 else
                               'filtered' if _lbt_pass is False else 'partial')
                _trace(trace_hash, 'lbt_check', channel=channel_id,
                       detail='\n'.join(_lbt_parts), status=_lbt_status,
                       ts_offset_ms=_offset_scan_result)
            if _cad_enabled:
                _cad_detected = bool(result.get('cad_detected', False))
                _cad_reason = (result.get('cad_reason') or '').strip()
                _cad_header = 'CAD DETECTED' if _cad_detected else 'CAD CLEAR'
                _cad_parts = [_cad_header]
                if _tx_nf is not None:
                    _cad_parts.append('  TX Noisefloor: %s dBm' % _tx_nf)
                if _cad_reason:
                    _cad_parts.append('  Reason: %s' % _cad_reason)
                _cad_parts.append('  Retries: %d' % _cad_retries)
                # CAD scan duration: use actual HAL-measured value when
                # available, fall back to computed offset difference.
                _hal_cad_dur_ms = result.get('cad', {}).get('duration_ms')
                if _hal_cad_dur_ms and float(_hal_cad_dur_ms) > 0:
                    _cad_duration_ms = float(_hal_cad_dur_ms)
                else:
                    _cad_duration_ms = _offset_scan_start - _offset_scan_result
                if _cad_duration_ms > 0:
                    _cad_parts.append('  Duration: %.1f ms' % _cad_duration_ms)
                _cad_status = ('filtered' if _cad_detected else
                               ('ok' if _cad_retries == 0 else 'partial'))
                _trace(trace_hash, 'cad_check', channel=channel_id,
                       detail='\n'.join(_cad_parts), status=_cad_status,
                       ts_offset_ms=_offset_scan_result)

            # ---- TX-phase events (only when TX actually sent and guard passed) ----
            if _tx_sent:
                _tx_detail = ('RF transmission started\n  Frequency: %s MHz\n'
                              '  Datarate: %s\n  Airtime: %.1f ms'
                              % (_freq_mhz, _datr, _airtime_ms))
                _trace(trace_hash, 'rf_tx_start', channel=channel_id,
                       detail=_tx_detail, status='ok',
                       ts_offset_ms=_offset_start)
                _trace(trace_hash, 'rf_tx_end', channel=channel_id,
                       detail='RF transmission complete\n  Airtime: %.1f ms'
                              % _airtime_ms,
                       status='ok', ts_offset_ms=_offset_end)
                _trace(trace_hash, 'sx1261_rx_restart', channel=channel_id,
                       detail='SX1261 LoRa RX restarted (post-TX inhibit cleared)',
                       status='ok', ts_offset_ms=_offset_restart)

            # ---- tx_ack (emit at current time) ----
            if _ack_received:
                if _tx_result == 'sent':
                    _ack_detail = ('TX acknowledged\n  Result: sent\n  Token: 0x%04x'
                                   % (int(result.get('tx_token', 0)) & 0xFFFF))
                    _ack_status = 'ok'
                elif _tx_result == 'dropped':
                    _ack_detail = ('TX dropped by JIT\n  Result: %s\n  Error: %s'
                                   % (_tx_result, result.get('error', 'unknown')))
                    _ack_status = 'error'
                else:
                    _ack_detail = 'TX blocked\n  Result: %s' % _tx_result
                    _ack_status = 'warning'
                if _cad_retries > 0:
                    _ack_detail += '\n  CAD retries: %d' % _cad_retries
                _trace(trace_hash, 'tx_ack', channel=channel_id,
                       detail=_ack_detail, status=_ack_status,
                       ts_offset_ms=0)
            else:
                # ACK timeout: emit a warning tx_ack so the trace shows it
                _trace(trace_hash, 'tx_ack', channel=channel_id,
                       detail='TX ACK timeout (no response from pkt_fwd)',
                       status='warning', ts_offset_ms=0)
        except Exception as _trace_e:
            logger.debug('WM1303Backend: _emit_tx_phase_trace failed: %s',
                         _trace_e)


    # ------------------------------------------------------------------
    # Status & Stats
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # AGC Recovery (post-TX concentrator reset)
    # ------------------------------------------------------------------

    def _schedule_agc_reset(self) -> None:
        """Schedule an AGC recovery reset after TX.

        Batches multiple TX events: cancels any pending timer and sets
        a new one. The reset fires `_agc_reset_delay` seconds after the
        LAST TX in a burst.
        """
        with self._agc_reset_lock:
            if self._agc_reset_timer:
                self._agc_reset_timer.cancel()
            self._agc_reset_timer = threading.Timer(
                self._agc_reset_delay, self._do_agc_reset)
            self._agc_reset_timer.daemon = True
            self._agc_reset_timer.start()
            logger.info("WM1303Backend: AGC reset scheduled in %.1fs",
                       self._agc_reset_delay)

    def _do_agc_reset(self) -> None:
        """Lightweight AGC recovery - restarts pkt_fwd without GPIO reset.

        After TX the SX1302 AGC firmware takes ~70s to self-recover.
        Restarting pkt_fwd restores RX in ~2.5s.
        """
        if not self._running:
            return
        if getattr(self, "_agc_resetting", False):
            logger.warning("WM1303Backend: [AGC RECOVERY] already in progress, skipping")
            return
        self._agc_resetting = True
        self._current_burst_tx_time = 0.0
        _start = time.monotonic()
        logger.info("WM1303Backend: [AGC RECOVERY] starting lightweight pkt_fwd restart")
        try:
            self._ensure_active_pktfwd_config()
            self._stop_pktfwd_process()
            time.sleep(0.3)
            self._start_pktfwd()  # includes PULL_DATA wait
            _elapsed = time.monotonic() - _start
            self._agc_resets += 1
            logger.info("WM1303Backend: [AGC RECOVERY] complete in %.1fs (total=%d)",
                        _elapsed, self._agc_resets)
        except Exception as _e:
            logger.error("WM1303Backend: [AGC RECOVERY] failed: %s", _e)
        finally:
            self._agc_resetting = False
            with self._agc_reset_lock:
                self._agc_reset_timer = None


    # === ORIGINAL FULL AGC RESET CODE (commented out for lightweight test) ===
    # AGC_FULL_RESET:     def _do_agc_reset(self) -> None:
    # AGC_FULL_RESET:         """Perform AGC recovery by restarting lora_pkt_fwd.
    # AGC_FULL_RESET:
    # AGC_FULL_RESET:         After TX on RF0 (same chain as RX), the SX1302 internal AGC
    # AGC_FULL_RESET:         firmware can get stuck, making the concentrator deaf. This
    # AGC_FULL_RESET:         method kills and restarts lora_pkt_fwd with a GPIO reset to
    # AGC_FULL_RESET:         restore RX sensitivity.
    # AGC_FULL_RESET:         """
    # AGC_FULL_RESET:         if not self._running:
    # AGC_FULL_RESET:             return
    # AGC_FULL_RESET:         if getattr(self, "_agc_resetting", False):
    # AGC_FULL_RESET:             logger.warning("WM1303Backend: [AGC RECOVERY] already in progress, skipping")
    # AGC_FULL_RESET:             return
    # AGC_FULL_RESET:
    # AGC_FULL_RESET:         self._agc_resetting = True
    # AGC_FULL_RESET:         _start = time.monotonic()
    # AGC_FULL_RESET:         logger.info("WM1303Backend: [AGC RECOVERY] starting concentrator reset")
    # AGC_FULL_RESET:         try:
    # AGC_FULL_RESET:             # Step 1: Stop current pkt_fwd
    # AGC_FULL_RESET:             self._stop_pktfwd_process()
    # AGC_FULL_RESET:             time.sleep(0.5)
    # AGC_FULL_RESET:
    # AGC_FULL_RESET:             # Step 2: GPIO reset to restore AGC
    # AGC_FULL_RESET:             try:
    # AGC_FULL_RESET:                 logger.info("WM1303Backend: [AGC RECOVERY] running GPIO reset")
    # AGC_FULL_RESET:                 r = subprocess.run(
    # AGC_FULL_RESET:                     ["sudo", str(PKTFWD_RESET), "start"],
    # AGC_FULL_RESET:                     cwd=str(PKTFWD_DIR), capture_output=True, timeout=15
    # AGC_FULL_RESET:                 )
    # AGC_FULL_RESET:                 if r.returncode != 0:
    # AGC_FULL_RESET:                     logger.warning("WM1303Backend: [AGC RECOVERY] GPIO reset rc=%d",
    # AGC_FULL_RESET:                                   r.returncode)
    # AGC_FULL_RESET:                 time.sleep(2.0)  # TCXO warmup
    # AGC_FULL_RESET:             except Exception as e:
    # AGC_FULL_RESET:                 logger.error("WM1303Backend: [AGC RECOVERY] GPIO reset failed: %s", e)
    # AGC_FULL_RESET:
    # AGC_FULL_RESET:             # Step 3: Restart pkt_fwd
    # AGC_FULL_RESET:             try:
    # AGC_FULL_RESET:                 cmd = ["sudo", str(PKTFWD_BIN), "-c", str(BRIDGE_CONF)]
    # AGC_FULL_RESET:                 logger.info("WM1303Backend: [AGC RECOVERY] restarting pkt_fwd: %s",
    # AGC_FULL_RESET:                            " ".join(cmd))
    # AGC_FULL_RESET:                 self._proc = subprocess.Popen(
    # AGC_FULL_RESET:                     cmd,
    # AGC_FULL_RESET:                     cwd=str(PKTFWD_DIR),
    # AGC_FULL_RESET:                     stdout=subprocess.PIPE,
    # AGC_FULL_RESET:                     stderr=subprocess.STDOUT,
    # AGC_FULL_RESET:                     text=True,
    # AGC_FULL_RESET:                 )
    # AGC_FULL_RESET:                 time.sleep(2.0)  # Wait for concentrator init (optimized)
    # AGC_FULL_RESET:                 if self._proc.poll() is not None:
    # AGC_FULL_RESET:                     out = self._proc.stdout.read()
    # AGC_FULL_RESET:                     logger.error("WM1303Backend: [AGC RECOVERY] pkt_fwd exited: %s",
    # AGC_FULL_RESET:                                 out[:500])
    # AGC_FULL_RESET:                 else:
    # AGC_FULL_RESET:                     logger.info("WM1303Backend: [AGC RECOVERY] pkt_fwd restarted (pid %d)",
    # AGC_FULL_RESET:                                self._proc.pid)
    # AGC_FULL_RESET:                     # Restart stdout reader thread
    # AGC_FULL_RESET:                     self._stdout_thread = threading.Thread(
    # AGC_FULL_RESET:                         target=self._pktfwd_stdout_reader, daemon=True,
    # AGC_FULL_RESET:                         name="pktfwd-stdout")
    # AGC_FULL_RESET:                     self._stdout_thread.start()
    # AGC_FULL_RESET:             except Exception as e:
    # AGC_FULL_RESET:                 logger.error("WM1303Backend: [AGC RECOVERY] restart failed: %s", e)
    # AGC_FULL_RESET:
    # AGC_FULL_RESET:             self._agc_resets += 1
    # AGC_FULL_RESET:             _elapsed_ms = round((time.monotonic() - _start) * 1000)
    # AGC_FULL_RESET:             logger.info("WM1303Backend: [AGC RECOVERY] complete (%dms, total=%d)",
    # AGC_FULL_RESET:                        _elapsed_ms, self._agc_resets)
    # AGC_FULL_RESET:         except Exception as e:
    # AGC_FULL_RESET:             logger.error("WM1303Backend: [AGC RECOVERY] failed: %s", e)
    # AGC_FULL_RESET:         finally:
    # AGC_FULL_RESET:             self._agc_resetting = False
    # AGC_FULL_RESET:             with self._agc_reset_lock:
    # AGC_FULL_RESET:                 self._agc_reset_timer = None
    # AGC_FULL_RESET:
    # === END ORIGINAL FULL AGC RESET CODE ===

    # ------------------------------------------------------------------
    # Full Concentrator Reset (used by heartbeat)
    # ------------------------------------------------------------------
    def _do_full_reset(self) -> None:
        """Recover through the same owned-process lifecycle as normal startup."""
        if not self._running or getattr(self, '_agc_resetting', False):
            return
        self._agc_resetting = True
        started = time.monotonic()
        try:
            self._restart_pkt_fwd()
            self._agc_resets += 1
            logger.info('WM1303Backend: [FULL RESET] complete (%dms, total=%d)',
                        round((time.monotonic() - started) * 1000), self._agc_resets)
        except Exception:
            logger.exception('WM1303Backend: [FULL RESET] failed')
        finally:
            self._agc_resetting = False
            self._current_burst_tx_time = 0.0

    # ------------------------------------------------------------------
    # Periodic Heartbeat Reset
    # ------------------------------------------------------------------
    def _start_heartbeat_reset(self) -> None:
        """Start periodic full concentrator reset to prevent IF chain drift."""
        self._heartbeat_interval = 180  # 3 minutes
        self._heartbeat_timer = None
        self._schedule_heartbeat()
        logger.info("WM1303Backend: heartbeat reset scheduled every %ds", self._heartbeat_interval)

    def _schedule_heartbeat(self) -> None:
        """Schedule the next heartbeat full reset."""
        if not self._running:
            return
        self._heartbeat_timer = threading.Timer(
            self._heartbeat_interval, self._heartbeat_reset)
        self._heartbeat_timer.daemon = True
        self._heartbeat_timer.start()

    def _heartbeat_reset(self) -> None:
        """Execute periodic full concentrator reset."""
        if not self._running:
            return
        logger.info("WM1303Backend: HEARTBEAT - periodic full concentrator reset")
        self._do_full_reset()
        logger.info("WM1303Backend: HEARTBEAT complete")
        self._schedule_heartbeat()  # schedule next heartbeat


    def _update_rx_stats(self, channel_id: str, freq_hz: int,
                         rssi: float, snr: float) -> None:
        """Update per-channel RX statistics."""
        if channel_id not in self._channel_rx_stats:
            self._channel_rx_stats[channel_id] = {
                "rx_count": 0, "last_rssi": -120.0, "last_snr": 0.0,
                "rssi_sum": 0.0, "snr_sum": 0.0,
                "last_rx_time": None, "freq_hz": freq_hz,
            }
        s = self._channel_rx_stats[channel_id]
        s["rx_count"] += 1
        s["last_rssi"] = round(rssi, 1)
        s["last_snr"] = round(snr, 1)
        s["rssi_sum"] += rssi
        s["snr_sum"] += snr
        s["last_rx_time"] = time.time()
        s["freq_hz"] = freq_hz

        # RX-based noise floor estimation: NF ≈ RSSI - SNR (when SNR > 0)
        # For negative SNR the signal is below the noise, so RSSI itself
        # is already close to the noise floor.
        if snr > 0:
            nf_est = rssi - snr
        else:
            nf_est = rssi
        now = time.time()
        with self._rx_nf_lock:
            if channel_id not in self._rx_nf_estimates:
                self._rx_nf_estimates[channel_id] = collections.deque(
                    maxlen=self._rx_nf_max_samples)
            self._rx_nf_estimates[channel_id].append((now, round(nf_est, 1)))

    def _init_channel_stats_db(self) -> None:
        """Create channel_stats_history table if it doesn't exist."""
        try:
            with _db_conn(self._db_path) as conn:
                conn.execute("""CREATE TABLE IF NOT EXISTS channel_stats_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    channel_id TEXT NOT NULL,
                    rx_count INTEGER DEFAULT 0,
                    avg_rssi REAL,
                    avg_snr REAL,
                    tx_count INTEGER DEFAULT 0,
                    tx_failed INTEGER DEFAULT 0,
                    tx_airtime_ms REAL DEFAULT 0,
                    tx_bytes INTEGER DEFAULT 0,
                    lbt_blocked INTEGER DEFAULT 0,
                    lbt_passed INTEGER DEFAULT 0,
                    lbt_last_rssi REAL,
                    lbt_threshold REAL,
                    noise_floor_dbm REAL,
                    tx_noisefloor_dbm REAL
                )""")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_csh_ts ON channel_stats_history(timestamp)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_csh_ch ON channel_stats_history(channel_id)")
                # Migration: add tx_noisefloor_dbm column if missing (existing installs)
                try:
                    conn.execute("ALTER TABLE channel_stats_history ADD COLUMN tx_noisefloor_dbm REAL")
                except Exception:
                    pass  # column already exists
                conn.commit()
            logger.info("channel_stats_history table ready")
        except Exception as e:
            logger.error("Failed to init channel_stats_history table: %s", e)

        # Create noise_floor_history table for per-channel noise floor tracking
        try:
            with _db_conn(self._db_path) as conn:
                conn.execute("""CREATE TABLE IF NOT EXISTS noise_floor_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    channel_id TEXT NOT NULL,
                    noise_floor_dbm REAL NOT NULL,
                    samples_collected INTEGER DEFAULT 0,
                    samples_accepted INTEGER DEFAULT 0,
                    min_rssi REAL,
                    max_rssi REAL
                )""")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_nfh_ts ON noise_floor_history(timestamp)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_nfh_ch ON noise_floor_history(channel_id)")
                conn.commit()
            logger.info("noise_floor_history table ready")
        except Exception as e:
            logger.error("Failed to init noise_floor_history table: %s", e)

    def _snapshot_channel_stats(self) -> None:
        """Take a snapshot of current per-channel stats and write to DB."""
        try:
            now = time.time()
            ch_stats = self.get_channel_stats()
            if not ch_stats:
                return

            # Get per-channel noise floors from NoiseFloorMonitor
            # Build channel_id -> UI config name mapping for noise floor lookup
            _ch_id_to_ui_name = {}
            try:
                _ui2 = self._read_active_ui()
                _CHID = ['channel_a', 'channel_b', 'channel_c', 'channel_d']
                for _aidx, _uch in enumerate(_ui2.get('channels', [])):
                    if _uch.get('active', False) and _aidx < len(_CHID):
                        _ch_id_to_ui_name[_CHID[_aidx]] = _uch.get('name', '')
                for cid, default_name in (('channel_e', 'Channel E'), ('channel_f', 'Channel F')):
                    settings = _ui2.get(cid, {})
                    if settings.get('enabled', False):
                        _ch_id_to_ui_name[cid] = settings.get('friendly_name', default_name)
            except Exception:
                pass
            with self._nf_lock:
                _nf_by_ch_id = {}
                for _cid, _uname in _ch_id_to_ui_name.items():
                    nf_val = self._channel_noise_floors.get(_uname)
                    if nf_val is not None:
                        _nf_by_ch_id[_cid] = nf_val

            with _db_conn(self._db_path) as conn:
                # Use active thresholds, never the desired next-start config.
                _lbt_thresholds = {}
                for ch_id in ch_stats:
                    lbt = self._get_channel_lbt_config(ch_id)
                    if lbt.get('lbt_enabled'):
                        _lbt_thresholds[ch_id] = lbt['lbt_rssi_target']

                for ch_id, data in ch_stats.items():
                    rx_count = data.get("rx_count", 0)
                    # Compute avg RSSI/SNR from raw sums
                    rx_raw = self._channel_rx_stats.get(ch_id, {})
                    rc = rx_raw.get("rx_count", 0)
                    avg_rssi = round(rx_raw["rssi_sum"] / rc, 1) if rc > 0 else None
                    avg_snr = round(rx_raw["snr_sum"] / rc, 1) if rc > 0 else None

                    # TX echo filter: discard RSSI/SNR values that are unrealistically
                    # strong (> -50 dBm). These are self-echo from the concentrator
                    # hearing its own TX back on the RX chain. Real LoRa signals are
                    # always below -50 dBm at the receiver.
                    if avg_rssi is not None and avg_rssi > -50:
                        logger.debug("TX echo filter: discarding avg_rssi=%.1f for %s (> -50 dBm)", avg_rssi, ch_id)
                        avg_rssi = None
                        avg_snr = None

                    tx_count = data.get("tx_count", 0)
                    tx_failed = data.get("tx_failed", 0)
                    tx_airtime = data.get("total_tx_airtime_ms", 0)
                    tx_bytes = data.get("tx_bytes", 0)
                    lbt_blocked = data.get("lbt_blocked", 0)
                    lbt_passed = data.get("lbt_passed", 0)
                    lbt_last_rssi = data.get("lbt_last_rssi")
                    lbt_threshold = data.get("lbt_last_threshold")
                    if lbt_threshold is None:
                        lbt_threshold = _lbt_thresholds.get(ch_id)
                    tx_nf_dbm = data.get("tx_noisefloor_avg")

                    conn.execute(
                        """INSERT INTO channel_stats_history
                        (timestamp, channel_id, rx_count, avg_rssi, avg_snr,
                         tx_count, tx_failed, tx_airtime_ms, tx_bytes,
                         lbt_blocked, lbt_passed, lbt_last_rssi, lbt_threshold,
                         noise_floor_dbm, tx_noisefloor_dbm)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (now, ch_id, rx_count, avg_rssi, avg_snr,
                         tx_count, tx_failed, tx_airtime, tx_bytes,
                         lbt_blocked, lbt_passed, lbt_last_rssi, lbt_threshold,
                         _nf_by_ch_id.get(ch_id), tx_nf_dbm))
                conn.commit()
            logger.debug("Channel stats snapshot: %d channels recorded", len(ch_stats))
        except Exception as e:
            logger.error("Error in _snapshot_channel_stats: %s", e)

    def _channel_stats_snapshot_loop(self) -> None:
        """Background thread: periodically snapshot channel stats to DB."""
        logger.info("Starting channel stats snapshot loop (interval=60s)")
        self._init_channel_stats_db()
        # Wait 60s before first snapshot to let the system stabilize
        for _ in range(12):
            if not self._snapshot_running:
                return
            self._stop_event.wait(5)
        while self._snapshot_running:
            try:
                self._snapshot_channel_stats()
                # Cleanup moved to metrics_retention.py
                # cutoff = time.time() - (7 * 86400)
                # try:
                #     with _db_conn(_DB_PATH) as conn:
                #         conn.execute(
                #             "DELETE FROM channel_stats_history WHERE timestamp < ?",
                #             (cutoff,))
                #         conn.commit()
                # except Exception as ce:
                #     logger.error("Error cleaning old channel stats: %s", ce)
            except Exception as e:
                logger.error("Error in snapshot loop: %s", e)
            # Sleep 60 seconds (1 minute) in small intervals
            for _ in range(12):
                if not self._snapshot_running:
                    return
                self._stop_event.wait(5)
        logger.info("Channel stats snapshot loop stopped")

    def get_channel_stats(self) -> dict:
        """Return combined per-channel RX and TX statistics."""
        result = {}
        tx_stats = {}
        if self._tx_queue_manager:
            tx_stats = self._tx_queue_manager.get_status()
        all_chs = set(list(self._channel_rx_stats.keys()) + list(tx_stats.keys()))
        for ch_id in all_chs:
            rx = self._channel_rx_stats.get(ch_id, {})
            tx = tx_stats.get(ch_id, {})
            tx_timing = self._channel_tx_stats.get(ch_id, {})
            rc = rx.get("rx_count", 0)
            # Calculate uptime-based duty cycle
            uptime_s = time.time() - self._start_time if self._start_time else 1
            total_airtime_s = tx_timing.get("total_airtime_ms", 0) / 1000.0
            duty_pct = round((total_airtime_s / uptime_s) * 100, 3) if uptime_s > 0 else 0
            result[ch_id] = {
                "rx_count": rc,
                "last_rssi": rx.get("last_rssi", -120.0),
                "rssi_avg": round(rx["rssi_sum"] / rc, 1) if rc > 0 else -120.0,
                "last_snr": rx.get("last_snr", 0.0),
                "snr_avg": round(rx["snr_sum"] / rc, 1) if rc > 0 else 0.0,
                "last_rx_time": rx.get("last_rx_time"),
                "tx_count": tx.get("total_sent", 0),
                "tx_failed": tx.get("total_failed", 0),
                "tx_pending": tx.get("pending", 0),
                "last_tx_time": tx.get("last_tx_time"),
                "avg_tx_time_ms": tx.get("avg_tx_time_ms", 0),
                "freq_hz": rx.get("freq_hz", tx.get("freq_hz", 0)),
                # New TX timing fields
                "avg_tx_airtime_ms": tx_timing.get("avg_airtime_ms", 0),
                "avg_tx_send_ms": tx_timing.get("avg_send_ms", 0),
                "avg_tx_wait_ms": tx_timing.get("avg_wait_ms", 0),
                "last_tx_airtime_ms": tx_timing.get("last_airtime_ms", 0),
                "last_tx_send_ms": tx_timing.get("last_send_ms", 0),
                "last_tx_wait_ms": tx_timing.get("last_wait_ms", 0),
                "total_tx_airtime_ms": tx_timing.get("total_airtime_ms", 0),
                "total_tx_send_ms": tx_timing.get("total_send_ms", 0),
                "total_tx_wait_ms": tx_timing.get("total_wait_ms", 0),
                "tx_bytes": tx_timing.get("tx_bytes", 0),
                "tx_duty_pct": duty_pct,
                # Software LBT stats
                "lbt_blocked": tx.get("lbt_blocked", 0),
                "lbt_passed": tx.get("lbt_passed", 0),
                "lbt_skipped": tx.get("lbt_skipped", 0),
                "lbt_last_blocked_at": tx.get("lbt_last_blocked_at"),
                "lbt_last_rssi": tx.get("lbt_last_rssi"),
                "lbt_last_threshold": tx.get("lbt_last_threshold"),
                # Rolling statistics contain only actual LBT/pre-CAD samples.
                # Keep absent measurements null and expose their sample counts.
                "noise_floor_lbt_avg": tx.get("noise_floor_lbt_avg"),
                "noise_floor_lbt_min": tx.get("noise_floor_lbt_min"),
                "noise_floor_lbt_max": tx.get("noise_floor_lbt_max"),
                "noise_floor_lbt_samples": tx.get("noise_floor_lbt_samples", 0),
                "tx_noisefloor_avg": tx.get("tx_noisefloor_avg"),
                "tx_noisefloor_min": tx.get("tx_noisefloor_min"),
                "tx_noisefloor_max": tx.get("tx_noisefloor_max"),
                "tx_noisefloor_last": tx.get("tx_noisefloor_last"),
                "tx_noisefloor_samples": tx.get("tx_noisefloor_samples", 0),
            }
        return result

    def get_tx_stats(self) -> dict:
        """Return TX subsystem stats."""
        stats = {
            'architecture': 'RF0_TX_DIRECT',
            'tx_packets_sent_total': self._tx_packets_sent_total,
            'tx_echo_detected': self._tx_echo_detected,
            'tx_echo_hashes_active': len(self._tx_echo_hashes),
            'watchdog_last_rx_ago': round(time.monotonic() - self._last_rx_timestamp, 1),
            'watchdog_zero_rx_stat_count': self._zero_rx_stat_count,
            'watchdog_rssi_spike_count': self._rssi_spike_count,
            'watchdog_last_stat_rxnb': self._last_stat_rxnb,
            'watchdog_last_stat_txnb': self._last_stat_txnb,
            'sx1261_available': self._sx1261_available,
            'sx1261_managed_by_hal': getattr(self, '_sx1261_managed_by_hal', False),
            'sx1261_role': 'managed_by_hal' if getattr(self, '_sx1261_managed_by_hal', False) else ('LBT/CAD only' if self._sx1261_available else 'not_available'),
        }
        if self._tx_queue_manager:
            stats['tx_queues'] = self._tx_queue_manager.get_status()
        if self._sx1261:
            stats['sx1261_status'] = self._sx1261.get_status()
        elif getattr(self, '_sx1261_managed_by_hal', False):
            stats['sx1261_status'] = {
                'managed_by_hal': True,
                'reason': 'SPI bus shared with lora_pkt_fwd',
                'config': getattr(self, '_sx1261_hal_config', {}),
            }
        return stats
