import logging
import math
import time
from pathlib import Path
from typing import Optional

try:
    import rrdtool

    RRDTOOL_AVAILABLE = True
except ImportError:
    RRDTOOL_AVAILABLE = False

logger = logging.getLogger("RRDToolHandler")


class RRDToolHandler:
    def __init__(self, storage_dir: Path):
        self.storage_dir = storage_dir
        self.rrd_path = self.storage_dir / "metrics.rrd"
        self.available = RRDTOOL_AVAILABLE
        self._last_rrd_update = 0
        self._last_counters = {}
        self._counter_baseline_loaded = False
        # Cache default 24-hour reads separately for each consolidation function.
        self._get_data_cache = {}
        self._init_rrd()
        if self.available and self.rrd_path.exists():
            try:
                # Startup reads preserve the timestamp and counter baseline
                # without querying metadata for every packet. lastupdate's date
                # is local/naive; last() avoids ambiguity during a DST fallback.
                previous = rrdtool.lastupdate(str(self.rrd_path))
                self._last_rrd_update = rrdtool.last(str(self.rrd_path))
                self._last_counters = {
                    name: int(value) for name, value in previous["ds"].items()
                    if value is not None and math.isfinite(value)
                }
                self._counter_baseline_loaded = True
            except Exception as exc:
                # The first update will reset COUNTER baselines to UNKNOWN,
                # avoiding a possible rollover spike when old values cannot be read.
                logger.warning("Could not load RRD counter baseline: %s", exc)

    def _init_rrd(self):
        if not self.available:
            logger.warning("RRDTool not available - skipping RRD initialization")
            return

        if self.rrd_path.exists():
            logger.info(f"RRD database exists: {self.rrd_path}")
            return

        try:
            rrdtool.create(
                str(self.rrd_path),
                "--step",
                "60",
                "--start",
                str(int(time.time() - 60)),
                "DS:rx_count:COUNTER:120:0:U",
                "DS:tx_count:COUNTER:120:0:U",
                "DS:drop_count:COUNTER:120:0:U",
                "DS:avg_rssi:GAUGE:120:-200:0",
                "DS:avg_snr:GAUGE:120:-30:30",
                "DS:avg_length:GAUGE:120:0:256",
                "DS:avg_score:GAUGE:120:0:1",
                "DS:neighbor_count:GAUGE:120:0:U",
                "DS:type_0:COUNTER:120:0:U",
                "DS:type_1:COUNTER:120:0:U",
                "DS:type_2:COUNTER:120:0:U",
                "DS:type_3:COUNTER:120:0:U",
                "DS:type_4:COUNTER:120:0:U",
                "DS:type_5:COUNTER:120:0:U",
                "DS:type_6:COUNTER:120:0:U",
                "DS:type_7:COUNTER:120:0:U",
                "DS:type_8:COUNTER:120:0:U",
                "DS:type_9:COUNTER:120:0:U",
                "DS:type_10:COUNTER:120:0:U",
                "DS:type_11:COUNTER:120:0:U",
                "DS:type_12:COUNTER:120:0:U",
                "DS:type_13:COUNTER:120:0:U",
                "DS:type_14:COUNTER:120:0:U",
                "DS:type_15:COUNTER:120:0:U",
                "DS:type_other:COUNTER:120:0:U",
                "RRA:AVERAGE:0.5:1:10080",
                "RRA:AVERAGE:0.5:5:8640",
                "RRA:AVERAGE:0.5:60:8760",
                "RRA:MAX:0.5:1:10080",
                "RRA:MIN:0.5:1:10080",
            )
            logger.info(f"RRD database created: {self.rrd_path}")

        except Exception as e:
            logger.error(f"Failed to create RRD database: {e}")

    def update_packet_metrics(self, record: dict, cumulative_counts: dict):
        """Write increasing integer-second samples into the 60-second RRD.

        Multiple updates per archive step are valid. Repeated or out-of-order
        seconds are skipped; later samples carry their cumulative increments.
        A decreasing counter is written as UNKNOWN, not interpreted as a wrap.
        """
        if not self.available or not self.rrd_path.exists():
            return

        try:
            timestamp = int(record.get("timestamp", time.time()))

            # RRDtool requires strictly increasing update timestamps.
            if timestamp <= self._last_rrd_update:
                return

            # Build update string from cumulative counts
            type_counts = cumulative_counts.get("type_counts", {})
            counters = {
                "rx_count": int(cumulative_counts.get("rx_total", 0)),
                "tx_count": int(cumulative_counts.get("tx_total", 0)),
                "drop_count": int(cumulative_counts.get("drop_total", 0)),
                **{f"type_{i}": int(type_counts.get(f"type_{i}", 0)) for i in range(16)},
                "type_other": int(type_counts.get("type_other", 0)),
            }
            encoded_counters = {}
            for name, value in counters.items():
                previous = self._last_counters.get(name)
                reset = previous is not None and value < previous
                encoded_counters[name] = (
                    "U" if not self._counter_baseline_loaded or reset or value < 0
                    else str(value)
                )
            rx_total = encoded_counters["rx_count"]
            tx_total = encoded_counters["tx_count"]
            drop_total = encoded_counters["drop_count"]
            type_values = [encoded_counters[f"type_{i}"] for i in range(16)]
            type_values.append(encoded_counters["type_other"])

            rssi = record.get("rssi")
            snr = record.get("snr")
            score = record.get("score")

            rssi_val = "U" if rssi is None else str(rssi)
            snr_val = "U" if snr is None else str(snr)
            score_val = "U" if score is None else str(score)
            length_val = str(record.get("length", 0))

            basic_values = (
                f"{timestamp}:{rx_total}:{tx_total}:{drop_total}:"
                f"{rssi_val}:{snr_val}:{length_val}:{score_val}:"
                f"U"
            )

            type_values_str = ":".join(type_values)
            values = f"{basic_values}:{type_values_str}"

            rrdtool.update(str(self.rrd_path), values)
            self._last_rrd_update = timestamp
            self._last_counters = counters
            self._counter_baseline_loaded = True
            self._get_data_cache.clear()

        except Exception as e:
            logger.error(f"Failed to update RRD packet metrics: {e}")
            logger.debug(f"RRD packet update failed - record: {record}")

    def get_data(
        self,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        resolution: str = "average",
    ) -> Optional[dict]:
        if not self.available or not self.rrd_path.exists():
            logger.error(
                f"RRD not available: available={self.available}, rrd_path exists={self.rrd_path.exists()}"
            )
            return None

        # Default reads may be up to 60 seconds old; accepted writes invalidate
        # them immediately. Explicit bounds always bypass this cache.
        now = time.time()
        cache_time = time.monotonic()
        consolidation = resolution.upper()
        use_cache = start_time is None and end_time is None
        if use_cache:
            cached = self._get_data_cache.get(consolidation)
            if cached is not None and cache_time - cached[0] < 60.0:
                return cached[1]

        try:
            if end_time is None:
                end_time = int(now)
            if start_time is None:
                start_time = end_time - (24 * 3600)

            fetch_result = rrdtool.fetch(
                str(self.rrd_path),
                consolidation,
                "--start",
                str(start_time),
                "--end",
                str(end_time),
            )

            if not fetch_result:
                logger.error("RRD fetch returned None")
                return None

            (start, end, step), data_sources, data_points = fetch_result

            if not data_points:
                logger.warning("No data points returned from RRD fetch")

            result = {
                "start_time": start,
                "end_time": end,
                "step": step,
                "data_sources": data_sources,
                "counter_mode": "rate",
                "timestamp_convention": "bucket_start",
                "packet_types": {},
                "metrics": {},
            }

            timestamps = []
            # RRDtool's first point covers (start, start + step]. Label chart
            # points by their bucket START, not by RRDtool's printed end time.
            current_time = start

            for ds in data_sources:
                if ds.startswith("type_"):
                    if "packet_types" not in result:
                        result["packet_types"] = {}
                    result["packet_types"][ds] = []
                else:
                    result["metrics"][ds] = []

            for point in data_points:
                timestamps.append(current_time)

                for i, value in enumerate(point):
                    ds_name = data_sources[i]
                    if ds_name.startswith("type_"):
                        result["packet_types"][ds_name].append(value)
                    else:
                        result["metrics"][ds_name].append(value)

                current_time += step

            result["timestamps"] = timestamps

            # Populate read cache for default (unconstrained) calls only.
            if use_cache:
                self._get_data_cache[consolidation] = (cache_time, result)

            return result

        except Exception as e:
            logger.error(f"Failed to get RRD data: {e}")
            return None

    def get_packet_type_stats(self, hours: int = 24) -> Optional[dict]:
        try:
            end_time = int(time.time())
            start_time = end_time - int(hours * 3600)

            rrd_data = self.get_data(start_time, end_time)
            if not rrd_data or "packet_types" not in rrd_data:
                logger.warning("No RRD data available")
                return None

            type_totals = {}
            packet_type_names = {
                "type_0": "Request (REQ)",
                "type_1": "Response (RESPONSE)",
                "type_2": "Plain Text Message (TXT_MSG)",
                "type_3": "Acknowledgment (ACK)",
                "type_4": "Node Advertisement (ADVERT)",
                "type_5": "Group Text Message (GRP_TXT)",
                "type_6": "Group Datagram (GRP_DATA)",
                "type_7": "Anonymous Request (ANON_REQ)",
                "type_8": "Returned Path (PATH)",
                "type_9": "Trace (TRACE)",
                "type_10": "Multi-part Packet (MULTIPART)",
                "type_11": "Control (CONTROL)",
                "type_12": "Reserved Type 12",
                "type_13": "Reserved Type 13",
                "type_14": "Reserved Type 14",
                "type_15": "Custom Packet (RAW_CUSTOM)",
                "type_other": "Other Types (>15)",
            }

            # COUNTER archives contain rates, not running totals. Integrate
            # only the overlap with the requested window: fetch rounds its
            # bounds and may return an extra trailing bucket.
            timestamps = rrd_data["timestamps"]
            step = rrd_data["step"]
            valid_points = 0
            for type_key, data_points in rrd_data["packet_types"].items():
                total = 0.0
                for bucket_start, rate in zip(timestamps, data_points):
                    overlap = min(bucket_start + step, end_time) - max(bucket_start, start_time)
                    if overlap > 0 and rate is not None and math.isfinite(rate) and rate >= 0:
                        total += rate * overlap
                        valid_points += 1
                type_name = packet_type_names.get(type_key, type_key)
                type_totals[type_name] = total

            if not valid_points:
                logger.warning("No known RRD packet rates in the requested window")
                return None

            result = {
                "hours": hours,
                "packet_type_totals": type_totals,
                "total_packets": sum(type_totals.values()),
                "period": f"{hours} hours",
                "data_source": "rrd",
                # Archive rates interpolate within buckets; UNKNOWN/reset gaps
                # are excluded, not assumed to have zero actual traffic.
                "approximate": True,
                "coverage": "observed",
            }

            return result

        except Exception as e:
            logger.error(f"Failed to get packet type stats from RRD: {e}")
            return None
