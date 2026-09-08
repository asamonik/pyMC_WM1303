"""Tiered query utility for pyMC_Repeater (WM1303).

Provides seamless querying across the multi-tier metrics storage system.
When the metrics retention system aggregates raw data into summary tables,
this module reads all storage tiers in one consistent snapshot and combines
partial buckets into a unified result set. Cleanup delays do not hide data.

Tier layout:
  Tier   | Age           | Table suffix | Resolution
  -------|---------------|--------------|------------
  Hot    | 0 - 7 h       | (raw)        | Full
  Warm   | 7 h - 3 d     | _1m          | 1 minute
  Cold   | 3 d - 8 d     | _15m         | 15 minutes

Legacy _10m buckets remain readable until expiry. The output width is rounded
up to a multiple of all native resolutions present, and is returned in each
row as bucket_seconds. A summary overlapping either window boundary is
included whole: exact partial-window counts cannot be recovered. Legacy
averages without per-field counts and distinct counts are also approximate.
Cumulative channel snapshots remain raw; their deltas are assigned to the
later sample, including resets, rather than dropped at bucket boundaries.

Usage:
    from repeater.web.tiered_query import tiered_channel_query

    with _db_conn(db_path) as conn:
        rows = tiered_channel_query(
            conn,
            table_name="packet_activity",
            channel_id="channel_a",
            since_ts=time.time() - 86400,
            until_ts=time.time(),
            bucket_seconds=900,
        )
        # rows = [{"bucket_ts": 1234567800, "total_rx_count": 5, ...}, ...]
"""
import logging
import functools
import math
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("tiered_query")

# ---------------------------------------------------------------------------
# Summary table resolutions (seconds)
# ---------------------------------------------------------------------------
RES_1M = 60
RES_10M = 600
RES_15M = 900

# ---------------------------------------------------------------------------
# Table registry — maps base table names to aggregation metadata.
# This mirrors DOWNSAMPLE_TABLES from metrics_retention.py but is kept
# self-contained so the query module has no import dependency on the
# retention thread.
# ---------------------------------------------------------------------------
# Each entry: {
#   "ts_col":      timestamp column in the raw table,
#   "group_cols":  grouping columns (present in both raw and summary),
#   "agg_cols":    [(raw_expr, summary_alias), ...]
# }
# The summary_alias is the column name in _1m/_10m/_15m tables.
# The raw_expr is the SQL aggregation applied to the raw table.
_TABLE_REGISTRY: Dict[str, Dict] = {
    "packet_activity": {
        "ts_col": "timestamp",
        "group_cols": ["channel_id"],
        "agg_cols": [
            ("COUNT(*)",        "sample_count"),
            ("SUM(rx_count)",   "total_rx_count"),
            ("SUM(tx_count)",   "total_tx_count"),
        ],
    },
    "noise_floor_history": {
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
    "channel_stats_history": {
        "ts_col": "timestamp",
        "group_cols": ["channel_id"],
        "agg_cols": [
            ("COUNT(*)",                                  "sample_count"),
            # Deltas are computed before time filtering and bucketing, so
            # a prior sample and transitions across buckets are preserved.
            ("SUM(delta_rx_count)",                       "total_rx_count"),
            ("AVG(avg_rssi)",                             "avg_rssi"),
            ("AVG(avg_snr)",                              "avg_snr"),
            ("SUM(delta_tx_count)",                       "total_tx_count"),
            ("SUM(delta_tx_failed)",                      "total_tx_failed"),
            ("SUM(delta_tx_airtime_ms)",                  "total_tx_airtime_ms"),
            ("SUM(delta_tx_bytes)",                       "total_tx_bytes"),
            ("SUM(delta_lbt_blocked)",                    "total_lbt_blocked"),
            ("SUM(delta_lbt_passed)",                     "total_lbt_passed"),
            ("AVG(noise_floor_dbm)",                      "avg_noise_floor_dbm"),
            ("AVG(tx_noisefloor_dbm)",                    "avg_tx_noisefloor_dbm"),
        ],
    },
    "dedup_events": {
        "ts_col": "ts",
        "group_cols": ["event_type", "source"],
        "agg_cols": [
            ("COUNT(*)",                 "sample_count"),
            ("COUNT(DISTINCT pkt_hash)", "unique_packets"),
            ("SUM(pkt_size)",            "total_bytes"),
        ],
    },
    "cad_events": {
        "ts_col": "timestamp",
        "group_cols": ["channel_id"],
        "agg_cols": [
            ("COUNT(*)",              "sample_count"),
            ("SUM(cad_clear)",        "total_cad_clear"),
            ("SUM(cad_detected)",     "total_cad_detected"),
            ("SUM(cad_skipped)",      "total_cad_skipped"),
            ("SUM(cad_hw_clear)",     "total_cad_hw_clear"),
            ("SUM(cad_hw_detected)",  "total_cad_hw_detected"),
            ("SUM(cad_sw_clear)",     "total_cad_sw_clear"),
            ("SUM(cad_sw_detected)",  "total_cad_sw_detected"),
        ],
    },
    "crc_error_rate": {
        "ts_col": "timestamp",
        "group_cols": ["channel_id"],
        "agg_cols": [
            ("COUNT(*)",                "sample_count"),
            ("SUM(crc_error_count)",    "total_crc_errors"),
            ("SUM(crc_disabled_count)", "total_crc_disabled"),
        ],
    },
    "packet_metrics": {
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
    "origin_channel_stats": {
        "ts_col": "timestamp",
        "group_cols": ["channel_id"],
        "agg_cols": [
            ("COUNT(*)",    "sample_count"),
            ("SUM(count)",  "total_count"),
        ],
    },
}

# Must match the storage schema. These denominators are internal unless
# explicitly requested; sample_count is not a valid weight for nullable data.
for _cfg in _TABLE_REGISTRY.values():
    _cfg["agg_cols"] += [
        (f"COUNT({expr[4:-1]})", f"{alias}_count")
        for expr, alias in _cfg["agg_cols"] if expr.startswith("AVG(")
    ]
_AVERAGE_ALIASES = {
    alias for cfg in _TABLE_REGISTRY.values()
    for expr, alias in cfg["agg_cols"] if expr.startswith("AVG(")
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _table_exists(conn, table_name: str) -> bool:
    """Check if a table exists in the connected database."""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,)
    ).fetchone()
    return row is not None


def _summary_table_name(base_table: str, suffix: str) -> str:
    """Generate summary table name, e.g. packet_activity_1m."""
    return f"{base_table}_{suffix}"


def _average_count_expr(alias: str, available_columns) -> str:
    fallback = f"CASE WHEN {alias} IS NULL THEN 0 ELSE sample_count END"
    count_alias = f"{alias}_count"
    if available_columns is None or count_alias in available_columns:
        return f"COALESCE({count_alias}, {fallback})"
    return fallback


def _reagg_expr(alias: str, available_columns=None) -> str:
    """Return the SQL re-aggregation expression for a summary column.

    Uses the naming convention established in metrics_retention.py:
      - sample_count, total_*, *_count  → SUM
      - avg_*                           → weighted average via non-NULL count
      - min_*                           → MIN
      - max_*                           → MAX
      - unique_packets                  → SUM (approximation)
    """
    if alias.endswith("_count") and alias[:-6] in _AVERAGE_ALIASES:
        return f"SUM({_average_count_expr(alias[:-6], available_columns)})"
    if alias in _AVERAGE_ALIASES:
        count_expr = _average_count_expr(alias, available_columns)
        return f"SUM({alias} * {count_expr}) / NULLIF(SUM({count_expr}), 0)"
    if alias.startswith("min_"):
        return f"MIN({alias})"
    if alias.startswith("max_"):
        return f"MAX({alias})"
    # Counters add; distinct counts can only be approximated by SUM.
    return f"SUM({alias})"


def _build_raw_query(
    table_name: str,
    ts_col: str,
    bucket_seconds: int,
    agg_cols: List[Tuple[str, str]],
    group_cols: List[str],
    filter_col: Optional[str],
    filter_val,
    seg_start: float,
    seg_end: float,
    extra_group_cols: Optional[List[str]] = None,
) -> Tuple[str, list]:
    """Build a bucketed aggregation query against the raw source table.

    Returns (sql, params).
    """
    bucket_expr = f"CAST(({ts_col} / {bucket_seconds}) AS INTEGER) * {bucket_seconds}"
    select_parts = [f"{bucket_expr} AS bucket_ts"]

    all_group_cols = list(group_cols)
    if extra_group_cols:
        for gc in extra_group_cols:
            if gc not in all_group_cols:
                all_group_cols.append(gc)

    for gc in all_group_cols:
        select_parts.append(gc)
    for raw_expr, alias in agg_cols:
        select_parts.append(f"{raw_expr} AS {alias}")

    where_parts = [f"{ts_col} >= ?", f"{ts_col} < ?"]
    params = [seg_start, seg_end]

    if filter_col and filter_val is not None:
        where_parts.append(f"{filter_col} = ?")
        params.append(filter_val)

    group_by = ["bucket_ts"] + all_group_cols

    source = table_name
    prefix = ""
    if table_name == "channel_stats_history":
        counters = [expr[10:-1] for expr, _ in agg_cols if expr.startswith("SUM(delta_")]
        if counters:
            previous = [f"LAG({col}) OVER counter_order AS previous_{col}" for col in counters]
            deltas = [
                f"CASE WHEN previous_{col} IS NULL OR {col} IS NULL THEN NULL "
                f"WHEN {col} >= previous_{col} THEN {col} - previous_{col} "
                f"ELSE {col} END AS delta_{col}"
                for col in counters
            ]
            # Read the prior snapshot before the requested window too.
            # The earliest retained sample has an unknown delta, not zero
            # or its entire lifetime count. A falling counter marks a reset.
            prefix = (
                f"WITH counter_samples AS (SELECT *, {', '.join(previous)} "
                f"FROM {table_name} WHERE {ts_col} < ? "
                f"WINDOW counter_order AS (PARTITION BY channel_id ORDER BY {ts_col}, rowid)), "
                f"counter_deltas AS (SELECT *, {', '.join(deltas)} FROM counter_samples) "
            )
            params.insert(0, seg_end)
            source = "counter_deltas"
    sql = prefix + (
        f"SELECT {', '.join(select_parts)} "
        f"FROM {source} "
        f"WHERE {' AND '.join(where_parts)} "
        f"GROUP BY {', '.join(group_by)} "
        f"ORDER BY bucket_ts ASC"
    )
    return sql, params


def _build_summary_query(
    summary_table: str,
    bucket_seconds: int,
    summary_resolution: int,
    agg_cols: List[Tuple[str, str]],
    group_cols: List[str],
    filter_col: Optional[str],
    filter_val,
    seg_start: float,
    seg_end: float,
    extra_group_cols: Optional[List[str]] = None,
    available_columns=None,
) -> Tuple[str, list]:
    """Build a query against a summary table, re-bucketing if needed.

    Combine partial summary buckets, re-bucketing when a coarser interval is requested.
    The caller chooses a width divisible by all participating resolutions.
    Boundary-overlapping buckets are included whole, necessarily approximate.

    Returns (sql, params).
    """
    all_group_cols = list(group_cols)
    if extra_group_cols:
        for gc in extra_group_cols:
            if gc not in all_group_cols:
                all_group_cols.append(gc)

    where_parts = [f"bucket_ts + {summary_resolution} > ?", "bucket_ts < ?"]
    params = [seg_start, seg_end]

    if filter_col and filter_val is not None:
        where_parts.append(f"{filter_col} = ?")
        params.append(filter_val)

    # Partial buckets and overridden groupings also require aggregation.
    bucket_expr = f"CAST((bucket_ts / {bucket_seconds}) AS INTEGER) * {bucket_seconds}"
    select_parts = [f"{bucket_expr} AS rebucket_ts"] + all_group_cols
    select_parts.extend(f"{_reagg_expr(alias, available_columns)} AS {alias}" for _, alias in agg_cols)
    group_by = ["rebucket_ts"] + all_group_cols
    sql = (
        f"SELECT {', '.join(select_parts)} FROM {summary_table} "
        f"WHERE {' AND '.join(where_parts)} "
        f"GROUP BY {', '.join(group_by)} ORDER BY rebucket_ts ASC"
    )

    return sql, params


def _rows_to_dicts(
    cursor_rows,
    agg_cols: List[Tuple[str, str]],
    group_cols: List[str],
    extra_group_cols: Optional[List[str]] = None,
) -> List[Dict]:
    """Convert raw cursor rows into a list of dicts.

    Expected column order: bucket_ts, *group_cols, *extra_group_cols, *agg_alias
    """
    all_group_cols = list(group_cols)
    if extra_group_cols:
        for gc in extra_group_cols:
            if gc not in all_group_cols:
                all_group_cols.append(gc)

    col_names = ["bucket_ts"] + all_group_cols + [alias for _, alias in agg_cols]
    results = []
    for row in cursor_rows:
        d = {}
        for i, name in enumerate(col_names):
            d[name] = row[i] if i < len(row) else None
        results.append(d)
    return results


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _read_snapshot(func):
    """See one consistent database snapshot while retention moves rows."""
    @functools.wraps(func)
    def wrapped(conn, *args, **kwargs):
        conn.execute("SAVEPOINT metrics_query")
        try:
            return func(conn, *args, **kwargs)
        finally:
            conn.execute("RELEASE metrics_query")
    return wrapped


@_read_snapshot
def tiered_channel_query(
    conn,
    table_name: str,
    channel_id: Optional[str],
    since_ts: float,
    until_ts: float,
    bucket_seconds: int,
    columns: Optional[List[str]] = None,
    group_cols: Optional[List[str]] = None,
    extra_filters: Optional[Dict[str, str]] = None,
) -> List[Dict]:
    """Query data across all tiers for a given timeframe.

    Transparently reads from raw and summary tables as needed, then returns
    a single merged result set sorted by ``bucket_ts`` ascending.

    Parameters
    ----------
    conn : sqlite3.Connection
        An open database connection (use ``_db_conn`` context manager).
    table_name : str
        Base table name, e.g. ``"packet_activity"``.
        Must be registered in the internal table registry.
    channel_id : str or None
        Channel filter value.  Pass ``None`` to skip channel filtering
        (useful for tables like ``dedup_events`` that group differently).
    since_ts : float
        Start of the query window (Unix timestamp, inclusive).
    until_ts : float
        End of the query window (Unix timestamp, exclusive).
    bucket_seconds : int
        Desired bucket width in seconds, rounded up to a common multiple of
        native bucket widths actually present in this window.
    columns : list of str, optional
        Subset of summary column aliases to return.  ``None`` returns all
        columns defined in the table registry.
    group_cols : list of str, optional
        Override the default group columns from the registry.  Useful for
        queries that need additional or fewer grouping dimensions.
    extra_filters : dict, optional
        Additional column=value filters applied to all tier queries.
        Example: ``{"direction": "rx"}`` for packet_metrics.

    Returns
    -------
    list of dict
        Each dict contains ``"bucket_ts"``, effective ``"bucket_seconds"``
        and the requested aggregation columns, sorted by ``bucket_ts``.
        Summary buckets overlapping the window are included whole. Such
        boundary results, old averages and historical distinct counts are
        approximate; already-lost legacy cumulative deltas are unrecoverable.
        If the table is not registered or does not exist, returns ``[]``.
    """
    cfg = _TABLE_REGISTRY.get(table_name)
    if cfg is None:
        logger.warning("tiered_query: unknown table %r", table_name)
        return []

    ts_col = cfg["ts_col"]
    default_group_cols = cfg["group_cols"]
    agg_cols = cfg["agg_cols"]

    if bucket_seconds <= 0 or int(bucket_seconds) != bucket_seconds:
        raise ValueError("bucket_seconds must be a positive integer")
    bucket_seconds = int(bucket_seconds)
    if since_ts >= until_ts:
        return []

    # SQL identifiers must come from this table's registry, not caller input.
    effective_group_cols = list(group_cols if group_cols is not None else default_group_cols)
    filters = dict(extra_filters or {})
    if channel_id is not None and "channel_id" in default_group_cols:
        filters["channel_id"] = channel_id
    if not set(effective_group_cols).issubset(default_group_cols) or not set(filters).issubset(default_group_cols):
        raise ValueError("Unknown metric grouping or filter column")
    average_counts = {alias: f"{alias}_count" for expr, alias in agg_cols if expr.startswith("AVG(")}
    internal_counts = set(average_counts.values())
    requested = set(columns) if columns is not None else {
        alias for _, alias in agg_cols if alias not in internal_counts}
    needed = requested | {"sample_count"} | {
        count for alias, count in average_counts.items() if alias in requested}
    filtered_agg_cols = [(expr, alias) for expr, alias in agg_cols
                         if alias in needed]

    sources = []
    native_multiple = 1
    for suffix, resolution in ((None, 1), ("1m", RES_1M), ("10m", RES_10M), ("15m", RES_15M)):
        source = _summary_table_name(table_name, suffix) if suffix else table_name
        if not _table_exists(conn, source):
            continue
        source_ts = "bucket_ts" if suffix else ts_col
        start_expr = f"bucket_ts + {resolution} > ?" if suffix else f"{ts_col} >= ?"
        where = [start_expr, f"{source_ts} < ?"] + [f"{col} IS ?" for col in filters]
        if conn.execute(f"SELECT 1 FROM {source} WHERE {' AND '.join(where)} LIMIT 1",
                        [since_ts, until_ts, *filters.values()]).fetchone():
            sources.append((source, suffix, resolution))
            native_multiple = math.lcm(native_multiple, resolution)
    bucket_seconds = ((bucket_seconds + native_multiple - 1) // native_multiple) * native_multiple

    # A row lives in exactly one table: retention moves it transactionally.
    # Query every storage tier for the whole window. Age-based selection loses
    # data when cleanup is delayed, and deduplicating buckets drops partial
    # buckets split across tiers (or written by later cleanup passes).
    merged = {}
    avg_weights = {}
    for source, suffix, resolution in sources:
        if suffix:
            available = {row[1] for row in conn.execute(f"PRAGMA table_info({source})")}
            sql, params = _build_summary_query(
                source, bucket_seconds, resolution, filtered_agg_cols,
                effective_group_cols, None, None, since_ts, until_ts,
                available_columns=available)
        else:
            sql, params = _build_raw_query(
                source, ts_col, bucket_seconds, filtered_agg_cols,
                effective_group_cols, None, None, since_ts, until_ts)
        if filters:
            where_idx = sql.index("GROUP BY")
            clause = " AND ".join(f"{col} IS ?" for col in filters)
            sql = sql[:where_idx] + f"AND {clause} " + sql[where_idx:]
            params.extend(filters.values())
        try:
            rows = _rows_to_dicts(conn.execute(sql, params).fetchall(),
                                 filtered_agg_cols, effective_group_cols)
        except Exception as exc:
            logger.warning("tiered_query: %s query failed: %s", source, exc)
            continue
        for row in rows:
            key = (row["bucket_ts"], *(row[col] for col in effective_group_cols))
            result = merged.setdefault(key, {
                "bucket_ts": row["bucket_ts"],
                "bucket_seconds": bucket_seconds,
                **{col: row[col] for col in effective_group_cols},
                **{alias: None for _, alias in filtered_agg_cols},
            })
            for _, alias in filtered_agg_cols:
                value = row[alias]
                if value is None:
                    continue
                previous = result[alias]
                if alias in average_counts:
                    weight_key = (key, alias)
                    old_weight = avg_weights.get(weight_key, 0)
                    weight = row.get(average_counts[alias]) or 0
                    total_weight = old_weight + weight
                    if total_weight:
                        result[alias] = ((previous or 0) * old_weight + value * weight) / total_weight
                    avg_weights[weight_key] = total_weight
                elif alias.startswith("min_"):
                    result[alias] = min(previous, value) if previous is not None else value
                elif alias.startswith("max_"):
                    result[alias] = max(previous, value) if previous is not None else value
                else:
                    result[alias] = (previous or 0) + value

    all_rows = sorted(merged.values(), key=lambda row: row["bucket_ts"])
    for row in all_rows:
        for alias in needed - requested:
            row.pop(alias, None)
    return all_rows

# ---------------------------------------------------------------------------
# Specialized helpers for common query patterns
# ---------------------------------------------------------------------------

def tiered_packet_activity_query(
    conn,
    channel_id: str,
    since_ts: float,
    until_ts: float,
    bucket_seconds: int,
) -> List[Dict]:
    """Tiered query for packet_activity (RX/TX counts per channel).

    Returns rows with: bucket_ts, total_rx_count, total_tx_count
    """
    return tiered_channel_query(
        conn,
        table_name="packet_activity",
        channel_id=channel_id,
        since_ts=since_ts,
        until_ts=until_ts,
        bucket_seconds=bucket_seconds,
        columns=["total_rx_count", "total_tx_count"],
    )


def tiered_noise_floor_query(
    conn,
    channel_id: str,
    since_ts: float,
    until_ts: float,
    bucket_seconds: int,
) -> List[Dict]:
    """Tiered query for noise_floor_history (per-channel noise floor).

    Returns rows with: bucket_ts, avg_noise_floor_dbm, min_noise_floor_dbm,
    max_noise_floor_dbm, min_rssi, max_rssi
    """
    return tiered_channel_query(
        conn,
        table_name="noise_floor_history",
        channel_id=channel_id,
        since_ts=since_ts,
        until_ts=until_ts,
        bucket_seconds=bucket_seconds,
        columns=[
            "avg_noise_floor_dbm", "min_noise_floor_dbm",
            "max_noise_floor_dbm", "min_rssi", "max_rssi",
            "total_samples_collected", "total_samples_accepted",
        ],
    )


def tiered_channel_stats_query(
    conn,
    channel_id: str,
    since_ts: float,
    until_ts: float,
    bucket_seconds: int,
    columns: Optional[List[str]] = None,
) -> List[Dict]:
    """Tiered query for channel_stats_history.

    Returns all aggregated channel stats columns unless a subset is specified.
    """
    return tiered_channel_query(
        conn,
        table_name="channel_stats_history",
        channel_id=channel_id,
        since_ts=since_ts,
        until_ts=until_ts,
        bucket_seconds=bucket_seconds,
        columns=columns,
    )


def tiered_dedup_query(
    conn,
    since_ts: float,
    until_ts: float,
    bucket_seconds: int,
) -> List[Dict]:
    """Tiered query for dedup_events (groups by event_type and source).

    Returns rows with: bucket_ts, event_type, source, sample_count,
    unique_packets, total_bytes

    Note: Unlike channel-based tables, dedup_events does not filter by
    channel_id.  The group columns are ``event_type`` and ``source``.
    """
    return tiered_channel_query(
        conn,
        table_name="dedup_events",
        channel_id=None,
        since_ts=since_ts,
        until_ts=until_ts,
        bucket_seconds=bucket_seconds,
    )


def tiered_cad_events_query(
    conn,
    channel_id: str,
    since_ts: float,
    until_ts: float,
    bucket_seconds: int,
) -> List[Dict]:
    """Tiered query for cad_events (CAD clear/detected/skipped per channel).

    Returns rows with: bucket_ts, total_cad_clear, total_cad_detected,
    total_cad_skipped, total_cad_hw_clear, total_cad_hw_detected,
    total_cad_sw_clear, total_cad_sw_detected
    """
    return tiered_channel_query(
        conn,
        table_name="cad_events",
        channel_id=channel_id,
        since_ts=since_ts,
        until_ts=until_ts,
        bucket_seconds=bucket_seconds,
        columns=[
            "total_cad_clear", "total_cad_detected", "total_cad_skipped",
            "total_cad_hw_clear", "total_cad_hw_detected",
            "total_cad_sw_clear", "total_cad_sw_detected",
        ],
    )


def tiered_crc_error_rate_query(
    conn,
    channel_id: str,
    since_ts: float,
    until_ts: float,
    bucket_seconds: int,
) -> List[Dict]:
    """Tiered query for crc_error_rate (per-channel CRC errors).

    Returns rows with: bucket_ts, total_crc_errors, total_crc_disabled
    """
    return tiered_channel_query(
        conn,
        table_name="crc_error_rate",
        channel_id=channel_id,
        since_ts=since_ts,
        until_ts=until_ts,
        bucket_seconds=bucket_seconds,
        columns=["total_crc_errors", "total_crc_disabled"],
    )


def tiered_packet_metrics_query(
    conn,
    channel_id: str,
    since_ts: float,
    until_ts: float,
    bucket_seconds: int,
    direction: Optional[str] = None,
) -> List[Dict]:
    """Tiered query for packet_metrics (RSSI/SNR/airtime per channel).

    Parameters
    ----------
    direction : str, optional
        Filter by direction (``"rx"`` or ``"tx"``).  ``None`` includes both.

    Returns rows with: bucket_ts, direction, avg_rssi, min_rssi, max_rssi,
    avg_snr, min_snr, max_snr, avg_airtime_ms, total_airtime_ms,
    total_bytes, avg_hop_count, crc_error_count
    """
    extra_filters = {}
    if direction is not None:
        extra_filters["direction"] = direction

    return tiered_channel_query(
        conn,
        table_name="packet_metrics",
        channel_id=channel_id,
        since_ts=since_ts,
        until_ts=until_ts,
        bucket_seconds=bucket_seconds,
        extra_filters=extra_filters if extra_filters else None,
    )


# ---------------------------------------------------------------------------
# Convenience: auto-select bucket size based on timeframe
# ---------------------------------------------------------------------------

def auto_bucket_seconds(hours: int) -> int:
    """Choose an appropriate bucket size for a given timeframe.

    Matches the bucket logic used by the existing API endpoints:
      <=1h  → 60s   (1 min)
      <=6h  → 300s  (5 min)
      <=24h → 900s  (15 min)
      <=72h → 3600s (1 hour)
      >72h  → 14400s (4 hours)
    """
    if hours <= 1:
        return 60
    if hours <= 6:
        return 300
    if hours <= 24:
        return 900
    if hours <= 72:
        return 3600
    return 14400
