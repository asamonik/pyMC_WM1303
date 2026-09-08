"""Task-owned, bounded replies around the retained companion command handlers."""

import asyncio
import logging

from openhop_core.companion.constants import MAX_PAYLOAD_SIZE

logger = logging.getLogger(__name__)


class CommandRepliesMixin:
    def _command_reply_writer_is_current(self, writer):
        return (writer is not None and self._client_writer is writer
                and not self._closing and not writer.is_closing())

    def _abort_command_reply_capture(self, capture):
        capture["failed"] = True
        capture["frames"].clear()
        logger.warning("Companion command reply exceeded its bounded frame capacity")
        writer = capture["writer"]
        if writer is not None:
            try:
                writer.transport.abort()
            except Exception as exc:
                logger.debug("Companion reply connection abort failed (%s)", type(exc).__name__)

    def _write_frame(self, data):
        try:
            task = asyncio.current_task()
        except RuntimeError:
            task = None
        capture = self._command_reply_captures.get(task)
        if capture is None:
            # Preserve legacy synchronous callers and unrelated RX pushes.
            return super()._write_frame(data)
        if capture["failed"]:
            return
        if (not isinstance(data, bytes) or len(data) > MAX_PAYLOAD_SIZE
                or len(capture["frames"]) >= 256):
            self._abort_command_reply_capture(capture)
            return
        capture["frames"].append(data)

    async def _flush_command_replies(self, capture):
        if capture["failed"]:
            return False
        writer = capture["writer"]
        for frame in capture["frames"]:
            if not self._command_reply_writer_is_current(writer):
                return False
            if not await self._enqueue_response_frame(frame):
                return False
        return self._command_reply_writer_is_current(writer)

    def _command_reply_capture_finished(self, capture):
        """Allow request owners to record admission before releasing waiters."""

    async def _run_command_reply_capture(self, coro, writer, *, parent_ready=None, state=None):
        task = asyncio.current_task()
        capture = {"writer": writer, "frames": [], "ready": asyncio.Event(),
                   "failed": False, "flushed": False}
        self._command_reply_captures[task] = capture
        started = False
        try:
            if parent_ready is not None:
                # A completion already available from the RF waiter must not
                # overtake the initiating command's SENT frame.
                await parent_ready.wait()
            started = True
            if state is not None:
                state["started"] = True
            # Stale connections still drain their already-owned request. Only
            # output is discarded; disconnect never cancels an RF waiter.
            result = await coro
            capture["flushed"] = await self._flush_command_replies(capture)
            return result
        finally:
            try:
                if not started:
                    coro.close()
                self._command_reply_capture_finished(capture)
            finally:
                capture["ready"].set()
                if self._command_reply_captures.get(task) is capture:
                    del self._command_reply_captures[task]

    async def _handle_cmd(self, payload):
        writer = self._client_writer
        if not self._command_reply_writer_is_current(writer):
            return
        await self._run_command_reply_capture(super()._handle_cmd(payload), writer)

    def _spawn_request_task(self, coro, label):
        # Only a task that owns a capture can supply its initial-reply barrier.
        # A global buffer or inherited ContextVar would mix sibling requests.
        parent = self._command_reply_captures.get(asyncio.current_task())
        writer = parent["writer"] if parent is not None else self._client_writer
        parent_ready = parent["ready"] if parent is not None else None
        state = {"started": False}
        work = self._run_command_reply_capture(
            coro, writer, parent_ready=parent_ready, state=state,
        )
        spawn = super()._spawn_request_task
        try:
            owned_task = spawn(work, f"{label} reply owner")
        except BaseException:
            work.close()
            coro.close()
            raise

        def close_unstarted(_task):
            # Task cancellation before its first step never enters the work
            # coroutine's finally block. Close its still-unawaited input too.
            if not state["started"]:
                coro.close()

        owned_task.add_done_callback(close_unstarted)

        async def wait_for_owned_reply():
            # Cancelling the compatibility task must not propagate through the
            # core completion coroutine into its already-owned RF request task.
            return await asyncio.shield(owned_task)

        waiter = wait_for_owned_reply()
        try:
            return spawn(waiter, label)
        except BaseException:
            waiter.close()
            # The inner work remains bridge-owned and will drain normally.
            raise
