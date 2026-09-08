"""Centralized metrics retention for pyMC_Repeater (WM1303).

Implements tiered downsampling to reduce database size while preserving
historical trends:

  Tier      | Period    | Resolution    | Action
  ----------|-----------|---------------|-----------------------------------------
  Hot       | 0-7h      | Full          | Keep all original data points
  Warm      | 7h-3 days | 1 minute      | Aggregate into _1m summary tables
  Cold      | 3-8 days  | 15 minutes    | Aggregate into _15m summary tables
  Expired   | >8 days   | Deleted       | Remove from all tables

Legacy _10m summaries remain readable until expiry; a 10-minute bucket cannot
be split truthfully into 15-minute buckets. Cumulative channel snapshots stay
raw until expiry (plus one baseline per retained channel), preserving the
transitions between samples. The eight-day limit is the configurable default.
After each cleanup pass, a WAL TRUNCATE checkpoint is performed.
"""
import logging
import os
import sqlite3
import threading
import ctypes
import ctypes.util
import functools
import math

from contextlib import contextmanager as _contextmanager

class _SharedConn:
    """Module-level shared SQLite connection with thread-safe access."""

    def __init__(self, path):
        self._path = str(path)
        self._conn = None
        self._lock = threading.RLock()

    def _ensure_conn(self):
        if self._conn is None:
            conn = sqlite3.connect(
                self._path, timeout=10, check_same_thread=False,
            )
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute("PRAGMA busy_timeout=5000")
                conn.execute("PRAGMA cache_size=-512")
                conn.execute("PRAGMA mmap_size=0")
                conn.execute("PRAGMA temp_store=MEMORY")
            except BaseException:
                conn.close()
                raise
            self._conn = conn
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
                    try:
                        self._conn.commit()
                    except BaseException:
                        self._conn.rollback()
                        raise
                else:
                    self._conn.rollback()
        finally:
            self._lock.release()
        return False

    def close(self):
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None


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

import time
from typing import List, Tuple, Optional, Dict

logger = logging.getLogger("metrics_retention")

# --- malloc_trim helper ---------------------------------------------------
# Python's allocator (pymalloc + glibc malloc) holds onto freed memory in
# per-arena free lists and does not release it to the OS.  After a metrics
# cleanup or WAL checkpoint (which frees a lot of short-lived objects) we
# explicitly ask glibc to return released pages to the kernel via
# malloc_trim(0).  No-op on systems without glibc.
try:
    _libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
    if hasattr(_libc, "malloc_trim"):
        _libc.malloc_trim.argtypes = [ctypes.c_size_t]
        _libc.malloc_trim.restype = ctypes.c_int
        _HAS_MALLOC_TRIM = True
    else:
        _HAS_MALLOC_TRIM = False
except Exception:  # pragma: no cover - defensive
    _libc = None
    _HAS_MALLOC_TRIM = False


def malloc_trim() -> None:
    """Return freed memory pages to the kernel (glibc only)."""
    if _HAS_MALLOC_TRIM:
        try:
            _libc.malloc_trim(0)
        except Exception:  # pragma: no cover - defensive
            pass

DEFAULT_RETENTION_DAYS = 8
DEFAULT_CLEANUP_INTERVAL_S = 3600        # once per hour
DEFAULT_VACUUM_INTERVAL_S = 7 * 86400    # weekly

# Tier boundaries (in seconds from now)
TIER_HOT_SECONDS = 7 * 3600              # 7 hours
TIER_COOL_SECONDS = 3 * 86400            # 3 days
# Cold = 3-8 days (until retention_days)

# Aggregation bucket sizes (in seconds)
BUCKET_1M = 60
BUCKET_10M = 600
BUCKET_15M = 900

# Tables that should only be deleted after retention (no downsampling)
# These are either already compact or not suitable for aggregation.
DELETE_ONLY_TABLES: List[Tuple[str, str, str]] = [
    ("repeater.db",         "packets",                 "timestamp"),
    ("repeater.db",         "adverts",                 "timestamp"),
    ("repeater.db",         "crc_errors",              "timestamp"),
    # Bug fix: invalid_packets was missing from retention -> rows lived past
    # the 8-day policy (design-doc requirement); table would grow forever.
    ("repeater.db",         "invalid_packets",         "timestamp"),
    ("repeater.db",         "noise_floor",             "timestamp"),
    ("repeater.db",         "sx1261_health_events",    "timestamp"),
    ("spectrum_history.db", "spectrum_scans",          "timestamp"),
]

# Tables that get tiered downsampling.
# (db_name, source_table, ts_col, aggregation_config)
# The aggregation_config defines how to aggregate each table.
DOWNSAMPLE_TABLES: List[Dict] = [
    {
        "db": "repeater.db",
        "table": "packet_metrics",
        "ts_col": "timestamp",
        "group_cols": ["channel_id", "direction"],
        "agg_cols": [
            ("COUNT(*)",        "sample_count"),
            ("AVG(rssi)",       "avg_rssi"),
            ("MIN(rssi)",       "min_rssi"),
            ("MAX(rssi)",       "max_rssi"),
            ("AVG(snr)",        "avg_snr"),
            ("MIN(snr)",        "min_snr"),
            ("MAX(snr)",        "max_snr"),
            ("AVG(airtime_ms)", "avg_airtime_ms"),
            ("SUM(airtime_ms)", "total_airtime_ms"),
            ("SUM(length)",     "total_bytes"),
            ("AVG(hop_count)",  "avg_hop_count"),
            ("SUM(CASE WHEN crc_ok=0 THEN 1 ELSE 0 END)", "crc_error_count"),
        ],
    },
    {
        "db": "repeater.db",
        "table": "dedup_events",
        "ts_col": "ts",
        "group_cols": ["event_type", "source"],
        "agg_cols": [
            ("COUNT(*)",                "sample_count"),
            ("COUNT(DISTINCT pkt_hash)", "unique_packets"),
            ("SUM(pkt_size)",           "total_bytes"),
        ],
    },
    {
        "db": "repeater.db",
        "table": "noise_floor_history",
        "ts_col": "timestamp",
        "group_cols": ["channel_id"],
        "agg_cols": [
            ("COUNT(*)",                    "sample_count"),
            ("AVG(noise_floor_dbm)",        "avg_noise_floor_dbm"),
            ("MIN(noise_floor_dbm)",        "min_noise_floor_dbm"),
            ("MAX(noise_floor_dbm)",        "max_noise_floor_dbm"),
            ("SUM(samples_collected)",      "total_samples_collected"),
            ("SUM(samples_accepted)",       "total_samples_accepted"),
            ("MIN(min_rssi)",               "min_rssi"),
            ("MAX(max_rssi)",               "max_rssi"),
        ],
    },
    {
        "db": "repeater.db",
        "table": "cad_events",
        "ts_col": "timestamp",
        "group_cols": ["channel_id"],
        "agg_cols": [
            ("COUNT(*)",            "sample_count"),
            ("SUM(cad_clear)",      "total_cad_clear"),
            ("SUM(cad_detected)",   "total_cad_detected"),
            ("SUM(cad_skipped)",    "total_cad_skipped"),
            ("SUM(cad_hw_clear)",   "total_cad_hw_clear"),
            ("SUM(cad_hw_detected)", "total_cad_hw_detected"),
            ("SUM(cad_sw_clear)",   "total_cad_sw_clear"),
            ("SUM(cad_sw_detected)", "total_cad_sw_detected"),
        ],
    },
    {
        "db": "repeater.db",
        "table": "channel_stats_history",
        "ts_col": "timestamp",
        "group_cols": ["channel_id"],
        "agg_cols": [
            ("COUNT(*)",                "sample_count"),
            # Legacy schema only: new cumulative snapshots are NOT rolled
            # up. MAX-MIN loses transitions between buckets and resets.
            # Keep raw snapshots (~69,120 rows at six channels/eight days)
            # and calculate reset-aware differences when querying.
            ("MAX(rx_count) - MIN(rx_count)",             "total_rx_count"),
            ("AVG(avg_rssi)",                             "avg_rssi"),
            ("AVG(avg_snr)",                              "avg_snr"),
            ("MAX(tx_count) - MIN(tx_count)",             "total_tx_count"),
            ("MAX(tx_failed) - MIN(tx_failed)",           "total_tx_failed"),
            ("MAX(tx_airtime_ms) - MIN(tx_airtime_ms)",   "total_tx_airtime_ms"),
            ("MAX(tx_bytes) - MIN(tx_bytes)",             "total_tx_bytes"),
            ("MAX(lbt_blocked) - MIN(lbt_blocked)",       "total_lbt_blocked"),
            ("MAX(lbt_passed) - MIN(lbt_passed)",         "total_lbt_passed"),
            ("AVG(noise_floor_dbm)",                      "avg_noise_floor_dbm"),
            ("AVG(tx_noisefloor_dbm)",                    "avg_tx_noisefloor_dbm"),
        ],
    },
    {
        "db": "repeater.db",
        "table": "packet_activity",
        "ts_col": "timestamp",
        "group_cols": ["channel_id"],
        "agg_cols": [
            ("COUNT(*)",        "sample_count"),
            ("SUM(rx_count)",   "total_rx_count"),
            ("SUM(tx_count)",   "total_tx_count"),
        ],
    },
    {
        "db": "repeater.db",
        "table": "crc_error_rate",
        "ts_col": "timestamp",
        "group_cols": ["channel_id"],
        "agg_cols": [
            ("COUNT(*)",                "sample_count"),
            ("SUM(crc_error_count)",    "total_crc_errors"),
            ("SUM(crc_disabled_count)", "total_crc_disabled"),
        ],
    },
    {
        "db": "repeater.db",
        "table": "origin_channel_stats",
        "ts_col": "timestamp",
        "group_cols": ["channel_id"],
        "agg_cols": [
            ("COUNT(*)",    "sample_count"),
            ("SUM(count)",  "total_count"),
        ],
    },
]

# AVG ignores NULL, so every average needs its own denominator. COUNT(*)
# alone cannot preserve nullable averages when partial buckets are merged.
for _cfg in DOWNSAMPLE_TABLES:
    _cfg["agg_cols"] += [
        (f"COUNT({expr[4:-1]})", f"{alias}_count")
        for expr, alias in _cfg["agg_cols"] if expr.startswith("AVG(")
    ]

DB_DIR = "/var/lib/openhop_repeater"


def _summary_table_name(base_table: str, suffix: str) -> str:
    """Generate summary table name: e.g., packet_metrics_1m"""
    return f"{base_table}_{suffix}"


def _create_summary_table(conn: sqlite3.Connection, cfg: Dict, suffix: str):
    """Create summaries and migrate legacy average denominators.

    Old summaries did not retain non-NULL counts. Their only available
    fallback is sample_count for a non-NULL average (necessarily approximate).
    New summaries always record the exact per-field count.
    """
    table_name = _summary_table_name(cfg["table"], suffix)
    group_cols = cfg["group_cols"]
    agg_cols = cfg["agg_cols"]

    cols = ["id INTEGER PRIMARY KEY AUTOINCREMENT",
            "bucket_ts REAL NOT NULL"]
    for gc in group_cols:
        cols.append(f"{gc} TEXT")
    for _, alias in agg_cols:
        cols.append(f"{alias} REAL")

    col_defs = ", ".join(cols)
    sql = f"CREATE TABLE IF NOT EXISTS {table_name} ({col_defs})"
    conn.execute(sql)
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table_name})")}
    for expr, alias in agg_cols:
        if expr.startswith("AVG("):
            count_alias = f"{alias}_count"
            if count_alias not in existing:
                conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {count_alias} REAL")
                conn.execute(
                    f"UPDATE {table_name} SET {count_alias} = "
                    f"CASE WHEN {alias} IS NULL THEN 0 ELSE sample_count END")

    # Create index on bucket_ts for fast range queries
    idx_name = f"idx_{table_name}_bucket_ts"
    conn.execute(f"CREATE INDEX IF NOT EXISTS {idx_name} ON {table_name}(bucket_ts)")

    # Create composite index for group+time queries
    if group_cols:
        idx_name2 = f"idx_{table_name}_grp_ts"
        grp_idx = ", ".join(group_cols + ["bucket_ts"])
        conn.execute(f"CREATE INDEX IF NOT EXISTS {idx_name2} ON {table_name}({grp_idx})")


def _atomic_rollup(func):
    """Keep each insert/delete pair atomic even if the caller catches errors."""
    @functools.wraps(func)
    def wrapped(conn, *args, **kwargs):
        conn.execute("SAVEPOINT metrics_rollup")
        try:
            result = func(conn, *args, **kwargs)
        except BaseException:
            conn.execute("ROLLBACK TO metrics_rollup")
            conn.execute("RELEASE metrics_rollup")
            raise
        conn.execute("RELEASE metrics_rollup")
        return result
    return wrapped


@_atomic_rollup
def _aggregate_from_source(conn: sqlite3.Connection, cfg: Dict,
                           from_ts: float, to_ts: float,
                           bucket_seconds: int, target_suffix: str) -> int:
    """Aggregate raw source data into a summary table and delete originals.

    Used for warm data (source → _1m) and old data after outages (→ _15m).
    Returns the number of source rows deleted.
    """
    source_table = cfg["table"]
    if source_table == "channel_stats_history":
        raise ValueError("Cumulative channel snapshots must remain raw until expiry")
    ts_col = cfg["ts_col"]
    group_cols = cfg["group_cols"]
    agg_cols = cfg["agg_cols"]
    target_table = _summary_table_name(source_table, target_suffix)

    # Check if there's data in this range in the source table
    count_row = conn.execute(
        f"SELECT COUNT(*) FROM {source_table} WHERE {ts_col} >= ? AND {ts_col} < ?",
        (from_ts, to_ts)
    ).fetchone()
    if not count_row or count_row[0] == 0:
        return 0

    # Existing summaries describe *other* consumed rows, never these rows.
    # Append partial buckets; queries and later tiers combine them additively.
    # Build the aggregation query from raw source data
    bucket_expr = f"CAST(({ts_col} / {bucket_seconds}) AS INTEGER) * {bucket_seconds}"
    select_cols = [f"{bucket_expr} AS bucket_ts"]
    for gc in group_cols:
        select_cols.append(gc)
    for expr, alias in agg_cols:
        select_cols.append(f"{expr} AS {alias}")

    group_by = ["bucket_ts"] + group_cols
    select_sql = f"""SELECT {', '.join(select_cols)}
                     FROM {source_table}
                     WHERE {ts_col} >= ? AND {ts_col} < ?
                     GROUP BY {', '.join(group_by)}"""

    # Insert aggregated data into summary table
    insert_cols = ["bucket_ts"] + group_cols + [alias for _, alias in agg_cols]
    placeholders = ", ".join(["?"] * len(insert_cols))
    insert_sql = f"INSERT INTO {target_table} ({', '.join(insert_cols)}) VALUES ({placeholders})"

    rows = conn.execute(select_sql, (from_ts, to_ts)).fetchall()
    if rows:
        conn.executemany(insert_sql, rows)

    # Delete original rows that have been aggregated
    cur = conn.execute(
        f"DELETE FROM {source_table} WHERE {ts_col} >= ? AND {ts_col} < ?",
        (from_ts, to_ts)
    )
    return cur.rowcount


@_atomic_rollup
def _aggregate_from_summary(conn: sqlite3.Connection, cfg: Dict,
                            from_ts: float, to_ts: float,
                            source_suffix: str, bucket_seconds: int,
                            target_suffix: str) -> int:
    """Re-aggregate from a finer summary table into a coarser one.

    Used for cascading: _1m → _15m. Non-divisible bucket widths cannot be
    re-aggregated without moving some observations into the wrong bucket.
    Reads from the source summary table, aggregates into the target summary
    table, and deletes the consumed source summary rows.
    Returns the number of source summary rows deleted.
    """
    base_table = cfg["table"]
    group_cols = cfg["group_cols"]
    agg_cols = cfg["agg_cols"]
    source_table = _summary_table_name(base_table, source_suffix)
    target_table = _summary_table_name(base_table, target_suffix)
    source_resolution = {"1m": BUCKET_1M, "10m": BUCKET_10M, "15m": BUCKET_15M}[source_suffix]
    if bucket_seconds % source_resolution:
        raise ValueError("Target bucket must be a multiple of the source resolution")

    # Check if there's data in this range in the source summary table
    count_row = conn.execute(
        f"SELECT COUNT(*) FROM {source_table} WHERE bucket_ts >= ? AND bucket_ts < ?",
        (from_ts, to_ts)
    ).fetchone()
    if not count_row or count_row[0] == 0:
        return 0

    # Build re-aggregation query from summary table.
    # Summary tables have: bucket_ts, group_cols, and agg columns.
    # For re-aggregation, we need to combine the summary values correctly:
    # - COUNT/SUM columns → SUM them
    # - AVG columns → weighted average using each field's non-NULL count
    # - MIN columns → MIN
    # - MAX columns → MAX
    bucket_expr = f"CAST((bucket_ts / {bucket_seconds}) AS INTEGER) * {bucket_seconds}"
    select_cols = [f"{bucket_expr} AS new_bucket_ts"]
    for gc in group_cols:
        select_cols.append(gc)

    # Identify averages from their expressions, not a *_count suffix:
    # avg_hop_count is an average; avg_hop_count_count is its denominator.
    reagg_exprs = []
    for raw_expr, alias in agg_cols:
        if raw_expr.startswith("AVG("):
            count_expr = (f"COALESCE({alias}_count, "
                          f"CASE WHEN {alias} IS NULL THEN 0 ELSE sample_count END)")
            reagg_exprs.append(
                (f"SUM({alias} * {count_expr}) / NULLIF(SUM({count_expr}), 0)", alias))
        elif alias.startswith("min_"):
            reagg_exprs.append((f"MIN({alias})", alias))
        elif alias.startswith("max_"):
            reagg_exprs.append((f"MAX({alias})", alias))
        else:
            # Counters add; distinct counts can only be approximated by SUM.
            reagg_exprs.append((f"SUM({alias})", alias))

    for expr, alias in reagg_exprs:
        select_cols.append(f"{expr} AS {alias}")

    group_by = ["new_bucket_ts"] + group_cols
    select_sql = f"""SELECT {', '.join(select_cols)}
                     FROM {source_table}
                     WHERE bucket_ts >= ? AND bucket_ts < ?
                     GROUP BY {', '.join(group_by)}"""

    # Insert into target
    insert_cols = ["bucket_ts"] + group_cols + [alias for _, alias in reagg_exprs]
    placeholders = ", ".join(["?"] * len(insert_cols))
    insert_sql = f"INSERT INTO {target_table} ({', '.join(insert_cols)}) VALUES ({placeholders})"

    rows = conn.execute(select_sql, (from_ts, to_ts)).fetchall()
    if rows:
        conn.executemany(insert_sql, rows)

    # Delete consumed source summary rows
    cur = conn.execute(
        f"DELETE FROM {source_table} WHERE bucket_ts >= ? AND bucket_ts < ?",
        (from_ts, to_ts)
    )
    return cur.rowcount


class MetricsRetention:
    def __init__(self,
                 retention_days: int = DEFAULT_RETENTION_DAYS,
                 cleanup_interval_s: int = DEFAULT_CLEANUP_INTERVAL_S,
                 vacuum_interval_s: int = DEFAULT_VACUUM_INTERVAL_S,
                 db_dir: str = DB_DIR):
        for name, value in (("retention_days", retention_days),
                            ("cleanup_interval_s", cleanup_interval_s),
                            ("vacuum_interval_s", vacuum_interval_s)):
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        self.retention_days = retention_days
        self.cleanup_interval_s = cleanup_interval_s
        self.vacuum_interval_s = vacuum_interval_s
        self.db_dir = db_dir
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lifecycle_lock = threading.Lock()
        # Persist last VACUUM timestamp so a service restart does not cause
        # an immediate VACUUM (which briefly uses 2-3x the DB size in RAM).
        self._vacuum_state_path = os.path.join(self.db_dir, ".last_vacuum")
        self._last_vacuum = self._load_last_vacuum()

    def _load_last_vacuum(self) -> float:
        """Load the last VACUUM timestamp from disk, or seed it to now.

        On first startup (no file yet) we seed with `time.time()` so the first
        VACUUM only runs after a full `vacuum_interval_s` has elapsed, instead
        of firing on every service restart.
        """
        try:
            with open(self._vacuum_state_path, "r") as fh:
                ts = float(fh.read().strip())
                if ts > 0 and ts <= time.time():
                    return ts
        except (OSError, ValueError):
            pass
        # No valid state found -> seed with now so first VACUUM is delayed.
        now = time.time()
        self._save_last_vacuum(now)
        return now

    def _save_last_vacuum(self, ts: float) -> None:
        """Persist the last VACUUM timestamp to disk."""
        try:
            os.makedirs(os.path.dirname(self._vacuum_state_path), exist_ok=True)
            with open(self._vacuum_state_path, "w") as fh:
                fh.write(str(ts))
        except OSError as exc:
            logger.debug("Could not persist vacuum timestamp: %s", exc)

    @property
    def retention_seconds(self) -> int:
        return self.retention_days * 86400

    def start(self):
        with self._lifecycle_lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._running = True
            self._thread = threading.Thread(target=self._run, daemon=True,
                                            name="MetricsRetention")
            try:
                self._thread.start()
            except BaseException:
                self._running = False
                self._thread = None
                raise
        logger.info("MetricsRetention started (retention=%dd, cleanup_every=%ds, "
                    "tiers=7h/24h/3d/%dd)",
                    self.retention_days, self.cleanup_interval_s,
                    self.retention_days)

    def stop(self):
        """Signal shutdown and join the worker before releasing its connections."""
        if self._thread is threading.current_thread():
            self._stop_event.set()
            raise RuntimeError("MetricsRetention cannot join its own worker")
        with self._lifecycle_lock:
            self._stop_event.set()
            if self._thread:
                # SQL execution/VACUUM is not bounded by busy_timeout. A timed
                # join would let shutdown close storage while this producer
                # still owns database work. The worker never takes this lock.
                self._thread.join()
            self._close_connections()
            self._running = False
            self._thread = None

    def _close_connections(self):
        with _shared_conn_lock:
            for path in list(_shared_conn_instances):
                if os.path.abspath(os.path.dirname(path)) == os.path.abspath(self.db_dir):
                    # Keep a failed close reachable for a later stop retry.
                    _shared_conn_instances[path].close()
                    del _shared_conn_instances[path]

    def _run(self):
        try:
            self._run_cycles()
        finally:
            try:
                self._close_connections()
            finally:
                self._running = False

    def _run_cycles(self):
        # First run after 60s so service start is clean
        if self._stop_event.wait(60):
            return
        while not self._stop_event.is_set():
            try:
                self.cleanup_once()
                if self._stop_event.is_set():
                    return
                self._wal_truncate()
                if self._stop_event.is_set():
                    return
                if time.time() - self._last_vacuum >= self.vacuum_interval_s:
                    self.vacuum_once()
                    self._last_vacuum = time.time()
                    self._save_last_vacuum(self._last_vacuum)
                # Return freed pages to the OS to keep RSS low on small devices
                malloc_trim()
            except Exception as e:
                logger.error("Retention cycle error: %s", e)
            if self._stop_event.wait(self.cleanup_interval_s):
                return

    def _ensure_summary_tables(self):
        """Create summary tables if they don't exist yet."""
        by_db: Dict[str, list] = {}
        for cfg in DOWNSAMPLE_TABLES:
            by_db.setdefault(cfg["db"], []).append(cfg)

        for db_name, configs in by_db.items():
            db_path = os.path.join(self.db_dir, db_name)
            if not os.path.exists(db_path):
                continue
            try:
                with _db_conn(db_path, timeout=10) as conn:
                    for cfg in configs:
                        for suffix in ["1m", "10m", "15m"]:
                            _create_summary_table(conn, cfg, suffix)
                    conn.commit()
            except Exception as e:
                logger.warning("MetricsRetention: summary table creation failed for %s: %s",
                               db_name, e)

    def cleanup_once(self):
        """Run one complete cleanup cycle: downsample + delete expired."""
        self._ensure_summary_tables()
        now = time.time()
        total_deleted = 0
        total_aggregated = 0

        # --- Phase 1: Tiered downsampling ---
        # Retain 1m until day three, then roll directly to 15m. Never move
        # legacy 10m buckets into 15m: the boundaries do not line up.
        warm_from = max(now - TIER_COOL_SECONDS, now - self.retention_seconds)
        warm_to = now - TIER_HOT_SECONDS

        # Cold tier: 3d-retention → 15 minute buckets
        cold_from = now - self.retention_seconds
        cold_to = now - TIER_COOL_SECONDS

        by_db: Dict[str, list] = {}
        for cfg in DOWNSAMPLE_TABLES:
            by_db.setdefault(cfg["db"], []).append(cfg)

        for db_name, configs in by_db.items():
            db_path = os.path.join(self.db_dir, db_name)
            if not os.path.exists(db_path):
                continue
            try:
                with _db_conn(db_path, timeout=30) as conn:
                    conn.execute("PRAGMA busy_timeout = 10000")
                    for cfg in configs:
                        table = cfg["table"]
                        ts_col = cfg["ts_col"]

                        # Check source table exists
                        exists = conn.execute(
                            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                            (table,)
                        ).fetchone()
                        if not exists:
                            continue

                        if table != "channel_stats_history":
                            # Raw fallbacks also cover late arrivals and outages.
                            for from_ts, to_ts, resolution, suffix in (
                                (warm_from, warm_to, BUCKET_1M, "1m"),
                                (cold_from, cold_to, BUCKET_15M, "15m"),
                            ):
                                try:
                                    deleted = _aggregate_from_source(
                                        conn, cfg, from_ts, to_ts, resolution, suffix)
                                    total_deleted += deleted
                                    total_aggregated += deleted
                                except Exception as e:
                                    logger.warning("Tier %s from raw %s failed: %s", suffix, table, e)
                            try:
                                deleted = _aggregate_from_summary(
                                    conn, cfg, cold_from, cold_to,
                                    "1m", BUCKET_15M, "15m")
                                total_deleted += deleted
                                total_aggregated += deleted
                            except Exception as e:
                                logger.warning("Tier cold from 1m %s failed: %s", table, e)

                        # Delete from source anything older than retention
                        try:
                            cutoff = now - self.retention_seconds
                            where = f"{ts_col} < ?"
                            params = [cutoff]
                            if table == "channel_stats_history":
                                # One older baseline per still-retained channel
                                # preserves its first in-window delta (+ at
                                # most six rows). Inactive old channels expire.
                                where += (
                                    f" AND rowid NOT IN (SELECT rowid FROM ("
                                    f"SELECT rowid, ROW_NUMBER() OVER (PARTITION BY channel_id "
                                    f"ORDER BY {ts_col} DESC, rowid DESC) AS newest "
                                    f"FROM {table} WHERE {ts_col} < ? AND channel_id IN "
                                    f"(SELECT channel_id FROM {table} WHERE {ts_col} >= ?)) "
                                    f"WHERE newest = 1)"
                                )
                                params.extend([cutoff, cutoff])
                            cur = conn.execute(
                                f"DELETE FROM {table} WHERE {where}", params,
                            )
                            if cur.rowcount > 0:
                                total_deleted += cur.rowcount
                                logger.info("MetricsRetention: %s.%s expired %d rows",
                                            db_name, table, cur.rowcount)
                        except Exception as e:
                            logger.warning("MetricsRetention: %s.%s expire failed: %s",
                                           db_name, table, e)

                    # Delete expired rows from summary tables too
                    for cfg in configs:
                        cutoff = now - self.retention_seconds
                        for suffix, resolution in (("1m", BUCKET_1M), ("10m", BUCKET_10M), ("15m", BUCKET_15M)):
                            summary_table = _summary_table_name(cfg["table"], suffix)
                            try:
                                cur = conn.execute(
                                    f"DELETE FROM {summary_table} WHERE bucket_ts + ? <= ?",
                                    (resolution, cutoff)
                                )
                                if cur.rowcount > 0:
                                    total_deleted += cur.rowcount
                                    logger.debug("MetricsRetention: %s expired %d rows",
                                                 summary_table, cur.rowcount)
                            except Exception as e:
                                pass  # table might not exist yet

                    conn.commit()
            except Exception as e:
                logger.warning("MetricsRetention: %s downsample failed: %s", db_name, e)

        # --- Phase 2: Delete-only tables (no downsampling) ---
        cutoff = now - self.retention_seconds
        by_db_del: Dict[str, list] = {}
        for db_name, table, ts_col in DELETE_ONLY_TABLES:
            by_db_del.setdefault(db_name, []).append((table, ts_col))

        for db_name, tables in by_db_del.items():
            db_path = os.path.join(self.db_dir, db_name)
            if not os.path.exists(db_path):
                continue
            try:
                with _db_conn(db_path, timeout=10) as conn:
                    for table, ts_col in tables:
                        try:
                            exists = conn.execute(
                                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                                (table,)
                            ).fetchone()
                            if not exists:
                                continue
                            cur = conn.execute(
                                f"DELETE FROM {table} WHERE {ts_col} < ?",
                                (cutoff,)
                            )
                            if cur.rowcount > 0:
                                total_deleted += cur.rowcount
                                logger.info("MetricsRetention: %s.%s deleted %d rows",
                                            db_name, table, cur.rowcount)
                        except Exception as e:
                            logger.warning("MetricsRetention: %s.%s cleanup failed: %s",
                                           db_name, table, e)
                    conn.commit()
            except Exception as e:
                logger.warning("MetricsRetention: %s open failed: %s", db_name, e)

        if total_aggregated > 0:
            logger.info("MetricsRetention cleanup complete: %d rows deleted "
                        "(%d aggregated into summary tables)",
                        total_deleted, total_aggregated)
        else:
            logger.info("MetricsRetention cleanup pass complete, %d rows deleted",
                        total_deleted)

    def _wal_truncate(self):
        """Perform WAL TRUNCATE checkpoint to keep WAL file compact."""
        db_names = set()
        for cfg in DOWNSAMPLE_TABLES:
            db_names.add(cfg["db"])
        for db_name, _, _ in DELETE_ONLY_TABLES:
            db_names.add(db_name)

        for db_name in db_names:
            db_path = os.path.join(self.db_dir, db_name)
            if not os.path.exists(db_path):
                continue
            try:
                with _db_conn(db_path, timeout=10) as conn:
                    result = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                    if result and result[1] > 0:
                        logger.debug("WAL truncate %s: pages=%d, checkpointed=%d",
                                     db_name, result[1], result[2])
            except Exception as e:
                logger.debug("WAL truncate %s failed: %s", db_name, e)

    def vacuum_once(self):
        """Run VACUUM on all databases to reclaim disk space."""
        db_names = set()
        for cfg in DOWNSAMPLE_TABLES:
            db_names.add(cfg["db"])
        for db_name, _, _ in DELETE_ONLY_TABLES:
            db_names.add(db_name)

        for db_name in db_names:
            db_path = os.path.join(self.db_dir, db_name)
            if not os.path.exists(db_path):
                continue
            try:
                with _db_conn(db_path, timeout=30) as conn:
                    conn.execute("VACUUM")
                logger.info("MetricsRetention: VACUUM %s complete", db_name)
            except Exception as e:
                logger.warning("MetricsRetention: VACUUM %s failed: %s", db_name, e)


_singleton: Optional[MetricsRetention] = None
_singleton_lock = threading.Lock()


def get_retention(config=None) -> MetricsRetention:
    global _singleton
    with _singleton_lock:
        if _singleton is None or (config is not None and not (
                _singleton._thread and _singleton._thread.is_alive())):
            storage = (config or {}).get("storage") or {}
            retention = storage.get("retention") or {}
            _singleton = MetricsRetention(
                retention_days=float(retention.get("metrics_days", DEFAULT_RETENTION_DAYS)),
                db_dir=storage.get("storage_dir") or DB_DIR,
            )
        return _singleton


def start(config=None):
    retention = get_retention(config)
    retention.start()
    return retention
