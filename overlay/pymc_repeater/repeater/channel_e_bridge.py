"""Channel E (native LoRa) Bridge Plugin for pymc-repeater.

Minimal plugin that:
- Listens on UDP port 1733 for LoRa packets from the Channel E RX daemon
- Injects them into the BridgeEngine (RX path)
- Handles TX by routing packets through the existing GlobalTXScheduler

Channel E parameters (frequency, SF, BW, CR, preamble, tx_power) are
read dynamically from /etc/openhop_repeater/wm1303_ui.json (or legacy /etc/pymc_repeater/wm1303_ui.json) at runtime.

TX uses the SX1302/SX1250/SKY66420 path via PULL_RESP to lora_pkt_fwd.

Usage (inside pymc-repeater):
    from channel_e_bridge import ChannelEBridge
    ch_e = ChannelEBridge(bridge_engine, backend=radio)
    asyncio.create_task(ch_e.run())
"""
import asyncio
import json
import logging
import socket
import hashlib
from pathlib import Path
from repeater.bridge_engine import _stable_hash
from repeater.web.packet_trace import trace_event as _trace
from openhop_core.paths import resolve_config_path  # WM1303 v2.7: central config-path helper

logger = logging.getLogger(__name__)

CHANNEL_E_NAME = 'channel_e'
CHANNEL_E_UDP_PORT = 1733
CHANNEL_E_TX_CHANNEL = 'channel_e'
UI_CONFIG_PATH = resolve_config_path('wm1303_ui.json')


def _load_channel_e_ui() -> dict:
    """Load channel_e settings from wm1303_ui.json."""
    try:
        if UI_CONFIG_PATH.exists():
            return json.loads(UI_CONFIG_PATH.read_text()).get('channel_e', {})
    except Exception as e:
        logger.warning('ChannelEBridge: failed to read UI config: %s', e)
    return {}


class ChannelEBridge:
    """Async UDP listener that feeds Channel E decoded packets into BridgeEngine."""

    def __init__(self, bridge_engine, udp_port=CHANNEL_E_UDP_PORT, backend=None):
        self.bridge = bridge_engine
        self.udp_port = udp_port
        self.backend = backend  # WM1303Backend for TX
        self.packets_received = 0
        self.packets_injected = 0
        self.packets_errors = 0
        self.tx_packets = 0
        self.tx_errors = 0
        self._running = False
        self._task = None
        self._rx_tasks = set()
        self._loop = None  # stored in run() for cross-thread async injection

    async def _tx_handler(self, data: bytes):
        """Handle packets forwarded TO Channel E - TX via HAL.

        Uses the SX1302/SX1250/SKY66420 TX path with parameters from
        the channel_e TX queue (configured from wm1303_ui.json).

        Channel E TX routes through the SAME TXQueueManager /
        GlobalTXScheduler / _send_pull_resp path as all other channels,
        so by passing `trace_hash=pkt_hash8` into `queue.enqueue()`
        the backend emits the full set of enriched trace events
        (tx_noisefloor, cad_start, lbt_check, cad_check, rf_tx_start,
        rf_tx_end, sx1261_rx_restart, tx_ack) automatically and with
        correct chronological timing. This makes channel_e traces
        visually identical to other channels in the Tracing UI.
        """
        pkt_hash8 = _stable_hash(data)[:8]
        # Friendly channel display name (e.g. "EU-Narrow") — never show raw id.
        try:
            _friendly = self.bridge._dn(CHANNEL_E_TX_CHANNEL) if self.bridge else CHANNEL_E_TX_CHANNEL
        except Exception:
            _friendly = CHANNEL_E_TX_CHANNEL
        if self.backend is None:
            self.tx_errors += 1
            logger.warning('Channel E TX: no backend, cannot send %d bytes', len(data))
            _trace(pkt_hash8, 'tx_send', channel=CHANNEL_E_TX_CHANNEL,
                   detail='TX FAILED on %s: no backend available' % _friendly,
                   status='error')
            return {'ok': False, 'error': 'no_backend'}

        try:
            # Route through existing TX queue (GlobalTXScheduler)
            if hasattr(self.backend, '_tx_queue_manager') and self.backend._tx_queue_manager:
                queue = self.backend._tx_queue_manager.queues.get(CHANNEL_E_TX_CHANNEL)
                if queue:
                    result = await queue.enqueue(data, trace_hash=pkt_hash8)
                    if result.get('ok'):
                        self.tx_packets += 1
                        logger.info(
                            'Channel E TX: sent %d bytes via HAL '
                            '(send=%.1fms, airtime=%.1fms)',
                            len(data),
                            result.get('send_ms', 0),
                            result.get('airtime_ms', 0)
                        )
                        # lbt_check + cad_check + rf_tx_start/end +
                        # sx1261_rx_restart + tx_ack are emitted
                        # automatically inside _emit_tx_phase_trace
                        # (backend) before we reach this code path.
                        # Rich multi-line tx_send detail (Frequency / Datarate /
                        # Airtime / Queue wait / Send) via shared formatter.
                        try:
                            _detail = self.bridge._format_tx_result(
                                result, _friendly, 'Repeater → %s' % _friendly)
                        except Exception:
                            # Fallback to a minimal detail if formatter fails.
                            _send_ms = result.get('send_ms', 0)
                            _airtime_ms = result.get('airtime_ms', 0)
                            _queue_wait = result.get('queue_wait_ms', 0)
                            _detail = ('TX on %s\n  Send: %.1fms\n  Airtime: %.1fms'
                                       '\n  Queue wait: %.1fms' % (
                                           _friendly, _send_ms, _airtime_ms, _queue_wait))
                        _trace(pkt_hash8, 'tx_send', channel=CHANNEL_E_TX_CHANNEL,
                               detail=_detail,
                               status='ok')
                        # Store per-packet TX metric for spectrum-tab charts (Option B).
                        try:
                            _handler = getattr(self.bridge, '_sqlite_handler', None) if self.bridge else None
                            if _handler is not None:
                                import time as _t
                                _handler.store_packet_metric({
                                    'timestamp': _t.time(),
                                    'channel_id': str(CHANNEL_E_TX_CHANNEL),
                                    'direction': 'tx',
                                    'length': int(len(data)),
                                    'hop_count': None,
                                    'crc_ok': True,
                                    'airtime_ms': float(result.get('airtime_ms', 0) or 0),
                                    'wait_time_ms': float(result.get('queue_wait_ms', 0) or 0),
                                    'pkt_hash': pkt_hash8 if isinstance(pkt_hash8, str) else None,
                                })
                        except Exception as _pm_e:
                            logger.warning('packet_metric TX(ch_e) store failed: %s', _pm_e)
                    else:
                        self.tx_errors += 1
                        logger.warning(
                            'Channel E TX FAIL: %s (%d bytes)',
                            result.get('error', 'unknown'), len(data)
                        )
                        # Enriched trace events (if any) were already
                        # emitted by _emit_tx_phase_trace inside the
                        # backend for TXs that reached the HAL stage.
                        # Here we just record the final tx_send error.
                        _trace(pkt_hash8, 'tx_send', channel=CHANNEL_E_TX_CHANNEL,
                               detail='TX FAILED on %s: %s' % (_friendly, result.get('error', 'unknown')),
                               status='error')
                    return result
                else:
                    logger.warning('Channel E TX: no channel_e queue found in TXQueueManager')
            else:
                logger.warning('Channel E TX: no TXQueueManager on backend')

            # Fallback: try backend.send() directly with UI-configured tx_power
            _ui = _load_channel_e_ui()
            _tx_power = int(_ui.get('tx_power', 27))
            meta = await self.backend.send(CHANNEL_E_TX_CHANNEL, data, tx_power=_tx_power, trace_hash=pkt_hash8)
            if not isinstance(meta, dict) or not meta.get('ok'):
                self.tx_errors += 1
                result = meta if isinstance(meta, dict) else {'ok': False, 'error': 'invalid_send_result'}
                _trace(pkt_hash8, 'tx_send', channel=CHANNEL_E_TX_CHANNEL,
                       detail='TX FAILED on %s: %s' % (_friendly, result.get('error', 'unknown')),
                       status='error')
                return result
            self.tx_packets += 1
            logger.info('Channel E TX: sent %d bytes via backend.send() (tx_power=%d)',
                       len(data), _tx_power)
            # backend.send() emits enriched events itself (via
            # _emit_tx_phase_trace) when trace_hash is provided, so we
            # only need to append the final tx_send summary here.
            _trace(pkt_hash8, 'tx_send', channel=CHANNEL_E_TX_CHANNEL,
                   detail='TX on %s via backend.send() (tx_power=%d dBm, %d bytes)' % (_friendly, _tx_power, len(data)),
                   status='ok')
            return meta
        except Exception as e:
            self.tx_errors += 1
            logger.error('Channel E TX error: %s', e)
            _trace(pkt_hash8, 'tx_send', channel=CHANNEL_E_TX_CHANNEL,
                   detail='TX FAILED on %s: %s' % (_friendly, e),
                   status='error')
            return {'ok': False, 'error': str(e)}


    def _rx_from_backend(self, payload, rssi=0, snr=0.0):
        """Callback invoked by WM1303Backend when a channel_e-frequency packet arrives."""
        self.packets_received += 1
        pkt_hash = _stable_hash(payload)
        pkt_hash8 = pkt_hash[:8] if pkt_hash else _stable_hash(payload)[:8]
        logger.info("Channel E RX (via backend): %dB rssi=%d snr=%.1f hash=%s",
                    len(payload), rssi, snr, pkt_hash)
        # Emit a `received` trace step BEFORE inject_packet so it becomes the
        # first step in the Tracing UI for channel_e packets. RSSI/SNR come
        # straight from the SX1261 RX metadata. Packet type is looked up via
        # the bridge helper for consistent labelling across channels.
        try:
            if self.bridge is not None and hasattr(self.bridge, 'emit_received_trace'):
                _pt = 'UNKNOWN'
                try:
                    _pt = self.bridge._get_packet_type_name(payload)
                except Exception:
                    pass
                self.bridge.emit_received_trace(
                    pkt_hash8, CHANNEL_E_NAME, _pt, len(payload),
                    rssi=rssi, snr=snr)
        except Exception as _te:
            logger.debug('Channel E RX: received-trace emit failed: %s', _te)
        try:
            if self._loop is not None and self._loop.is_running():
                self._loop.call_soon_threadsafe(
                    self._schedule_rx, payload, rssi, snr)
            else:
                logger.warning("Channel E RX: no event loop stored, cannot inject")
        except Exception as e:
            self.packets_errors += 1
            logger.error("Channel E RX inject error: %s", e)

    def _schedule_rx(self, payload, rssi, snr):
        if not self._running:
            return
        # Construct the coroutine on its event loop, avoiding leaked
        # coroutine objects when a backend callback races loop shutdown.
        async def inject():
            try:
                accepted = await self.bridge.inject_packet(
                    CHANNEL_E_NAME, payload, origin_channel=CHANNEL_E_NAME,
                    rssi=rssi, snr=snr)
                if accepted is not False:
                    self.packets_injected += 1
            except Exception:
                self.packets_errors += 1
                logger.exception("Channel E RX injection failed")
        task = asyncio.create_task(inject())
        self._rx_tasks.add(task)
        task.add_done_callback(self._rx_tasks.discard)


    async def _wait_for_bridge(self, timeout=30):
        """Wait for bridge_engine to be fully initialized."""
        for i in range(timeout * 10):
            if (self.bridge is not None
                    and hasattr(self.bridge, '_endpoint_handlers')
                    and self.bridge._endpoint_handlers is not None):
                return True
            await asyncio.sleep(0.1)
        return False

    async def run(self):
        """Register the endpoint until stopped; release callbacks on every exit."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.current_task()
        sock = None
        try:
            if not await self._wait_for_bridge():
                logger.error('Channel E: bridge engine not ready after timeout')
                return
            self._loop = asyncio.get_running_loop()
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(('127.0.0.1', self.udp_port))
            sock.setblocking(False)
            self._running = True
            self.bridge._endpoint_handlers[CHANNEL_E_NAME] = self._tx_handler
            if self.backend is not None:
                self.backend._channel_e_rx_callback = self._rx_from_backend
            logger.info('Channel E: bridge endpoint active')
            while self._running:
                data = await self._loop.sock_recv(sock, 4096)
                if not data or len(data) < 2:
                    continue
                self.packets_received += 1
                try:
                    accepted = await self.bridge.inject_packet(
                        CHANNEL_E_NAME, data, origin_channel=CHANNEL_E_NAME)
                    if accepted is not False:
                        self.packets_injected += 1
                except Exception:
                    self.packets_errors += 1
                    logger.exception('Channel E UDP injection failed')
        except asyncio.CancelledError:
            pass
        finally:
            # This socket is receive-only; queued injections transmit through
            # the backend. Close it before any cleanup can raise or be cancelled
            # again while awaiting those injections.
            if sock is not None:
                sock.close()
            self._task = None
            self.stop()
            if self._rx_tasks:
                await asyncio.gather(*tuple(self._rx_tasks), return_exceptions=True)

    def stop(self):
        self._running = False
        self._loop = None
        handlers = getattr(self.bridge, '_endpoint_handlers', {})
        if handlers.get(CHANNEL_E_NAME) == self._tx_handler:
            handlers.pop(CHANNEL_E_NAME, None)
        if (self.backend is not None
                and getattr(self.backend, '_channel_e_rx_callback', None) == self._rx_from_backend):
            self.backend._channel_e_rx_callback = None
        task = self._task
        if task is not None and not task.done():
            task.get_loop().call_soon_threadsafe(task.cancel)
        for task in tuple(self._rx_tasks):
            if not task.done():
                task.get_loop().call_soon_threadsafe(task.cancel)

    def get_stats(self) -> dict:
        return {
            'received': self.packets_received,
            'injected': self.packets_injected,
            'errors': self.packets_errors,
            'tx_packets': self.tx_packets,
            'tx_errors': self.tx_errors,
        }
