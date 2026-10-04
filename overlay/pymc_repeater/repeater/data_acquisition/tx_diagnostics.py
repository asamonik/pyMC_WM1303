"""WM1303 TX outcomes; missing measurements never imply a clear channel."""

import math


SCHEMA = """
    CREATE TABLE IF NOT EXISTS tx_diagnostics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp REAL NOT NULL,
        channel_id TEXT NOT NULL,
        packet_type INTEGER,
        pkt_hash TEXT,
        ok BOOLEAN,
        tx_result TEXT,
        error TEXT,
        ack_received BOOLEAN NOT NULL,
        cad_enabled BOOLEAN,
        cad_detected BOOLEAN,
        cad_retries INTEGER,
        cad_reason TEXT,
        lbt_enabled BOOLEAN,
        lbt_pass BOOLEAN,
        lbt_retries INTEGER,
        lbt_rssi_dbm REAL,
        lbt_threshold_dbm REAL,
        tx_noisefloor_dbm REAL,
        rssi REAL,
        snr REAL,
        scheduler_attempt INTEGER
    )
"""

FIELDS = (
    "timestamp", "channel_id", "packet_type", "pkt_hash", "ok", "tx_result", "error",
    "ack_received", "cad_enabled", "cad_detected", "cad_retries", "cad_reason",
    "lbt_enabled", "lbt_pass", "lbt_retries", "lbt_rssi_dbm", "lbt_threshold_dbm",
    "tx_noisefloor_dbm", "rssi", "snr", "scheduler_attempt",
)


def record_values(record):
    """Validate the internal writer contract while preserving unknown values."""
    values = dict(record)
    timestamp = values.get("timestamp")
    if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp):
        raise ValueError("TX diagnostic timestamp must be finite")
    channel_id = values.get("channel_id")
    if not isinstance(channel_id, str) or not channel_id:
        raise ValueError("TX diagnostic channel_id is required")
    for key in ("ok", "ack_received", "cad_enabled", "cad_detected", "lbt_enabled", "lbt_pass"):
        value = values.get(key)
        if value is not None and type(value) is not bool:
            raise ValueError(f"TX diagnostic {key} must be boolean or unknown")
    if type(values.get("ack_received")) is not bool:
        raise ValueError("TX diagnostic ack_received is required")
    for key in ("cad_retries", "lbt_retries", "scheduler_attempt"):
        value = values.get(key)
        if value is not None and (type(value) is not int or value < (1 if key == "scheduler_attempt" else 0)):
            raise ValueError(f"TX diagnostic {key} must be a nonnegative integer or unknown")
    packet_type = values.get("packet_type")
    if packet_type is not None and (type(packet_type) is not int or not 0 <= packet_type <= 15):
        raise ValueError("TX diagnostic packet_type must fit a MeshCore type")
    for key in ("lbt_rssi_dbm", "lbt_threshold_dbm", "tx_noisefloor_dbm", "rssi", "snr"):
        value = values.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                  or not math.isfinite(value)):
            raise ValueError(f"TX diagnostic {key} must be finite or unknown")
    # The HAL uses -128 as an unavailable RSSI sentinel.
    for key in ("lbt_rssi_dbm", "tx_noisefloor_dbm", "rssi"):
        if values.get(key) == -128:
            values[key] = None
    for key in ("pkt_hash", "tx_result", "error", "cad_reason"):
        value = values.get(key)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"TX diagnostic {key} must be text or unknown")
    return tuple(values.get(key) for key in FIELDS)


_LABELS = {
    0: "Request (REQ)", 1: "Response (RESPONSE)", 2: "Plain Text Message (TXT_MSG)",
    3: "Acknowledgment (ACK)", 4: "Node Advertisement (ADVERT)",
    5: "Group Text Message (GRP_TXT)", 6: "Group Datagram (GRP_DATA)",
    7: "Anonymous Request (ANON_REQ)", 8: "Returned Path (PATH)", 9: "Trace (TRACE)",
    10: "Multi-part Packet (MULTIPART)", 11: "Control (CONTROL)", 15: "Custom Packet (RAW_CUSTOM)",
}


def _label(packet_type):
    return _LABELS.get(packet_type, f"Reserved Type {packet_type}" if 0 <= packet_type <= 15 else "Unknown packet type")


def _new_counts():
    return dict.fromkeys((
        "recorded_transmissions", "transmissions", "total_attempts", "attempts_1", "attempts_2",
        "attempts_3", "attempts_4_plus", "retry_packets", "first_attempt_success",
        "failed_transmissions", "busy_channel_events", "severe_contention_count", "max_attempts",
        "unknown_transmissions", "disabled_transmissions", "acknowledged_transmissions",
        "confirmed_tx_failures", "confirmed_tx_outcomes", "scheduler_retried_transmissions",
    ), 0)


def _percentile(distribution, quantile):
    count = sum(distribution.values())
    if not count:
        return None
    target, cumulative = max(1, math.ceil(count * quantile)), 0
    for attempts, frequency in sorted(distribution.items()):
        cumulative += frequency
        if cumulative >= target:
            return float(attempts)


def _finish(counts, distribution):
    result = dict(counts)
    total = counts["transmissions"]
    result["attempts_3_plus"] = counts["attempts_3"] + counts["attempts_4_plus"]
    for name, numerator in (
        ("retry_rate_pct", counts["retry_packets"]),
        ("first_attempt_success_rate_pct", counts["first_attempt_success"]),
        ("attempts_3_plus_pct", result["attempts_3_plus"]),
        ("attempts_4_plus_pct", counts["attempts_4_plus"]),
        ("severe_contention_pct", counts["severe_contention_count"]),
    ):
        result[name] = numerator * 100.0 / total if total else None
    result["avg_attempts"] = counts["total_attempts"] / total if total else None
    result["median_attempts"] = _percentile(distribution, 0.5)
    result["p95_attempts"] = _percentile(distribution, 0.95)
    result["max_attempts"] = counts["max_attempts"] if total else None
    return result


def _correlation(pairs):
    count = len(pairs)
    coefficient = None
    if count >= 3:
        mean_x = sum(x for x, _ in pairs) / count
        mean_y = sum(y for _, y in pairs) / count
        variance_x = sum((x - mean_x) ** 2 for x, _ in pairs)
        variance_y = sum((y - mean_y) ** 2 for _, y in pairs)
        if variance_x > 0 and variance_y > 0:
            coefficient = sum((x - mean_x) * (y - mean_y) for x, y in pairs) / math.sqrt(variance_x * variance_y)
            coefficient = max(-1.0, min(1.0, coefficient))
    return {"coefficient": coefficient, "sample_count": count}


def aggregate(conn, start_timestamp, end_timestamp, bucket_seconds=300, severe_attempt_threshold=4):
    """SQL aggregates bound memory by bucket/type/attempt, rather than raw rows.

    Each row describes one backend transmission operation. Its attempt count
    includes measured CAD/LBT retries; scheduler retries are tracked separately.
    A missing ACK, scan error, unknown enable state or absent retry count is
    excluded from the retry denominator. Disabled checks do not imply success.
    """
    rows = conn.execute("""
        WITH normalized AS (
            SELECT *, CAST(timestamp / ? AS INTEGER) * ? AS bucket_ts,
                CASE WHEN ack_received = 1 AND (cad_enabled = 1 OR lbt_enabled = 1)
                    AND (cad_enabled = 0 OR (cad_enabled = 1 AND cad_detected IS NOT NULL
                         AND cad_retries >= 0 AND COALESCE(cad_reason, '') NOT IN ('scan_error', 'not_run', 'unsupported_bw')))
                    AND (lbt_enabled = 0 OR (lbt_enabled = 1 AND lbt_pass IS NOT NULL AND lbt_retries >= 0))
                    THEN 1 + CASE WHEN cad_enabled = 1 THEN cad_retries ELSE 0 END
                           + CASE WHEN lbt_enabled = 1 THEN lbt_retries ELSE 0 END
                    ELSE NULL END AS attempts_total,
                CASE WHEN ack_received = 1 AND cad_enabled = 0 AND lbt_enabled = 0 THEN 1 ELSE 0 END AS disabled
            FROM tx_diagnostics WHERE timestamp >= ? AND timestamp <= ?
        )
        SELECT bucket_ts, COALESCE(packet_type, -1), channel_id, attempts_total, disabled,
            COUNT(*), SUM(CASE WHEN ack_received = 1 AND ok = 1 THEN 1 ELSE 0 END),
            SUM(CASE WHEN ack_received = 1 AND ok = 0 THEN 1 ELSE 0 END),
            SUM(CASE WHEN lbt_pass = 0 OR cad_detected = 1 OR cad_retries > 0 OR lbt_retries > 0 THEN 1 ELSE 0 END),
            SUM(ack_received), SUM(CASE WHEN scheduler_attempt > 1 THEN 1 ELSE 0 END),
            SUM(rssi), COUNT(rssi), SUM(snr), COUNT(snr)
        FROM normalized GROUP BY bucket_ts, packet_type, channel_id, attempts_total, disabled
        ORDER BY bucket_ts, packet_type, channel_id, attempts_total
    """, (bucket_seconds, bucket_seconds, start_timestamp, end_timestamp)).fetchall()
    total, total_distribution = _new_counts(), {}
    bucket_counts, bucket_distributions, type_counts, type_distributions = {}, {}, {}, {}
    type_bucket_counts, type_bucket_distributions, channel_counts, channel_distributions = {}, {}, {}, {}
    signals = {}
    for row in rows:
        (timestamp, packet_type, channel_id, attempts, disabled, count, succeeded, failed,
         busy, acknowledged, scheduler_retried, rssi_sum, rssi_count, snr_sum, snr_count) = row
        values = _new_counts()
        values.update(recorded_transmissions=count, acknowledged_transmissions=acknowledged,
                      confirmed_tx_outcomes=succeeded + failed, confirmed_tx_failures=failed,
                      scheduler_retried_transmissions=scheduler_retried)
        if attempts is None:
            values["disabled_transmissions" if disabled else "unknown_transmissions"] = count
        else:
            values.update(transmissions=count, total_attempts=attempts * count,
                          retry_packets=count if attempts > 1 else 0,
                          first_attempt_success=succeeded if attempts == 1 else 0,
                          failed_transmissions=failed, busy_channel_events=busy,
                          severe_contention_count=count if attempts >= severe_attempt_threshold else 0,
                          max_attempts=attempts)
            values[f"attempts_{attempts}" if attempts <= 3 else "attempts_4_plus"] = count
        groups = (
            (total, total_distribution),
            (bucket_counts.setdefault(timestamp, _new_counts()), bucket_distributions.setdefault(timestamp, {})),
            (type_counts.setdefault(packet_type, _new_counts()), type_distributions.setdefault(packet_type, {})),
            (type_bucket_counts.setdefault((timestamp, packet_type), _new_counts()),
             type_bucket_distributions.setdefault((timestamp, packet_type), {})),
            (channel_counts.setdefault(channel_id, _new_counts()), channel_distributions.setdefault(channel_id, {})),
        )
        for counts, distribution in groups:
            for key, value in values.items():
                counts[key] = max(counts[key], value) if key == "max_attempts" else counts[key] + value
            if attempts is not None:
                distribution[attempts] = distribution.get(attempts, 0) + count
        signal = signals.setdefault(timestamp, [0.0, 0, 0.0, 0])
        signal[0] += rssi_sum or 0
        signal[1] += rssi_count
        signal[2] += snr_sum or 0
        signal[3] += snr_count
    buckets = []
    for timestamp, counts in sorted(bucket_counts.items()):
        bucket = {"timestamp": timestamp, **_finish(counts, bucket_distributions[timestamp])}
        rssi_sum, rssi_count, snr_sum, snr_count = signals[timestamp]
        bucket["rf"] = {
            "traffic_volume": counts["recorded_transmissions"],
            "avg_rssi": rssi_sum / rssi_count if rssi_count else None,
            "avg_snr": snr_sum / snr_count if snr_count else None,
            "rssi_sample_count": rssi_count, "snr_sample_count": snr_count,
            "packet_loss_rate_pct": None,
        }
        buckets.append(bucket)
    summary = _finish(total, total_distribution)
    summary["total_transmissions"] = total["transmissions"]
    summary["has_lbt_data"] = total["transmissions"] > 0
    summary["severe_attempt_threshold"] = severe_attempt_threshold
    observed = [bucket for bucket in buckets if bucket["retry_rate_pct"] is not None]
    worst = max(observed, key=lambda bucket: bucket["retry_rate_pct"], default=None)
    summary["worst_bucket"] = ({key: worst[key] for key in (
        "timestamp", "retry_rate_pct", "attempts_3_plus_pct", "max_attempts", "transmissions",
    )} if worst else None)
    snr_pairs = [(bucket["retry_rate_pct"], bucket["rf"]["avg_snr"]) for bucket in observed
                 if bucket["rf"]["avg_snr"] is not None]
    return {
        "start_time": int(start_timestamp), "end_time": int(end_timestamp), "bucket_seconds": bucket_seconds,
        "supported": True, "data_source": "wm1303_tx_diagnostics",
        "attempts_source": "confirmed_hal_cad_lbt_retries",
        "traffic_source": "recorded_backend_tx_operations",
        "source_limitations": [
            "Legacy packet records are excluded because they do not contain measured WM1303 retry counts.",
            "CAD/LBT attempts count checks within each backend TX operation; scheduler retries are reported separately.",
            "Packet reception loss is not measured by TX outcomes and remains unknown.",
        ],
        "summary": summary, "buckets": buckets,
        "packet_types": [{"packet_type": key, "packet_type_label": _label(key),
                          **_finish(counts, type_distributions[key])}
                         for key, counts in sorted(type_counts.items(), key=lambda entry: -entry[1]["transmissions"])],
        "packet_type_buckets": [{"timestamp": timestamp, "packet_type": packet_type,
                                 "packet_type_label": _label(packet_type),
                                 **_finish(counts, type_bucket_distributions[(timestamp, packet_type)])}
                                for (timestamp, packet_type), counts in sorted(type_bucket_counts.items())],
        "channels": [{"channel_id": key, **_finish(counts, channel_distributions[key])}
                     for key, counts in sorted(channel_counts.items())],
        "correlations": {"retry_rate_vs_avg_snr": _correlation(snr_pairs),
                         "retry_rate_vs_packet_loss_rate": {"coefficient": None, "sample_count": 0}},
    }
