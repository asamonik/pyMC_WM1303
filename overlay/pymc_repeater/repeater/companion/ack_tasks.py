"""Own delayed text ACK work before the core's compatibility task can run."""

import asyncio
import logging

logger = logging.getLogger(__name__)


async def _wait_for_owned_ack(task):
    # The core still creates a task from the returned coroutine. Cancelling
    # that thin compatibility waiter must not orphan/cancel the actual ACK.
    return await asyncio.shield(task)


def install_owned_text_acks(bridge):
    handler = bridge._get_text_handler()

    async def send_delayed_ack(packet, delay_s, timestamp):
        await asyncio.sleep(delay_s)
        if bridge._stop_task is not None:
            logger.debug("Skipping delayed companion ACK while stopping")
            return False
        try:
            # Preserve the handler's injector and caller-supplied firmware
            # delay. The daemon injector also rejects TX as soon as shutdown
            # begins, which precedes the later bridge-drain phase.
            sent = await handler.send_packet(packet, wait_for_ack=False)
        except Exception as exc:
            logger.debug("Delayed companion ACK failed (%s)", type(exc).__name__)
            return False
        if sent:
            logger.debug("Delayed companion ACK accepted for timestamp %s", timestamp)
        else:
            logger.debug("Delayed companion ACK was not accepted for transmission")
        return bool(sent)

    def schedule_delayed_ack(packet, delay_s, timestamp):
        # This must be synchronous: the core calls this function before its
        # bare create_task. Tracking only when that task starts leaves a
        # queued-but-not-yet-running ACK invisible to bridge shutdown.
        work = send_delayed_ack(packet, delay_s, timestamp)
        try:
            task = bridge._spawn_background_task(work, "delayed text ACK")
        except RuntimeError:
            work.close()
            raise
        return _wait_for_owned_ack(task)

    handler._send_delayed_ack = schedule_delayed_ack
