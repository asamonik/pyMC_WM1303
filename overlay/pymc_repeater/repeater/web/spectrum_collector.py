"""Spectrum data collector - polls /tmp/pymc_spectral_results.json, stores in SQLite.

The Semtech HAL writes spectral scan results to /tmp/pymc_spectral_results.json
(and /tmp/spectral_debug.log). This collector polls that JSON file every 60s.
Previously this module tailed journalctl, but HAL output never reaches stdout
when results are only written to files.

CAD and LBT events are tracked separately in repeater.db by the
_packet_activity_recorder (wm1303_api.py). This collector only handles
spectral scan data.

Retention/cleanup is handled centrally by repeater.metrics_retention.
"""
import sqlite3
import threading
import time
import os
import json
import logging
import math
from pathlib import Path
from contextlib import closing

logger = logging.getLogger('spectrum_collector')

DB_PATH = '/var/lib/openhop_repeater/spectrum_history.db'
JSON_PATH = '/tmp/pymc_spectral_results.json'
POLL_INTERVAL_S = 60

# RSSI histogram constants kept for backward compatibility with legacy helpers.
RSSI_BIN_START = -140.0  # dBm for bin 0
RSSI_BIN_STEP = 2.0      # dBm per bin
NUM_BINS = 33


def histogram_to_rssi(bins):
    """Convert a list of histogram bin counts to a weighted average RSSI (dBm).
    Returns None if all bins are zero."""
    total = sum(bins)
    if total == 0:
        return None
    weighted = 0.0
    for i, count in enumerate(bins):
        rssi_center = RSSI_BIN_START + (i * RSSI_BIN_STEP) + (RSSI_BIN_STEP / 2)
        weighted += rssi_center * count
    return weighted / total


def histogram_to_peak_rssi(bins):
    """Return the RSSI of the highest-count bin (peak energy)."""
    if not bins or max(bins) == 0:
        return None
    peak_idx = bins.index(max(bins))
    return RSSI_BIN_START + (peak_idx * RSSI_BIN_STEP) + (RSSI_BIN_STEP / 2)


class SpectrumCollector:
    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path
        self._running = False
        self._thread = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        logger.info(f'SpectrumCollector initialized, db={db_path}')

    def _init_db(self):
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS spectrum_scans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                freq_mhz REAL NOT NULL,
                rssi_dbm REAL NOT NULL
            )''')
            # Legacy tables kept for backward compatibility (not actively written)
            conn.execute('''CREATE TABLE IF NOT EXISTS lbt_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                channel_freq_hz INTEGER NOT NULL,
                rssi_dbm REAL,
                channel_clear INTEGER NOT NULL,
                tx_allowed INTEGER NOT NULL
            )''')
            conn.execute('''CREATE TABLE IF NOT EXISTS cad_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                freq_hz INTEGER,
                cad_detected INTEGER NOT NULL,
                rssi_dbm REAL
            )''')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_spec_ts ON spectrum_scans(timestamp)')

    def start(self):
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._running = True
            self._thread = threading.Thread(target=self._collect_loop, daemon=True,
                                            name='spectrum-collector')
            try:
                self._thread.start()
            except BaseException:
                self._running = False
                self._thread = None
                raise
        logger.info('SpectrumCollector started - polling %s every %ds', JSON_PATH, POLL_INTERVAL_S)

    def stop(self):
        if self._thread is threading.current_thread():
            self._stop.set()
            raise RuntimeError('SpectrumCollector cannot join its own worker')
        with self._lock:
            self._stop.set()
            if self._thread is not None:
                # A database commit can outlast SQLite's lock timeout. The
                # singleton must not discard this worker until it really exits.
                self._thread.join()
            self._running = False

    def _collect_loop(self):
        try:
            self._collect_scans()
        finally:
            self._running = False

    def _collect_scans(self):
        """Poll /tmp/pymc_spectral_results.json every POLL_INTERVAL_S and store rows."""
        last_ts = 0.0
        while not self._stop.is_set():
            try:
                if os.path.exists(JSON_PATH):
                    size = os.path.getsize(JSON_PATH)
                    if size == 0:
                        # Empty file — sweep likely disabled, skip silently
                        pass
                    else:
                        with open(JSON_PATH) as f:
                            data = json.load(f)
                        raw_ts = data.get('timestamp')
                        ts = float(raw_ts)
                        if isinstance(raw_ts, bool) or not math.isfinite(ts) or ts <= 0:
                            raise ValueError('Spectral scan has no valid observation timestamp')
                        if ts > last_ts:
                            channels = data.get('channels') or {}
                            readings = []
                            for freq_str, ch in channels.items():
                                try:
                                    freq_hz = int(freq_str)
                                    rssi = ch.get('rssi_avg')
                                    if freq_hz <= 0 or rssi is None or isinstance(rssi, bool):
                                        continue
                                    # HAL writes numeric placeholders for failed
                                    # scans with samples=0. Missing sample counts
                                    # in legacy genuine readings remain accepted.
                                    if 'samples' in ch:
                                        samples = float(ch['samples'])
                                        if isinstance(ch['samples'], bool) or not math.isfinite(samples) or samples <= 0:
                                            continue
                                    rssi = float(rssi)
                                    if not math.isfinite(rssi):
                                        continue
                                    readings.append((freq_hz / 1e6, rssi))
                                except Exception as e:
                                    logger.debug(f'Skipping channel {freq_str}: {e}')
                            if not readings or self._store_spectrum(ts, readings):
                                if readings:
                                    logger.debug('SpectrumCollector stored %d channels @ ts=%s', len(readings), ts)
                                last_ts = ts
            except json.JSONDecodeError:
                # Malformed JSON (truncated write, etc.) — skip silently
                logger.debug('Spectrum poll: JSON file empty or malformed, skipping')
            except Exception as e:
                logger.warning(f'Spectrum poll error: {e}')
            if self._stop.wait(POLL_INTERVAL_S):
                return

    def _store_spectrum(self, ts, readings):
        """Commit one scan atomically so a failed write can be retried."""
        try:
            with closing(sqlite3.connect(self.db_path)) as conn, conn:
                conn.executemany(
                    'INSERT INTO spectrum_scans(timestamp,freq_mhz,rssi_dbm) VALUES(?,?,?)',
                    ((ts, freq_mhz, rssi_dbm) for freq_mhz, rssi_dbm in readings),
                )
            return True
        except Exception as e:
            logger.error(f'Store spectrum error: {e}')
            return False


    def get_spectrum_history(self, hours=24):
        cutoff = time.time() - (hours * 3600)
        try:
            with closing(sqlite3.connect(self.db_path)) as conn:
                rows = conn.execute(
                    'SELECT timestamp, freq_mhz, rssi_dbm FROM spectrum_scans WHERE timestamp >= ? ORDER BY timestamp',
                    (cutoff,)
                ).fetchall()
            return [{'timestamp': r[0], 'freq_mhz': r[1], 'rssi_dbm': r[2]} for r in rows]
        except Exception as e:
            logger.error(f'Get spectrum history error: {e}')
            return []



# Singleton
_collector = None
_collector_lock = threading.Lock()


def get_collector(db_path=None):
    global _collector
    with _collector_lock:
        if _collector is not None and db_path is not None and str(_collector.db_path) != str(db_path):
            _collector.stop()
            _collector = None
        if _collector is None:
            _collector = SpectrumCollector(db_path or DB_PATH)
        _collector.start()
        return _collector


def stop_collector():
    global _collector
    with _collector_lock:
        if _collector is not None:
            _collector.stop()
            _collector = None
