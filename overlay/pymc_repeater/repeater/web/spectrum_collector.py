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
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._init_db()
        logger.info(f'SpectrumCollector initialized, db={db_path}')

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS spectrum_scans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL NOT NULL,
            freq_mhz REAL NOT NULL,
            rssi_dbm REAL NOT NULL
        )''')
        # Legacy tables kept for backward compatibility (not actively written)
        c.execute('''CREATE TABLE IF NOT EXISTS lbt_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL NOT NULL,
            channel_freq_hz INTEGER NOT NULL,
            rssi_dbm REAL,
            channel_clear INTEGER NOT NULL,
            tx_allowed INTEGER NOT NULL
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS cad_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL NOT NULL,
            freq_hz INTEGER,
            cad_detected INTEGER NOT NULL,
            rssi_dbm REAL
        )''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_spec_ts ON spectrum_scans(timestamp)')
        conn.commit()
        conn.close()

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._collect_loop, daemon=True)
        self._thread.start()
        logger.info('SpectrumCollector started - polling %s every %ds', JSON_PATH, POLL_INTERVAL_S)

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)

    def _collect_loop(self):
        """Poll /tmp/pymc_spectral_results.json every POLL_INTERVAL_S and store rows."""
        last_ts = 0.0
        while self._running:
            try:
                if os.path.exists(JSON_PATH):
                    size = os.path.getsize(JSON_PATH)
                    if size == 0:
                        # Empty file — sweep likely disabled, skip silently
                        pass
                    else:
                        with open(JSON_PATH) as f:
                            data = json.load(f)
                        ts = float(data.get('timestamp', time.time()))
                        if ts > last_ts:
                            channels = data.get('channels') or {}
                            stored = 0
                            for freq_str, ch in channels.items():
                                try:
                                    freq_hz = int(freq_str)
                                    rssi = ch.get('rssi_avg')
                                    if rssi is None:
                                        continue
                                    self._store_spectrum(ts, freq_hz / 1e6, float(rssi))
                                    stored += 1
                                except Exception as e:
                                    logger.debug(f'Skipping channel {freq_str}: {e}')
                            if stored:
                                logger.debug('SpectrumCollector stored %d channels @ ts=%s', stored, ts)
                            last_ts = ts
            except json.JSONDecodeError:
                # Malformed JSON (truncated write, etc.) — skip silently
                logger.debug('Spectrum poll: JSON file empty or malformed, skipping')
            except Exception as e:
                logger.warning(f'Spectrum poll error: {e}')
            # Sleep in 5s chunks for clean shutdown
            for _ in range(POLL_INTERVAL_S // 5):
                if not self._running:
                    return
                time.sleep(5)

    def _store_spectrum(self, ts, freq_mhz, rssi_dbm):
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute('INSERT INTO spectrum_scans(timestamp,freq_mhz,rssi_dbm) VALUES(?,?,?)',
                        (ts, freq_mhz, rssi_dbm))
            # Cleanup moved to metrics_retention.py
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f'Store spectrum error: {e}')


    def get_spectrum_history(self, hours=24):
        cutoff = time.time() - (hours * 3600)
        try:
            conn = sqlite3.connect(self.db_path)
            rows = conn.execute(
                'SELECT timestamp, freq_mhz, rssi_dbm FROM spectrum_scans WHERE timestamp >= ? ORDER BY timestamp',
                (cutoff,)
            ).fetchall()
            conn.close()
            return [{'timestamp': r[0], 'freq_mhz': r[1], 'rssi_dbm': r[2]} for r in rows]
        except Exception as e:
            logger.error(f'Get spectrum history error: {e}')
            return []



# Singleton
_collector = None
def get_collector():
    global _collector
    if _collector is None:
        _collector = SpectrumCollector()
        _collector.start()
    return _collector
