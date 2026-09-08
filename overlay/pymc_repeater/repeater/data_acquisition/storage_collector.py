import concurrent.futures
from copy import deepcopy
import logging
import threading
import time
from typing import Optional

from repeater.config import resolve_storage_dir

from .mqtt_handler import MeshCoreToMqttPusher
from .rrdtool_handler import RRDToolHandler
from .sqlite_handler import SQLiteHandler
from .storage_utils import PacketRecord

logger = logging.getLogger("StorageCollector")


class StorageCollector:
    def __init__(self, config: dict, local_identity=None, repeater_handler=None):
        self.config = config
        self.repeater_handler = repeater_handler
        self.glass_publish_callback = None
        self._close_lock = threading.RLock()
        self._shutdown_lock = threading.Lock()
        self._close_complete = False
        self._closed = False
        self._write_slots = threading.BoundedSemaphore(1024)
        self.storage_dropped = 0
        self.storage_errors = 0
        self._writes_pending = 0

        self._db_executor = None
        self._db_worker_ident = None
        self._writer_close_error = None
        self._caller_close_error = None
        self.sqlite_handler = None
        self.mqtt_handler = None
        self._stats_stop_event = threading.Event()
        self._stats_thread = None
        self._stats_close_error = None
        try:
            metrics_config = config.get("metrics")
            if not isinstance(metrics_config, dict):
                metrics_config = {}
            self.rrd_enabled = bool(metrics_config.get("rrd_enabled", True))

            # Dedicated single writer thread for all blocking storage work (the SQLite
            # write, the cumulative-counts aggregate, RRD updates, and network
            # publishing). This keeps that work off the asyncio event loop, which it
            # was previously stalling for seconds per packet on a busy mesh — starving
            # every other coroutine (e.g. send_advert would time out). One worker
            # preserves packet write ordering and reuses a single thread-local SQLite
            # connection (no WAL writer contention, no connection fan-out).
            self._db_executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="storage-writer"
            )

            self.storage_dir = resolve_storage_dir(config)
            self.storage_dir.mkdir(parents=True, exist_ok=True)

            self.sqlite_handler = SQLiteHandler(self.storage_dir)
            self.rrd_handler = None
            if self.rrd_enabled:
                candidate_rrd_handler = RRDToolHandler(self.storage_dir)
                if (
                    candidate_rrd_handler.available
                    and candidate_rrd_handler.rrd_path.exists()
                ):
                    self.rrd_handler = candidate_rrd_handler
                    logger.info("RRDtool metrics enabled")
                else:
                    logger.warning(
                        "RRDtool requested but unavailable; using SQLite metrics fallback"
                    )
            else:
                logger.info(
                    "RRDtool metrics disabled; SQLite metrics fallback will be used"
                )

            # Initialize MQTT handler only when at least one broker is configured
            self.mqtt_handler = None
            mqtt_brokers_config = config.get("mqtt_brokers", {}) or {}
            letsmesh_config = config.get("letsmesh", {}) or {}
            mqtt_config = config.get("mqtt", {}) or {}
            has_brokers_configured = (
                bool(mqtt_brokers_config.get("brokers"))
                or bool(letsmesh_config)
                or bool(mqtt_config)
            )
            if has_brokers_configured and local_identity:
                try:
                    # Pass local_identity directly (supports both standard and firmware keys)
                    self.mqtt_handler = MeshCoreToMqttPusher(
                        local_identity=local_identity,
                        config=config,
                        stats_provider=self._get_live_stats,
                    )
                    self.mqtt_handler.connect()

                    public_key_hex = local_identity.get_public_key().hex()
                    logger.info(
                        f"MQTT handler initialized with public key: {public_key_hex[:16]}..."
                    )
                except Exception as e:
                    logger.error(f"Failed to initialize MQTT handler: {e}")
                    self.mqtt_handler = None
            else:
                logger.info("MQTT handler disabled - no brokers configured")

            # Initialize hardware stats collector
            from .hardware_stats import HardwareStatsCollector

            self.hardware_stats = HardwareStatsCollector()
            logger.info("Hardware stats collector initialized")

            # Initialize WebSocket handler for real-time updates
            self.websocket_available = False
            self.websocket_has_connected_clients = lambda: False
            self._ws_stats_broadcast_interval_sec: float = 5.0
            self._stats_stop_event = threading.Event()
            self._stats_thread: Optional[threading.Thread] = None
            try:
                from .websocket_handler import (
                    broadcast_packet,
                    broadcast_stats,
                    has_connected_clients,
                )

                self.websocket_broadcast_packet = broadcast_packet
                self.websocket_broadcast_stats = broadcast_stats
                self.websocket_has_connected_clients = has_connected_clients
                self.websocket_available = True
                logger.info("WebSocket handler initialized for real-time updates")

                # Broadcast aggregate stats on a fixed cadence rather than inline on the
                # per-packet write path. get_packet_stats(24h) is a multi-second aggregate;
                # running it inside _record_packet_blocking made the storage writer thread
                # spend ~1-2s of every 5s on it, competing with packet inserts. A dedicated
                # tick keeps the writer doing only fast writes and only runs the aggregate
                # when a dashboard client is actually connected.
                self._stats_thread = threading.Thread(
                    target=self._stats_broadcast_loop,
                    name="stats-broadcast",
                    daemon=True,
                )
                self._stats_thread.start()
            except ImportError:
                logger.debug("WebSocket handler not available")
        except BaseException:
            try:
                self.close()
            except Exception as exc:
                logger.error("Storage initialization cleanup also failed: %s", type(exc).__name__)
            raise

    def _get_live_stats(self) -> dict:
        """Get live stats from RepeaterHandler"""
        if not self.repeater_handler:
            return {
                "uptime_secs": 0,
                "packets_sent": 0,
                "packets_received": 0,
                "errors": 0,
                "queue_len": 0,
            }

        uptime_secs = int(time.time() - self.repeater_handler.start_time)

        # Get airtime stats
        airtime_stats = self.repeater_handler.airtime_mgr.get_stats()

        # Get latest noise floor from database
        noise_floor = None
        try:
            recent_noise = self.sqlite_handler.get_noise_floor_history(
                hours=0.5, limit=1
            )
            if recent_noise and len(recent_noise) > 0:
                noise_floor = recent_noise[-1].get("noise_floor_dbm")
        except Exception as e:
            logger.debug(f"Could not fetch noise floor: {e}")

        stats = {
            "uptime_secs": uptime_secs,
            "packets_sent": self.repeater_handler.forwarded_count,
            "packets_received": self.repeater_handler.rx_count,
            "errors": 0,
            "queue_len": 0,  # N/A for Python repeater
        }

        # Add airtime stats
        if airtime_stats:
            stats["tx_air_secs"] = airtime_stats["total_airtime_ms"] / 1000
            stats["current_airtime_ms"] = airtime_stats["current_airtime_ms"]
            stats["utilization_percent"] = airtime_stats["utilization_percent"]

        # Add noise floor if available
        if noise_floor is not None:
            stats["noise_floor"] = noise_floor

        return stats

    def record_packet(
        self,
        packet_record: dict,
        skip_letsmesh_if_invalid: bool = True,
        *,
        skip_mqtt_if_invalid: Optional[bool] = None,
    ):
        """Record a packet to storage and publish it.

        All blocking work — the SQLite write, the cumulative-counts aggregate, the
        RRD update, and network publishing — runs on the dedicated writer thread so
        it never blocks the asyncio event loop. Callers treat this as
        fire-and-forget (the previous synchronous version blocked the loop).

        Args:
            packet_record: Dictionary containing packet information
            skip_mqtt_if_invalid: If True, don't publish packets with drop_reason to mqtt
        """
        logger.debug(
            f"Recording packet: type={packet_record.get('type')}, "
            f"transmitted={packet_record.get('transmitted')}"
        )
        skip = (
            skip_letsmesh_if_invalid
            if skip_mqtt_if_invalid is None
            else skip_mqtt_if_invalid
        )
        return self._submit_db(
            self._record_packet_blocking, deepcopy(packet_record), skip
        )

    def _submit_db(self, fn, *args):
        """Queue blocking work in order; never move it back onto the RX loop."""
        with self._close_lock:
            if self._closed or not self._write_slots.acquire(blocking=False):
                self.storage_dropped += 1
                if self.storage_dropped == 1 or self.storage_dropped % 100 == 0:
                    logger.error(
                        "Storage record rejected (closed or backlog full); total=%d",
                        self.storage_dropped,
                    )
                return False
            try:
                self._writes_pending += 1
                future = self._db_executor.submit(self._run_db_task, fn, *args)
            except BaseException:
                self._writes_pending -= 1
                self._write_slots.release()
                raise
            future.add_done_callback(self._write_done)
            return True

    def _write_done(self, future):
        with self._close_lock:
            self._writes_pending -= 1
            self._write_slots.release()

    def get_storage_stats(self):
        with self._close_lock:
            return {
                "pending_writes": self._writes_pending,
                "rejected_writes": self.storage_dropped,
                "failed_writes": self.storage_errors,
            }

    def _run_db_task(self, fn, *args):
        """Execute a writer-thread task, logging (not raising) on failure."""
        self._db_worker_ident = threading.get_ident()
        try:
            fn(*args)
        except Exception as e:
            with self._close_lock:
                self.storage_errors += 1
            logger.error(f"Storage writer task failed: {e}", exc_info=True)

    def _record_packet_blocking(self, packet_record: dict, skip_mqtt: bool):
        """Store, aggregate, update metrics, and publish one packet (writer thread)."""
        packet_id = self.sqlite_handler.store_packet(packet_record)
        if packet_id is not None:
            packet_record["id"] = packet_id
        else:
            with self._close_lock:
                self.storage_errors += 1

        if self.rrd_handler is not None:
            try:
                cumulative_counts = self.sqlite_handler.get_cumulative_counts()
                self.rrd_handler.update_packet_metrics(packet_record, cumulative_counts)
            except Exception as exc:
                # A secondary metrics failure must not suppress packet delivery.
                logger.warning("RRDtool packet metrics update failed: %s", exc)

        self._publish_packet_sync(packet_record, skip_mqtt)

    def _publish_packet_sync(self, packet_record: dict, skip_mqtt: bool):
        """Publish a single packet (glass, per-packet WebSocket event, MQTT).

        Only fast, per-packet work runs here. The aggregate stats broadcast is
        driven separately by _stats_broadcast_loop so the writer thread is not
        held by the multi-second get_packet_stats(24h) query.
        """
        self._publish_to_glass(packet_record, "packet")

        if self.websocket_available:
            try:
                self.websocket_broadcast_packet(packet_record)
            except Exception as e:
                logger.debug(f"WebSocket broadcast failed: {e}")

        if not (skip_mqtt and packet_record.get("drop_reason")):
            self._publish_packet_to_mqtt(packet_record)

    def _broadcast_stats_once(self) -> None:
        """Compute the 24h aggregate and broadcast it to WebSocket clients."""
        packet_stats_24h = self.sqlite_handler.get_packet_stats(hours=24)
        uptime_seconds = (
            time.time() - self.repeater_handler.start_time
            if self.repeater_handler
            else 0
        )
        self.websocket_broadcast_stats(
            {
                "packet_stats": packet_stats_24h,
                "system_stats": {"uptime_seconds": uptime_seconds},
            }
        )

    def _stats_broadcast_loop(self) -> None:
        """Broadcast aggregate stats every interval while clients are connected.

        Runs on its own thread (off the event loop and off the storage writer) so
        the heavy get_packet_stats(24h) aggregate never sits in the packet write
        path. Skips the query entirely when no dashboard client is connected.
        """
        try:
            while not self._stats_stop_event.wait(
                self._ws_stats_broadcast_interval_sec
            ):
                try:
                    if self.websocket_has_connected_clients():
                        self._broadcast_stats_once()
                except Exception as e:
                    logger.debug(f"Stats broadcast failed: {e}")
        finally:
            try:
                self.sqlite_handler.close_thread_connection()
            except Exception as exc:
                # join() alone cannot report failed worker finalization.
                self._stats_close_error = exc
                logger.error("Stats connection close failed: %s", type(exc).__name__)

    def _publish_packet_to_mqtt(self, packet_record: dict):
        """Publish packet to mqtt broker if enabled and allowed.

        The ``duration`` field in the published JSON is sourced from
        ``packet_record['airtime_ms']``, populated upstream by
        RepeaterHandler._build_packet_record using the Semtech reference
        time-on-air formula. No recomputation is needed here.
        """
        if not self.mqtt_handler:
            return

        try:
            packet_type = packet_record.get("type")
            if packet_type is None:
                logger.error(
                    "Cannot publish to mqtt: packet_record missing 'type' field"
                )
                return

            node_name = self.config.get("repeater", {}).get("node_name", "Unknown")
            packet = PacketRecord.from_packet_record(
                packet_record, origin=node_name, origin_id=self.mqtt_handler.public_key
            )

            if packet:
                self.mqtt_handler.publish_packet(packet.to_dict())
                logger.debug(f"Published packet type 0x{packet_type:02X} to mqtt")
            else:
                logger.debug("Skipped mqtt publish: packet missing raw_packet data")

        except Exception as e:
            logger.error(f"Failed to publish packet to mqtt: {e}", exc_info=True)

    def record_advert(self, advert_record: dict):
        return self._submit_db(
            self._record_other_blocking,
            "store_advert",
            deepcopy(advert_record),
            self._publish_advert_sync,
        )

    def _publish_advert_sync(self, advert_record: dict):
        if self.mqtt_handler:
            self.mqtt_handler.publish_mqtt(advert_record, "advert")
        self._publish_to_glass(advert_record, "advert")

    def record_noise_floor(self, noise_floor_dbm: float):
        record = {"timestamp": time.time(), "noise_floor_dbm": noise_floor_dbm}
        return self._submit_db(
            self._record_other_blocking,
            "store_noise_floor",
            record,
            self._publish_noise_floor_sync,
        )

    def _publish_noise_floor_sync(self, noise_record: dict):
        if self.mqtt_handler:
            self.mqtt_handler.publish_mqtt(noise_record, "noise_floor")
        self._publish_to_glass(noise_record, "noise_floor")

    def record_crc_errors(self, count: int):
        record = {"timestamp": time.time(), "count": count}
        return self._submit_db(
            self._record_other_blocking,
            "store_crc_errors",
            record,
            self._publish_crc_errors_sync,
        )

    def _record_other_blocking(self, method, record, publish):
        getattr(self.sqlite_handler, method)(record)
        publish(record)

    def _publish_crc_errors_sync(self, crc_record: dict):
        if self.mqtt_handler:
            self.mqtt_handler.publish_mqtt(crc_record, "crc_errors")
        self._publish_to_glass(crc_record, "crc_errors")

    def get_crc_error_count(self, hours: int = 24) -> int:
        return self.sqlite_handler.get_crc_error_count(hours)

    def get_crc_error_history(self, hours: int = 24, limit: int = None) -> list:
        return self.sqlite_handler.get_crc_error_history(hours, limit)

    def get_policy_event_counts(
        self,
        start_timestamp: float,
        end_timestamp: float,
        bucket_seconds: int = 60,
    ) -> list:
        return self.sqlite_handler.get_policy_event_counts(
            start_timestamp=start_timestamp,
            end_timestamp=end_timestamp,
            bucket_seconds=bucket_seconds,
        )

    def get_lbt_diagnostics(
        self,
        start_timestamp: float,
        end_timestamp: float,
        bucket_seconds: int = 300,
        severe_attempt_threshold: int = 4,
    ) -> dict:
        return self.sqlite_handler.get_lbt_diagnostics(
            start_timestamp=start_timestamp,
            end_timestamp=end_timestamp,
            bucket_seconds=bucket_seconds,
            severe_attempt_threshold=severe_attempt_threshold,
        )

    def get_packet_stats(self, hours: int = 24) -> dict:
        return self.sqlite_handler.get_packet_stats(hours)

    def get_recent_packets(self, limit: int = 100) -> list:
        return self.sqlite_handler.get_recent_packets(limit)

    def get_filtered_packets(
        self,
        packet_type: Optional[int] = None,
        route: Optional[int] = None,
        start_timestamp: Optional[float] = None,
        end_timestamp: Optional[float] = None,
        limit: int = 1000,
        offset: int = 0,
    ) -> list:
        return self.sqlite_handler.get_filtered_packets(
            packet_type, route, start_timestamp, end_timestamp, limit, offset
        )

    def get_airtime_data(
        self,
        start_timestamp: Optional[float] = None,
        end_timestamp: Optional[float] = None,
        limit: int = 50000,
    ) -> list:
        return self.sqlite_handler.get_airtime_data(
            start_timestamp, end_timestamp, limit
        )

    def get_airtime_buckets(
        self,
        start_timestamp: float,
        end_timestamp: float,
        bucket_seconds: int = 60,
        sf: int = 9,
        bw_hz: int = 62500,
        cr: int = 5,
        preamble: int = 17,
    ) -> dict:
        return self.sqlite_handler.get_airtime_buckets(
            start_timestamp, end_timestamp, bucket_seconds, sf, bw_hz, cr, preamble
        )

    def get_packet_by_hash(self, packet_hash: str) -> Optional[dict]:
        return self.sqlite_handler.get_packet_by_hash(packet_hash)

    def get_packet_by_id(self, packet_id: int) -> Optional[dict]:
        return self.sqlite_handler.get_packet_by_id(packet_id)

    def get_neighbor_link_history(
        self,
        *,
        peer_hash: str,
        path_hash_size: int,
        hours: int = 24,
        limit: int = 1000,
    ) -> list:
        return self.sqlite_handler.get_neighbor_link_history(
            peer_hash=peer_hash,
            path_hash_size=path_hash_size,
            hours=hours,
            limit=limit,
        )

    def get_rrd_data(
        self,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        resolution: str = "average",
    ) -> Optional[dict]:
        return self.get_metrics_data(start_time, end_time, resolution)

    def get_metrics_data(
        self,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        resolution: str = "average",
    ) -> dict:
        if self.rrd_handler is not None:
            try:
                rrd_data = self.rrd_handler.get_data(start_time, end_time, resolution)
            except Exception as e:
                logger.warning(
                    f"RRDtool metrics read failed; using SQLite metrics fallback: {e}",
                    exc_info=True,
                )
            else:
                if self._metrics_data_is_valid(rrd_data):
                    rrd_data.setdefault("data_source", "rrd")
                    return rrd_data

                logger.warning(
                    "RRDtool metrics read returned no usable data; using SQLite metrics fallback"
                )

        sqlite_data = self.sqlite_handler.get_metrics_data(
            start_time, end_time, resolution
        )
        sqlite_data.setdefault("data_source", "sqlite")
        return sqlite_data

    def _metrics_data_is_valid(self, metrics_data: Optional[dict]) -> bool:
        if not isinstance(metrics_data, dict):
            return False
        if not isinstance(metrics_data.get("metrics"), dict):
            return False
        if not isinstance(metrics_data.get("timestamps"), list):
            return False
        return True

    def get_packet_type_stats(self, hours: int = 24) -> dict:
        if self.rrd_handler is not None:
            try:
                rrd_stats = self.rrd_handler.get_packet_type_stats(hours)
            except Exception as e:
                logger.warning(
                    f"RRDtool packet type stats failed; using SQLite fallback: {e}",
                    exc_info=True,
                )
            else:
                if rrd_stats:
                    return rrd_stats

            logger.warning("Falling back to SQLite for packet type stats")
        return self.sqlite_handler.get_packet_type_stats(hours)

    def get_route_stats(self, hours: int = 24) -> dict:
        return self.sqlite_handler.get_route_stats(hours)

    def record_neighbour_sample(
        self, pubkey: str, rssi, snr, channel: str = ""
    ) -> bool:
        return self._submit_db(
            self.sqlite_handler.record_neighbour_sample, pubkey, rssi, snr, channel
        )

    def get_neighbour_samples(self, pubkey: str, limit: int = 200) -> list:
        return self.sqlite_handler.get_neighbour_samples(pubkey, limit)

    def get_neighbors(self) -> dict:
        return self.sqlite_handler.get_neighbors()

    def get_node_name_by_pubkey(self, pubkey: str) -> Optional[str]:
        """
        Lookup node name from adverts table by public key.

        Args:
            pubkey: Public key in hex string format

        Returns:
            Node name if found, None otherwise
        """
        try:
            with self.sqlite_handler._connect() as conn:
                result = conn.execute(
                    "SELECT node_name FROM adverts WHERE pubkey = ? AND node_name IS NOT NULL ORDER BY last_seen DESC LIMIT 1",
                    (pubkey,),
                ).fetchone()
                return result[0] if result else None
        except Exception as e:
            logger.debug(
                f"Could not lookup node name for {pubkey[:8] if pubkey else 'None'}: {e}"
            )
            return None

    def cleanup_old_data(
        self, days: int = 7, companion_events_days: Optional[int] = None
    ):
        self.sqlite_handler.cleanup_old_data(
            days, companion_events_days=companion_events_days
        )

    def get_noise_floor_history(self, hours: int = 24, limit: int = None) -> list:
        return self.sqlite_handler.get_noise_floor_history(hours, limit)

    def get_noise_floor_stats(self, hours: int = 24) -> dict:
        return self.sqlite_handler.get_noise_floor_stats(hours)

    def close(self):
        # Stop admitting records, then drain accepted writes before disconnecting
        # publishers. This is called off the event loop by the daemon.
        if self._db_worker_ident == threading.get_ident():
            raise RuntimeError("Storage writer cannot join itself")
        self._stats_stop_event.set()
        if self._stats_thread is threading.current_thread():
            raise RuntimeError("Storage stats worker cannot join itself")
        # This lock is distinct from _close_lock: writer completion callbacks
        # need the admission/counter lock while shutdown waits for their work.
        with self._shutdown_lock:
            if self._close_complete:
                return
            with self._close_lock:
                self._closed = True
            if self._stats_thread is not None:
                # Constructor rollback may own a Thread whose start() failed.
                if self._stats_thread.ident is not None:
                    self._stats_thread.join()
            errors = []
            if self._stats_close_error is not None:
                errors.append(("Stats connection cleanup failed", self._stats_close_error))
            if self._db_executor is not None:
                executor = self._db_executor
                try:
                    if self.sqlite_handler is not None:
                        executor.submit(
                            self.sqlite_handler.close_thread_connection
                        ).result()
                except Exception as exc:
                    if self._writer_close_error is None:
                        self._writer_close_error = exc
                finally:
                    # Even failure to enqueue/finalize the connection cannot
                    # let accepted writes outlive their publishers. A failed
                    # owner close remains terminal after this thread exits.
                    try:
                        executor.shutdown(wait=True)
                    except Exception as exc:
                        if self._writer_close_error is None:
                            self._writer_close_error = exc
                        else:
                            logger.error("Storage writer drain also failed: %s", type(exc).__name__)
                    else:
                        self._db_executor = None
                        self._db_worker_ident = None
            if self._writer_close_error is not None:
                errors.append(("Storage writer cleanup failed", self._writer_close_error))
            if self.sqlite_handler is not None:
                try:
                    self.sqlite_handler.stop_wal_checkpoint_thread()
                except Exception as exc:
                    errors.append(("SQLite checkpoint cleanup failed", exc))
                try:
                    self.sqlite_handler.close_thread_connection()
                except Exception as exc:
                    # A retry of close() may run on a different to_thread
                    # worker, so it cannot certify this owner's cleanup.
                    if self._caller_close_error is None:
                        self._caller_close_error = exc
            if self._caller_close_error is not None:
                errors.append(("Storage caller connection cleanup failed", self._caller_close_error))
            if self.mqtt_handler and self._db_executor is None:
                try:
                    self.mqtt_handler.disconnect()
                    logger.info("MQTT handler disconnected")
                except Exception as exc:
                    errors.append(("MQTT disconnect failed", exc))
                else:
                    self.mqtt_handler = None
            if errors:
                for message, exc in errors:
                    logger.error("%s: %s", message, type(exc).__name__)
                raise RuntimeError(errors[0][0]) from errors[0][1]
            self._close_complete = True

    def set_glass_publisher(self, publish_callback):
        self.glass_publish_callback = publish_callback

    def _publish_to_glass(self, record: dict, record_type: str):
        if not self.glass_publish_callback:
            return
        try:
            self.glass_publish_callback(record_type, record)
        except Exception as e:
            logger.debug(f"Failed to publish telemetry to Glass MQTT: {e}")

    def create_transport_key(
        self,
        name: str,
        flood_policy: str,
        transport_key: Optional[str] = None,
        parent_id: Optional[int] = None,
        last_used: Optional[float] = None,
    ) -> Optional[int]:
        return self.sqlite_handler.create_transport_key(
            name, flood_policy, transport_key, parent_id, last_used
        )

    def get_transport_keys(self) -> list:
        return self.sqlite_handler.get_transport_keys()

    def get_transport_key_by_id(self, key_id: int) -> Optional[dict]:
        return self.sqlite_handler.get_transport_key_by_id(key_id)

    def update_transport_key(
        self,
        key_id: int,
        name: Optional[str] = None,
        flood_policy: Optional[str] = None,
        transport_key: Optional[str] = None,
        parent_id: Optional[int] = None,
        last_used: Optional[float] = None,
    ) -> bool:
        return self.sqlite_handler.update_transport_key(
            key_id, name, flood_policy, transport_key, parent_id, last_used
        )

    def delete_transport_key(self, key_id: int) -> bool:
        return self.sqlite_handler.delete_transport_key(key_id)

    def delete_advert(self, advert_id: int) -> bool:
        return self.sqlite_handler.delete_advert(advert_id)

    def delete_neighbors_by_pubkey_prefix(self, pubkey_prefix: str | None) -> int:
        return self.sqlite_handler.delete_neighbors_by_pubkey_prefix(pubkey_prefix)

    def get_hardware_stats(self) -> Optional[dict]:
        """Get current hardware statistics"""
        try:
            return self.hardware_stats.get_stats()
        except Exception as e:
            logger.error(f"Error getting hardware stats: {e}")
            return None

    def get_hardware_processes(self) -> Optional[list]:
        """Get current process summary"""
        try:
            return self.hardware_stats.get_processes_summary()
        except Exception as e:
            logger.error(f"Error getting hardware processes: {e}")
            return None
