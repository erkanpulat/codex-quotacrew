"""Bounded goal activation observation; no messages or Desktop restarts."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from contextlib import ExitStack
from hashlib import sha256
from typing import TYPE_CHECKING

from codex_account_manager.codex.status import normalize_turn_status
from codex_account_manager.continuity.tracking import goal_signature
from codex_account_manager.core.child_process import finish_cleanup
from codex_account_manager.core.errors import AppServerError, TransactionError
from codex_account_manager.core.events import bus
from codex_account_manager.core.logging import get_logger
from codex_account_manager.core.operation_lock import OperationLock
from codex_account_manager.core.paths import paths
from codex_account_manager.storage.database import connect
from codex_account_manager.storage.repositories import SettingsRepository

if TYPE_CHECKING:
    from codex_account_manager.continuity.automation import (
        ContinuationSupervisor,
        ContinuationTicket,
    )

OBSERVATION_SECONDS = 60.0
REOPEN_AFTER_SECONDS = 10.0
POLL_SECONDS = 3.0
log = get_logger(__name__)


class DesktopGoalObserver:
    def __init__(self, owner: ContinuationSupervisor):
        self.owner = owner
        self.tasks: dict[tuple[str, str], asyncio.Task] = {}
        self._stoppers: dict[tuple[str, str], Callable[[], None]] = {}

    async def state(self, ticket: ContinuationTicket) -> tuple[str, float] | None:
        async with connect() as db:
            row = await (
                await db.execute(
                    """SELECT attempt.status, pending.created_at
                    FROM continuation_attempts AS attempt
                    JOIN pending_continuations AS pending
                      ON pending.thread_id=attempt.thread_id AND pending.turn_id=attempt.source_turn_id
                    WHERE attempt.thread_id=? AND attempt.source_turn_id=?
                      AND pending.account_id=? AND pending.goal_signature=?
                      AND attempt.status IN ('goal_opened', 'goal_reopened')""",
                    (ticket.thread_id, ticket.turn_id, ticket.account_id, ticket.goal_signature),
                )
            ).fetchone()
        return (row[0], row[1]) if row else None

    async def opened(self, ticket: ContinuationTicket) -> None:
        async with connect() as db:
            await db.execute(
                "UPDATE continuation_attempts SET status='goal_opened' "
                "WHERE thread_id=? AND source_turn_id=?",
                (ticket.thread_id, ticket.turn_id),
            )
            await db.execute(
                "INSERT OR REPLACE INTO pending_continuations VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ticket.thread_id,
                    ticket.turn_id,
                    ticket.account_id,
                    ticket.goal_signature,
                    True,
                    None,
                    time.time(),
                    ticket.resume_active_goal,
                ),
            )
            await db.commit()

    def start(self, ticket: ContinuationTicket) -> None:
        key = (ticket.thread_id, ticket.turn_id)
        if key in self.tasks:
            return
        entered = False
        stopped = False
        loop = asyncio.get_running_loop()

        def stop() -> None:
            nonlocal stopped
            if stopped:
                return
            stopped = True
            if entered and not task.cancelling():
                loop.call_soon_threadsafe(task.cancel)

        def event_stop(event) -> None:
            if event.payload.get("scope") != "ide":
                stop()

        async def run() -> None:
            nonlocal entered
            entered = True
            if stopped:
                await self._finish(ticket, "goal_stopped", "stopped")
            else:
                await self._run(ticket)

        unsubscribe = bus.subscribe("continuation.stop", event_stop)
        task = asyncio.create_task(run())
        self.tasks[key] = task
        self._stoppers[key] = stop

        def done(_task) -> None:
            unsubscribe()
            self.tasks.pop(key, None)
            self._stoppers.pop(key, None)
            if not _task.cancelled() and _task.exception() is not None:
                log.warning("A goal observation could not finish its bookkeeping.")

        task.add_done_callback(done)

    async def stop(self) -> None:
        tasks = tuple(self.tasks.values())
        for stop in tuple(self._stoppers.values()):
            stop()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _finish(self, ticket: ContinuationTicket, status: str, event: str) -> None:
        async with connect() as db:
            updated = await db.execute(
                "UPDATE continuation_attempts SET status=? WHERE thread_id=? AND source_turn_id=? "
                "AND status IN ('goal_opened','goal_reopened') "
                "AND EXISTS (SELECT 1 FROM pending_continuations AS pending "
                "WHERE pending.thread_id=continuation_attempts.thread_id "
                "AND pending.turn_id=continuation_attempts.source_turn_id "
                "AND pending.account_id=? AND pending.goal_signature=?)",
                (
                    status,
                    ticket.thread_id,
                    ticket.turn_id,
                    ticket.account_id,
                    ticket.goal_signature,
                ),
            )
            if not updated.rowcount:
                return
            await db.execute(
                "DELETE FROM pending_continuations WHERE thread_id=? AND turn_id=? AND account_id=?",
                (ticket.thread_id, ticket.turn_id, ticket.account_id),
            )
            await db.commit()
        await self.owner.report(ticket.thread_id, event, "goal_activation")
        bus.publish("work.observed")

    async def _probe(self, ticket: ContinuationTicket, *, reopen: bool) -> str:
        from codex_account_manager.continuity.automation import (
            goal_allows_continuation,
            matches_desktop_interruption,
        )
        from codex_account_manager.platform.windows import open_conversation

        owner = self.owner
        adapter = owner.factory()
        key = sha256(ticket.thread_id.encode()).hexdigest()
        with ExitStack() as locks:
            locks.enter_context(
                OperationLock(paths.data_dir / "locks" / f"continuation-{key}.lock")
            )
            locks.enter_context(owner._account_session())
            try:
                await adapter.start()
                if (
                    not await owner.enabled()
                    or not await owner.desktop_enabled()
                    or await SettingsRepository().get("monitor_enabled", "true") != "true"
                    or not await owner._local_allows(ticket.thread_id)
                ):
                    return "stopped"
                account = await adapter.read_account()
                goal = await adapter.get_goal(ticket.thread_id)
                if goal is None:
                    raise AppServerError("Goal state could not be verified.")
                if (
                    account.account_id != ticket.account_id
                    or not goal.present
                    or goal.status != "active"
                    or not goal_allows_continuation(goal)
                    or goal_signature(goal) != ticket.goal_signature
                ):
                    return "stopped"
                turn = await adapter.latest_turn(ticket.thread_id)
                activity = await owner.native.thread_status(ticket.thread_id)
                if activity == "active":
                    if ticket.resume_active_goal and turn and turn.get("id") == ticket.turn_id:
                        turn = {**turn, "status": "inProgress"}
                    if (
                        turn
                        and (turn.get("id") != ticket.turn_id or ticket.resume_active_goal)
                        and turn.get("status") == "inProgress"
                    ):
                        await owner.tracker.observe(
                            ticket.account_id or "", ticket.thread_id, turn, goal
                        )
                    bus.publish("monitor.refresh_requested")
                    return "running"
                if not turn:
                    raise AppServerError("Goal continuation turn is unavailable.")
                if turn.get("id") != ticket.turn_id:
                    if normalize_turn_status(turn.get("status")) == "completed":
                        await owner.tracker.observe(
                            ticket.account_id or "", ticket.thread_id, turn, goal
                        )
                        return "completed"
                    return "stopped"
                if not matches_desktop_interruption(ticket, turn, goal):
                    return "stopped"
                if (
                    reopen
                    and not ticket.resume_active_goal
                    and activity == "idle"
                    and account.ordinary_usage_allowed is True
                ):
                    final_goal = await adapter.get_goal(ticket.thread_id)
                    if (
                        final_goal is None
                        or not goal_allows_continuation(final_goal)
                        or final_goal.status != "active"
                        or goal_signature(final_goal) != ticket.goal_signature
                        or not await owner._local_allows(ticket.thread_id)
                        or not await owner.enabled()
                        or not await owner.desktop_enabled()
                    ):
                        return "stopped"
                    async with connect() as db:
                        claimed = await db.execute(
                            "UPDATE continuation_attempts SET status='goal_reopened' "
                            "WHERE thread_id=? AND source_turn_id=? AND status='goal_opened' "
                            "AND EXISTS (SELECT 1 FROM pending_continuations AS pending "
                            "WHERE pending.thread_id=continuation_attempts.thread_id "
                            "AND pending.turn_id=continuation_attempts.source_turn_id "
                            "AND pending.account_id=? AND pending.goal_signature=? "
                            "AND pending.created_at BETWEEN ? AND ?)",
                            (
                                ticket.thread_id,
                                ticket.turn_id,
                                ticket.account_id,
                                ticket.goal_signature,
                                time.time() - OBSERVATION_SECONDS,
                                time.time(),
                            ),
                        )
                        await db.commit()
                    if claimed.rowcount:
                        await finish_cleanup(asyncio.to_thread(open_conversation, ticket.thread_id))
                        await owner.report(ticket.thread_id, "goal_reopened", "goal_activation")
                return "waiting"
            finally:
                await finish_cleanup(adapter.aclose())

    async def _run(self, ticket: ContinuationTicket) -> None:
        try:
            while state := await self.state(ticket):
                status, started = state
                elapsed = time.time() - started
                if elapsed < 0 or elapsed >= OBSERVATION_SECONDS:
                    break
                try:
                    async with asyncio.timeout(min(8.0, OBSERVATION_SECONDS - elapsed)):
                        result = await self._probe(
                            ticket,
                            reopen=status == "goal_opened" and elapsed >= REOPEN_AFTER_SECONDS,
                        )
                except (AppServerError, OSError, TimeoutError, TransactionError):
                    result = "waiting"
                if result != "waiting":
                    await self._finish(
                        ticket,
                        "goal_resumed" if result in {"running", "completed"} else "goal_stopped",
                        result,
                    )
                    return
                await asyncio.sleep(POLL_SECONDS)
            await self._finish(ticket, "goal_unconfirmed", "needs_user")
        except asyncio.CancelledError:
            await finish_cleanup(self._finish(ticket, "goal_stopped", "stopped"))
            raise
        except Exception:
            await self._finish(ticket, "goal_unconfirmed", "needs_user")
