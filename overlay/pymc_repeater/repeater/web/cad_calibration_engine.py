import asyncio
import logging
import random
import threading
from typing import Any, Dict

logger = logging.getLogger("HTTPServer")


class CADCalibrationEngine:

    def __init__(self, daemon_instance=None, event_loop=None):
        self.daemon_instance = daemon_instance
        self.event_loop = event_loop
        self.running = False
        self.results = {}
        self.current_test = None
        self.progress = {"current": 0, "total": 0}
        self.clients = set()  # SSE clients
        self.calibration_thread = None
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._active_future = None
        self._active_tasks = set()
        self._closed = False
        self.message_queue = []
        self._message_generation = 0

    def get_test_ranges(self, spreading_factor: int):
        """Get CAD test ranges"""
        # Higher values = less sensitive, lower values = more sensitive
        # Test from LESS sensitive to MORE sensitive to find the sweet spot
        sf_ranges = {
            7: (range(22, 30, 1), range(12, 20, 1)),
            8: (range(22, 30, 1), range(12, 20, 1)),
            9: (range(24, 32, 1), range(14, 22, 1)),
            10: (range(26, 34, 1), range(16, 24, 1)),
            11: (range(28, 36, 1), range(18, 26, 1)),
            12: (range(30, 38, 1), range(20, 28, 1)),
        }
        return sf_ranges.get(spreading_factor, sf_ranges[8])

    async def test_cad_config(
        self, radio, det_peak: int, det_min: int, samples: int = 20
    ) -> Dict[str, Any]:

        detections = 0
        baseline_detections = 0

        async def sample(peak, minimum):
            result = await radio.perform_cad(
                det_peak=peak, det_min=minimum, timeout=0.3, calibration=True
            )
            if isinstance(result, dict):
                if result.get("error") or not result.get("cad_done"):
                    raise RuntimeError("CAD measurement did not complete")
                result = result.get("detected")
            if not isinstance(result, bool):
                raise RuntimeError("Radio returned an invalid CAD measurement")
            return result

        # First, get baseline with very insensitive settings (should detect nothing)
        baseline_samples = 5
        for _ in range(baseline_samples):
            # Failed/unfinished scans are not evidence of a clear channel.
            if await sample(35, 25):
                baseline_detections += 1
            await asyncio.sleep(0.1)  # 100ms between baseline samples

        # Wait before actual test
        await asyncio.sleep(0.5)

        # Now test the actual configuration
        for i in range(samples):
            if await sample(det_peak, det_min):
                detections += 1

            # Variable delay to avoid sampling artifacts
            delay = 0.05 + (i % 3) * 0.05  # 50ms, 100ms, 150ms rotation
            await asyncio.sleep(delay)

        # Calculate adjusted detection rate
        baseline_rate = (baseline_detections / baseline_samples) * 100
        detection_rate = (detections / samples) * 100

        # Subtract baseline noise
        adjusted_rate = max(0, detection_rate - baseline_rate)

        return {
            "det_peak": det_peak,
            "det_min": det_min,
            "samples": samples,
            "detections": detections,
            "detection_rate": detection_rate,
            "baseline_rate": baseline_rate,
            "adjusted_rate": adjusted_rate,  # This is the useful metric
            "sensitivity_score": self._calculate_sensitivity_score(
                det_peak, det_min, adjusted_rate
            ),
        }

    def _calculate_sensitivity_score(
        self, det_peak: int, det_min: int, adjusted_rate: float
    ) -> float:

        # Ideal detection rate is around 10-30% for good sensitivity without false positives
        ideal_rate = 20.0
        rate_penalty = abs(adjusted_rate - ideal_rate) / ideal_rate

        # Prefer moderate sensitivity settings (not too extreme)
        sensitivity_penalty = (abs(det_peak - 25) + abs(det_min - 15)) / 20.0

        # Lower penalty = higher score
        score = max(0, 100 - (rate_penalty * 50) - (sensitivity_penalty * 20))
        return score

    def broadcast_to_clients(self, data):

        # Store the message for clients to pick up
        with self._lock:
            self.last_message = data
            self.message_queue.append(data)

    def get_messages_since(self, index, generation):
        """Return one consistent SSE batch, restarting its cursor for a new run."""
        with self._lock:
            if generation != self._message_generation:
                index = 0
            return self.message_queue[index:], len(self.message_queue), self._message_generation

    async def _run_test(self, radio, det_peak, det_min, samples, generation):
        # A canceled concurrent Future can finish before the radio's awaited
        # cleanup. Retain the actual task until that cleanup really completes.
        task = asyncio.current_task()
        with self._lock:
            if self._closed or not self.running or generation != self._message_generation:
                raise asyncio.CancelledError
            self._active_tasks.add(task)
        try:
            return await self.test_cad_config(radio, det_peak, det_min, samples)
        finally:
            with self._lock:
                self._active_tasks.discard(task)

    def calibration_worker(self, samples: int, delay_ms: int):

        try:
            # Get radio from daemon instance
            if not self.daemon_instance:
                self.broadcast_to_clients(
                    {"type": "error", "message": "No daemon instance available"}
                )
                return

            radio = getattr(self.daemon_instance, "radio", None)
            if not radio:
                self.broadcast_to_clients(
                    {"type": "error", "message": "Radio instance not available"}
                )
                return
            if not hasattr(radio, "perform_cad"):
                self.broadcast_to_clients(
                    {"type": "error", "message": "Radio does not support CAD"}
                )
                return

            # Get spreading factor from daemon instance
            config = getattr(self.daemon_instance, "config", {})
            radio_config = config.get("radio", {})
            sf = radio_config.get("spreading_factor", 8)

            # Get test ranges
            peak_range, min_range = self.get_test_ranges(sf)

            total_tests = len(peak_range) * len(min_range)
            self.progress = {"current": 0, "total": total_tests}

            self.broadcast_to_clients(
                {
                    "type": "status",
                    "message": f"Starting calibration: SF{sf}, {total_tests} tests",
                    "test_ranges": {
                        "peak_min": min(peak_range),
                        "peak_max": max(peak_range),
                        "min_min": min(min_range),
                        "min_max": max(min_range),
                        "spreading_factor": sf,
                        "total_tests": total_tests,
                    },
                }
            )

            current = 0

            peak_list = list(peak_range)
            min_list = list(min_range)

            # Create all test combinations
            test_combinations = []
            for det_peak in peak_list:
                for det_min in min_list:
                    test_combinations.append((det_peak, det_min))

            # Sort by distance from center for center-out pattern
            peak_center = (max(peak_list) + min(peak_list)) / 2
            min_center = (max(min_list) + min(min_list)) / 2

            def distance_from_center(combo):
                peak, min_val = combo
                return ((peak - peak_center) ** 2 + (min_val - min_center) ** 2) ** 0.5

            # Sort by distance from center
            test_combinations.sort(key=distance_from_center)

            # Randomize within bands for better coverage
            band_size = max(1, len(test_combinations) // 8)  # Create 8 bands
            randomized_combinations = []

            for i in range(0, len(test_combinations), band_size):
                band = test_combinations[i : i + band_size]
                random.shuffle(band)  # Randomize within each band
                randomized_combinations.extend(band)

            # Run calibration in event loop with center-out randomized pattern
            if self.event_loop:
                for det_peak, det_min in randomized_combinations:
                    if not self.running:
                        break

                    current += 1
                    self.progress["current"] = current

                    # Update progress
                    self.broadcast_to_clients(
                        {
                            "type": "progress",
                            "current": current,
                            "total": total_tests,
                            "peak": det_peak,
                            "min": det_min,
                        }
                    )

                    # Run the test
                    with self._lock:
                        if not self.running:
                            break
                        test = self._run_test(
                            radio, det_peak, det_min, samples, self._message_generation
                        )
                        try:
                            future = asyncio.run_coroutine_threadsafe(test, self.event_loop)
                        except Exception:
                            test.close()
                            raise
                        self._active_future = future

                    try:
                        # Include transport/lock overhead and all requested samples.
                        result = future.result(timeout=max(30, samples * 2 + 15))

                        # Store result
                        key = f"{det_peak}-{det_min}"
                        self.results[key] = result

                        # Send result to clients
                        self.broadcast_to_clients({"type": "result", **result})
                    except Exception as e:
                        future.cancel()
                        if not self.running:
                            break
                        raise RuntimeError(f"CAD test failed for peak={det_peak}, min={det_min}: {e}") from e
                    finally:
                        with self._lock:
                            self._active_future = None

                    # Delay between tests
                    if self.running and delay_ms > 0:
                        self._stop_event.wait(delay_ms / 1000.0)

            if self.running:
                # Find best result based on sensitivity score (not just detection rate)
                best_result = None
                recommended_result = None
                if self.results:
                    # Find result with highest sensitivity score (best balance)
                    best_result = max(
                        self.results.values(), key=lambda x: x.get("sensitivity_score", 0)
                    )

                    # Also find result with ideal adjusted detection rate (10-30%)
                    ideal_results = [
                        r for r in self.results.values() if 10 <= r.get("adjusted_rate", 0) <= 30
                    ]
                    if ideal_results:
                        # Among ideal results, pick the one with best sensitivity score
                        recommended_result = max(
                            ideal_results, key=lambda x: x.get("sensitivity_score", 0)
                        )
                    else:
                        recommended_result = best_result

                self.broadcast_to_clients(
                    {
                        "type": "completed",
                        "message": "Calibration completed",
                        "results": (
                            {
                                "best": best_result,
                                "recommended": recommended_result,
                                "total_tests": len(self.results),
                            }
                            if best_result
                            else None
                        ),
                    }
                )
            else:
                self.broadcast_to_clients({"type": "status", "message": "Calibration stopped"})

        except Exception as e:
            logger.error(f"Calibration worker error: {e}")
            self.broadcast_to_clients({"type": "error", "message": str(e)})
        finally:
            with self._lock:
                self.running = False

    def start_calibration(self, samples: int = 8, delay_ms: int = 100):

        if isinstance(samples, bool) or not isinstance(samples, int) or not 1 <= samples <= 100:
            raise ValueError("samples must be an integer between 1 and 100")
        if isinstance(delay_ms, bool) or not isinstance(delay_ms, int) or not 0 <= delay_ms <= 10000:
            raise ValueError("delay must be an integer between 0 and 10000 milliseconds")
        radio = getattr(self.daemon_instance, "radio", None)
        if not callable(getattr(radio, "perform_cad", None)):
            raise RuntimeError("Radio does not support this CAD calibration; configure WM1303 CAD in the Manager")
        if self.event_loop is None or not self.event_loop.is_running():
            raise RuntimeError("Calibration event loop is not running")

        with self._lock:
            if self._closed:
                raise RuntimeError("Calibration is shutting down")
            if self.running or self._active_tasks or (self.calibration_thread and self.calibration_thread.is_alive()):
                return False
            self.running = True
            self._stop_event.clear()
            self.results.clear()
            self.progress = {"current": 0, "total": 0}
            self.clear_message_queue()
            self.calibration_thread = threading.Thread(
                target=self.calibration_worker, args=(samples, delay_ms), daemon=True
            )
            try:
                self.calibration_thread.start()
            except Exception:
                self.running = False
                self.calibration_thread = None
                raise

        return True

    def stop_calibration(self):

        with self._lock:
            self.running = False
            self._stop_event.set()
            future = self._active_future
            worker = self.calibration_thread
        if future is not None:
            future.cancel()
        if worker and worker is not threading.current_thread():
            worker.join(timeout=2)

    async def close(self):
        """Close admission and drain radio restoration on the owning loop."""
        with self._lock:
            self._closed = True
        await asyncio.to_thread(self.stop_calibration)
        with self._lock:
            tasks = tuple(self._active_tasks)
        if tasks:
            # Do not cancel again: a second cancellation could interrupt the
            # radio's restoration finally block after the first cancellation.
            await asyncio.gather(*tasks, return_exceptions=True)

    def clear_message_queue(self):

        with self._lock:
            self.message_queue.clear()
            self._message_generation += 1
