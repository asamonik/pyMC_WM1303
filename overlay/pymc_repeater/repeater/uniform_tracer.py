"""Uniform RX Packet Tracer (v2.4.7+)

Provides uniform `received` trace events for all 5 WM1303 channels
(channel_a / channel_b / channel_c / channel_d / channel_e) so the
WM1303 Tracing UI shows every incoming packet regardless of which
channel it arrived on, with correct 1/2/3-byte hash-size detection.

Design:
  * Monkey-patches WM1303Backend._dispatch_rx (for channels a/b/c/d)
    and wraps the channel_e path so every RX emits a `received` trace.
  * Uses BridgeEngine._parse_path_info for consistent header/path parsing
    (supports 1/2/3-byte hashes; see upstream MeshCore Packet.h).
  * Friendly channel names from WM1303Backend._ch_id_to_ui_name
    (e.g. channel_a -> 'local-test', channel_e -> 'EU-Narrow').
  * CRC_ERROR / CRC_DISABLED packets ARE traced (noise visibility).
  * channel_e additionally keeps its existing richer bridge trace flow
    (bridge_inject / dedup_* / tx_enqueue / cad_* / rf_tx_*); this module
    only adds the initial `received` event so all 5 channels have a
    uniform starting point.
"""
from __future__ import annotations

import base64
from openhop_core.meshcore_wire import packet_hash
import logging
import threading
import time
from typing import Any

logger = logging.getLogger('UniformTracer')

# Flag so install_on_radio is idempotent (service restart / reload).
_INSTALLED_MARK = '_uniform_tracer_installed'

# Short cache: channel_id -> (pkt_hash8, monotonic_ts) for de-duplicating the
# `received` event across multi-demod arrivals from the same frame.
_RX_SEEN: dict[str, tuple[str, float]] = {}
_RX_SEEN_TTL = 2.0  # seconds

# CRC status map (matches pkt_fwd convention)
_CRC_LABEL = {1: 'CRC_OK', 0: 'CRC_DISABLED', -1: 'CRC_ERROR'}


def _packet_hash8(data: bytes) -> str:
    """Use the same eight-character trace identity as BridgeEngine."""
    return packet_hash(data, 8)


def _packet_type_name(data: bytes) -> str:
    if not data:
        return 'UNKNOWN'
    pt = (data[0] >> 2) & 0x0F
    return {
        0x00: 'REQ', 0x01: 'RESP', 0x02: 'TXT_MSG',
        0x03: 'ACK', 0x04: 'ADVERT', 0x05: 'GRP_TXT',
        0x06: 'GRP_DATA', 0x07: 'ANON_REQ', 0x08: 'PATH',
        0x09: 'TRACE', 0x0A: 'MULTIPART',
    }.get(pt, f'PT_{pt:X}')


def _fire_received(channel_id: str, ui_name: str, data: bytes,
                   rssi: float | int, snr: float, crc_ok: bool,
                   crc_stat: int = 1) -> None:
    """Emit a single uniform `received` trace event for any channel."""
    try:
        # Lazy imports to avoid circular deps at module load
        from repeater.bridge_engine import BridgeEngine
        from repeater.web.packet_trace import trace_event
    except Exception as _imp_err:
        logger.debug('uniform tracer imports failed: %s', _imp_err)
        return

    if not data or len(data) < 2:
        return

    pkt_hash8 = _packet_hash8(data)
    pkt_type = _packet_type_name(data)

    # De-duplicate across multi-demod (same frame reaches us 1-8x in a burst)
    _now = time.monotonic()
    _prev = _RX_SEEN.get(channel_id)
    if _prev and _prev[0] == pkt_hash8 and (_now - _prev[1]) < _RX_SEEN_TTL:
        return
    _RX_SEEN[channel_id] = (pkt_hash8, _now)
    # Light TTL cleanup
    if len(_RX_SEEN) > 64:
        for _k, (_h, _t) in list(_RX_SEEN.items()):
            if _now - _t > _RX_SEEN_TTL:
                del _RX_SEEN[_k]

    # v2.4.7+: CRC-safety — only trust path/hash parsing for CRC_OK packets.
    # CRC_ERROR / CRC_DISABLED frames are typically noise with garbage bytes;
    # running _parse_path_info on them yields bogus hash_size / path_hashes
    # (e.g. a rt=2/pt=0x0C "packet" decoded as a spurious 3-byte hash).
    if crc_ok:
        try:
            pinfo = BridgeEngine._parse_path_info(data) or {}
        except Exception:
            pinfo = {}
        hops = len(pinfo.get('path_hashes') or [])
        hsz = int(pinfo.get('path_hash_size') or 1)
    else:
        pinfo = {}
        hops = 0
        hsz = 0  # unknown — UI will render as '?'/'n/a'

    # Build human-readable detail line
    _rssi_s = f'{float(rssi):.1f}' if rssi not in (None, '', '?') else '?'
    _snr_s  = f'{float(snr):.1f}'  if snr  not in (None, '', '?') else '?'
    _crc_tag = '' if crc_ok else f' {_CRC_LABEL.get(crc_stat, "CRC?")}'
    if crc_ok:
        detail = f'RX {pkt_type} {len(data)}b rssi={_rssi_s} snr={_snr_s} hops={hops} hsz={hsz}b'
    else:
        # Don't claim hops/hash_size for noise — payload bytes are unreliable.
        detail = f'RX {pkt_type}? {len(data)}b rssi={_rssi_s} snr={_snr_s}{_crc_tag} (parsed fields suppressed)'

    try:
        trace_event(
            pkt_hash8, 'received',
            channel=ui_name or channel_id,
            pkt_type=pkt_type,
            detail=detail,
            status='ok' if crc_ok else 'warning',
            pkt_hash_full=pkt_hash8,
            hops=hops if crc_ok else None,
            hash_size=hsz if crc_ok else None,
            src_hash=pinfo.get('src_hash') if crc_ok else None,
            dst_hash=pinfo.get('dst_hash') if crc_ok else None,
            path_hashes=pinfo.get('path_hashes') if crc_ok else None,
            payload_hex=data.hex(),
            payload_full_size=len(data),
        )
    except Exception as _te:
        logger.debug('uniform tracer trace_event failed: %s', _te)


def install_on_radio(radio: Any) -> bool:
    """Wrap WM1303Backend._dispatch_rx + channel_e callback to fire `received`
    trace events uniformly for all 5 channels. Idempotent.

    Returns True on success, False if radio is not a WM1303Backend-like object
    or the hook is already installed.
    """
    if radio is None:
        logger.warning('install_on_radio: radio is None')
        return False
    if getattr(radio, _INSTALLED_MARK, False):
        logger.debug('install_on_radio: already installed on %r', type(radio).__name__)
        return False

    # ---- Hook 1: _dispatch_rx (channels a/b/c/d) --------------------------
    orig_dispatch = getattr(radio, '_dispatch_rx', None)
    if callable(orig_dispatch):
        def _traced_dispatch_rx(rxpk: dict, _orig=orig_dispatch, _radio=radio) -> None:
            # Emit trace BEFORE original (so CRC_ERROR / early-drop are visible).
            try:
                _freq = rxpk.get('freq', 0)
                _datr = rxpk.get('datr', '?')
                _rssi = rxpk.get('rssi', -120)
                _lsnr = rxpk.get('lsnr', 0.0)
                _stat = rxpk.get('stat', -1)
                _b64  = rxpk.get('data', '')
                if _b64:
                    try:
                        _payload = base64.b64decode(_b64)
                    except Exception:
                        _payload = b''
                    if len(_payload) >= 5:  # align with pymc noise filter
                        _freq_hz = int(float(_freq) * 1e6) if _freq else 0
                        _ch_id = None
                        _ftc = getattr(_radio, '_freq_to_ch_id', {}) or {}
                        if _freq_hz:
                            _ch_id = _ftc.get(_freq_hz)
                            if _ch_id is None:
                                # tolerate ±1 kHz drift
                                for _k, _v in _ftc.items():
                                    if abs(_k - _freq_hz) <= 1000:
                                        _ch_id = _v
                                        break
                        if _ch_id:
                            _ui_map = getattr(_radio, '_ch_id_to_ui_name', {}) or {}
                            _ui_name = _ui_map.get(_ch_id) or _ch_id
                            _crc_ok = (_stat == 1)
                            _fire_received(_ch_id, _ui_name, _payload,
                                           _rssi, _lsnr, _crc_ok, _stat)
            except Exception as _pre_err:
                logger.debug('traced _dispatch_rx pre-hook error: %s', _pre_err)
            return _orig(rxpk)
        try:
            radio._dispatch_rx = _traced_dispatch_rx  # type: ignore[assignment]
            logger.info('UniformTracer: installed _dispatch_rx hook (channel_a/b/c/d)')
        except Exception as _he:
            logger.warning('UniformTracer: could not install _dispatch_rx hook: %s', _he)

    # ---- Hook 2: channel_e callback wrapper --------------------------------
    # We wrap the property/attribute indirectly: replace the callback setter by
    # monitoring the attribute. Since WM1303Backend stores _channel_e_rx_callback
    # as a plain attribute (set by channel_e_bridge on startup), we install a
    # descriptor-style property substitute that logs on every call.
    try:
        _existing_cb = getattr(radio, '_channel_e_rx_callback', None)
        # We cannot easily intercept future assignments to a plain attribute,
        # so defer the wrap: whenever _channel_e_rx_callback is set, ensure our
        # wrapper is applied. Implement via a wrapper-install helper that the
        # caller invokes after channel_e_bridge is ready.
        radio._uniform_tracer_wrap_channel_e = lambda: _wrap_channel_e_callback(radio)
        # Try once now in case a callback is already set
        _wrap_channel_e_callback(radio)
    except Exception as _ee:
        logger.debug('UniformTracer: channel_e wrap setup failed: %s', _ee)

    setattr(radio, _INSTALLED_MARK, True)
    return True


def _wrap_channel_e_callback(radio: Any) -> None:
    """Wrap `radio._channel_e_rx_callback` (if present) so a `received` trace
    fires for channel_e too, BEFORE the original handler runs."""
    cb = getattr(radio, '_channel_e_rx_callback', None)
    if cb is None:
        return
    # Avoid double-wrapping
    if getattr(cb, '_uniform_tracer_wrapped', False):
        return

    _ui_map = getattr(radio, '_ch_id_to_ui_name', {}) or {}
    _ui_name = _ui_map.get('channel_e') or 'channel_e'

    def _wrapped_e(payload: bytes, rssi: int = -120, snr: float = 0.0,
                   _orig=cb, _ui=_ui_name) -> None:
        try:
            if payload and len(payload) >= 5:
                _fire_received('channel_e', _ui, payload, rssi, snr, True, 1)
        except Exception as _fe:
            logger.debug('channel_e uniform trace error: %s', _fe)
        return _orig(payload, rssi=rssi, snr=snr)

    try:
        setattr(_wrapped_e, '_uniform_tracer_wrapped', True)
        radio._channel_e_rx_callback = _wrapped_e  # type: ignore[assignment]
        logger.info('UniformTracer: wrapped channel_e RX callback (%s)', _ui_name)
    except Exception as _we:
        logger.debug('UniformTracer: could not wrap channel_e callback: %s', _we)


def schedule_channel_e_wrap_retry(radio: Any, max_attempts: int = 20,
                                   interval_sec: float = 1.0) -> None:
    """Retry wrapping channel_e RX callback in a background thread.

    ChannelEBridge installs `_channel_e_rx_callback` via an async task after
    startup, so an immediate wrap often runs BEFORE the callback is set.
    This retry loop polls the attribute for up to ~max_attempts * interval_sec
    seconds and wraps as soon as it appears.
    """
    if radio is None:
        return

    def _retry_loop() -> None:
        for _i in range(max_attempts):
            try:
                cb = getattr(radio, '_channel_e_rx_callback', None)
                if cb is not None and not getattr(cb, '_uniform_tracer_wrapped', False):
                    _wrap_channel_e_callback(radio)
                    # If wrap succeeded, it flips the _uniform_tracer_wrapped flag
                    new_cb = getattr(radio, '_channel_e_rx_callback', None)
                    if getattr(new_cb, '_uniform_tracer_wrapped', False):
                        logger.info('UniformTracer: channel_e wrap succeeded after %ds', _i + 1)
                        return
            except Exception as _e:
                logger.debug('UniformTracer: retry attempt %d error: %s', _i, _e)
            time.sleep(interval_sec)
        logger.warning('UniformTracer: channel_e wrap retry gave up after %d attempts (no callback appeared)', max_attempts)

    t = threading.Thread(target=_retry_loop, name='UniformTracerChE', daemon=True)
    t.start()
