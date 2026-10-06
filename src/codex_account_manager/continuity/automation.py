"""Supervised continuation after a verified usage-limit handoff.

Claims are persisted before sending input. An uncertain delivery is never replayed.
No conversation content or model output is stored in the attempt journal.

This module does not bypass rate limits or manipulate quotas. It monitors for a
``usageLimitExceeded`` error and switches to a *separate, independently registered*
account. Users are responsible for complying with all applicable terms of service.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
from hashlib import sha256
from uuid import uuid4

from codex_account_manager.adapters.app_server import CodexAppServer
from codex_account_manager.adapters.interfaces import GoalInfo
from codex_account_manager.adapters.native_desktop import NativeDesktop, verified_desktop_turn
from codex_account_manager.adapters.native_ide import NativeIDE, OwnerSnapshot
from codex_account_manager.continuity.tracking import WorkTracker, goal_signature
from codex_account_manager.core.errors import (
    AppServerError,
    ConnectionNotReadyError,
    DesktopContinuationRequired,
    OwnerNotFoundError,
    TransactionError,
)
from codex_account_manager.core.events import bus
from codex_account_manager.core.logging import get_logger
from codex_account_manager.core.operation_lock import OperationLock
from codex_account_manager.core.paths import paths
from codex_account_manager.domain.states import GoalState
from codex_account_manager.goals.service import GoalService
from codex_account_manager.platform.ide_session import IDESession
from codex_account_manager.storage.database import connect
from codex_account_manager.storage.repositories import EventRepository, SettingsRepository

DESKTOP_OBSERVATION_SECONDS = 10.0
NATIVE_CONTINUATION_SECONDS = 180.0
log = get_logger(__name__)


@dataclass(frozen=True)
class ContinuationTicket:
    thread_id: str
    turn_id: str
    account_id: str | None = None
    goal_signature: str | None = None
    desktop: bool = False
    owner_id: str | None = None


def goal_allows_continuation(goal: GoalInfo | None) -> bool:
    if goal is None:
        return False
    if not goal.present:
        return True
    if goal.status not in {"active", "usageLimited"}:
        return False
    budget, used = goal.token_budget, goal.tokens_used
    if not isinstance(used, int) or isinstance(used, bool) or used < 0:
        return False
    return budget is None or (
        isinstance(budget, int) and not isinstance(budget, bool) and used < budget
    )


def has_verified_limit_turn(turn: dict | None, goal: GoalInfo | None) -> bool:
    if not turn or not goal_allows_continuation(goal):
        return False
    error = turn.get("error")
    if turn.get("status") == "failed":
        return isinstance(error, dict) and error.get("codexErrorInfo") == "usageLimitExceeded"
    return bool(
        turn.get("status") == "completed"
        and goal is not None
        and goal.present
        and goal.status == "usageLimited"
    )


class ContinuationSupervisor:
    def __init__(self, *, factory: Callable[[], CodexAppServer] | None = None):
        self.factory = factory or (
            lambda: CodexAppServer(paths.shared_codex_home, experimental=True)
        )
        self.native = NativeDesktop()
        self.ide = NativeIDE()
        self.ide_session = IDESession()
        self.tasks: set[asyncio.Task] = set()
        self.goals = GoalService()
        self.tracker = WorkTracker()
        self._account_readers = 0
        self._account_lock: OperationLock | None = None

    @contextmanager
    def _account_session(self) -> Iterator[None]:
        if self._account_readers == 0:
            lock = OperationLock(paths.data_dir / "account-operation.lock")
            lock.__enter__()
            self._account_lock = lock
        self._account_readers += 1
        try:
            yield
        finally:
            self._account_readers -= 1
            if self._account_readers == 0 and self._account_lock is not None:
                self._account_lock.__exit__(None, None, None)
                self._account_lock = None

    async def enabled(self) -> bool:
        return await SettingsRepository().get("auto_continue", "true") == "true"

    async def ide_enabled(self) -> bool:
        return await SettingsRepository().get("ide_continue", "false") == "true"

    async def desktop_enabled(self) -> bool:
        return await SettingsRepository().get("desktop_continue", "true") == "true"

    async def uses_ide(self, adapter: CodexAppServer, thread_id: str) -> bool:
        return await self.ide_enabled() and not await adapter.is_desktop_thread(thread_id)

    async def owner_turn(
        self, adapter: CodexAppServer, thread_id: str, *, wait: bool = False
    ) -> tuple[dict, OwnerSnapshot]:
        owner = (
            await self.ide.wait_inspect(thread_id) if wait else await self.ide.inspect(thread_id)
        )
        turn = owner.turn
        if turn.get("status") == "failed":
            stored = await adapter.latest_turn(thread_id)
            if not stored or stored.get("id") != turn.get("id") or stored.get("status") != "failed":
                raise AppServerError("The IDE failure does not match the stored turn.")
            turn = {**turn, "error": stored.get("error")}
        return turn, owner

    async def report(self, thread_id: str, state: str, stage: str) -> None:
        try:
            await EventRepository().append(
                "continuation.status", thread_id=thread_id, payload={"state": state, "stage": stage}
            )
        except Exception:
            log.warning("Could not persist continuation status (stage=%s).", stage)
        bus.publish("continuation.status", thread_id=thread_id, state=state, stage=stage)

    async def _local_allows(self, thread_id: str) -> bool:
        checkpoint = await self.goals.get(thread_id)
        if checkpoint is None:
            return True
        if checkpoint.user_cleared:
            return False
        # Reconciliation marks a native usage limit as blocked. Only that specific
        # account-limit state may be resumed; user pauses and goal budgets remain intact.
        return checkpoint.local_status in {
            GoalState.ACTIVE,
            GoalState.ACTIVE_AFTER_HANDOFF,
            GoalState.SWITCHING,
            GoalState.RESUMING,
        } or (
            checkpoint.local_status == GoalState.BLOCKED
            and checkpoint.native_goal_status == "usageLimited"
        )

    async def prepare(self, thread_id: str) -> ContinuationTicket | None:
        if not await self.enabled():
            await self.report(thread_id, "skipped", "disabled")
            return None
        if not await self._local_allows(thread_id):
            await self.report(thread_id, "needs_user", "checkpoint")
            return None
        adapter = self.factory()
        try:
            await adapter.start()
            desktop = False
            try:
                await adapter.require_headless_compatible(thread_id)
            except DesktopContinuationRequired:
                desktop = True
            owner_id = None
            ide_thread = desktop and not await adapter.is_desktop_thread(thread_id)
            if desktop and not (
                await self.ide_enabled() if ide_thread else await self.desktop_enabled()
            ):
                await self.report(thread_id, "skipped", "disabled")
                return None
            turn: dict | None
            if ide_thread:
                turn, owner = await self.owner_turn(adapter, thread_id)
                owner_id = owner.owner_id
                if owner.blocked:
                    await self.report(thread_id, "needs_user", "owner_busy")
                    return None
            else:
                turn = (
                    await verified_desktop_turn(self.native, adapter, thread_id)
                    if desktop
                    else await adapter.latest_turn(thread_id)
                )
            goal = await adapter.get_goal(thread_id)
            if goal is None:
                raise AppServerError("Could not verify the goal before preparing continuation.")
            if not turn or not has_verified_limit_turn(turn, goal):
                await self.report(thread_id, "skipped", "verification")
                return None
            return ContinuationTicket(
                thread_id,
                turn["id"],
                goal_signature=goal_signature(goal),
                desktop=desktop,
                owner_id=owner_id,
            )
        finally:
            await adapter.aclose()

    def launch_batch(self, tickets: list[ContinuationTicket]) -> None:
        lanes: dict[str, list[ContinuationTicket]] = {}
        for ticket in tickets:
            lane = "ide" if ticket.owner_id else "desktop" if ticket.desktop else ticket.thread_id
            lanes.setdefault(lane, []).append(ticket)

        async def run_lane(queued: list[ContinuationTicket]) -> None:
            waiting = set()
            try:
                for ticket in queued:
                    try:
                        if not await self.enabled():
                            break
                        if ticket.owner_id and not await self.ide_enabled():
                            await self.report(ticket.thread_id, "skipped", "disabled")
                            continue
                        if (
                            ticket.desktop
                            and not ticket.owner_id
                            and not await self.desktop_enabled()
                        ):
                            await self.report(ticket.thread_id, "skipped", "disabled")
                            continue
                        if ticket.desktop:
                            async with asyncio.timeout(NATIVE_CONTINUATION_SECONDS):
                                await self.run(ticket)
                        else:
                            await self.run(ticket)
                    except ConnectionNotReadyError:
                        waiting.add((ticket.thread_id, ticket.turn_id))
                        await self.report(
                            ticket.thread_id,
                            "waiting_connection",
                            "ide" if ticket.owner_id else "desktop",
                        )
                    except Exception:
                        await self.report(ticket.thread_id, "needs_user", "connection")
            except asyncio.CancelledError:
                waiting.clear()
                raise
            finally:
                await self.discard_pending(
                    [t for t in queued if (t.thread_id, t.turn_id) not in waiting]
                )

        async def run_all() -> None:
            results = await asyncio.gather(
                *(run_lane(queued) for queued in lanes.values()), return_exceptions=True
            )
            if any(isinstance(result, Exception) for result in results):
                log.warning("A continuation queue could not finish its bookkeeping.")

        task = asyncio.create_task(run_all())
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def save_pending(self, tickets: list[ContinuationTicket]) -> None:
        if not tickets:
            return
        async with connect() as db:
            await db.executemany(
                "INSERT OR REPLACE INTO pending_continuations VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        t.thread_id,
                        t.turn_id,
                        t.account_id,
                        t.goal_signature,
                        t.desktop,
                        t.owner_id,
                        time.time(),
                    )
                    for t in tickets
                ],
            )
            await db.commit()

    async def discard_pending(self, tickets: list[ContinuationTicket]) -> None:
        if not tickets:
            return
        async with connect() as db:
            await db.executemany(
                "DELETE FROM pending_continuations WHERE thread_id=? AND turn_id=?",
                [(t.thread_id, t.turn_id) for t in tickets],
            )
            await db.commit()

    async def recover_pending(self, account_id: str | None) -> None:
        if self.tasks:
            return
        async with connect() as db:
            rows = await (
                await db.execute("SELECT * FROM pending_continuations ORDER BY created_at")
            ).fetchall()
        if not rows:
            return
        enabled = await self.enabled()
        ready = []
        for row in rows:
            ticket = ContinuationTicket(
                row[0], row[1], row[2], row[3], desktop=bool(row[4]), owner_id=row[5]
            )
            allowed = enabled and (
                await self.ide_enabled()
                if ticket.owner_id
                else await self.desktop_enabled()
                if ticket.desktop
                else True
            )
            if allowed and account_id is None and 0 <= time.time() - row[6] < 600:
                continue
            if (
                allowed
                and account_id
                and ticket.account_id == account_id
                and 0 <= time.time() - row[6] < 600
            ):
                ready.append(ticket)
            else:
                await self.report(ticket.thread_id, "needs_user", "recovery")
                await self.discard_pending([ticket])
        if ready:
            self.launch_batch(ready)

    async def stop(self) -> None:
        tasks = tuple(self.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        async with connect() as db:
            await db.execute("DELETE FROM pending_continuations")
            await db.commit()

    async def _claim(self, ticket: ContinuationTicket) -> str | None:
        message_id = str(uuid4())
        async with connect() as db:
            cursor = await db.execute(
                "INSERT OR IGNORE INTO continuation_attempts VALUES (?, ?, ?, 'sending')",
                (ticket.thread_id, ticket.turn_id, message_id),
            )
            await db.commit()
            return message_id if cursor.rowcount == 1 else None

    async def _record(self, ticket: ContinuationTicket, status: str) -> None:
        async with connect() as db:
            await db.execute(
                "UPDATE continuation_attempts SET status=? WHERE thread_id=? AND source_turn_id=?",
                (status, ticket.thread_id, ticket.turn_id),
            )
            await db.commit()

    async def run(self, ticket: ContinuationTicket) -> None:
        if ticket.desktop:
            await self._run_desktop(ticket)
            return
        adapter = self.factory()
        claimed: ContinuationTicket | None = None
        key = sha256(ticket.thread_id.encode()).hexdigest()
        loop = asyncio.get_running_loop()
        task = asyncio.current_task()

        def stop_requested(event) -> None:
            if task and event.payload.get("scope") not in {"ide", "desktop"}:
                loop.call_soon_threadsafe(task.cancel)

        unsubscribe = bus.subscribe("continuation.stop", stop_requested)
        locks = ExitStack()
        stage = "connection"
        try:
            locks.enter_context(
                OperationLock(paths.data_dir / "locks" / f"continuation-{key}.lock")
            )
            locks.enter_context(self._account_session())
            await adapter.start()
            stage = "account"
            account_id = ticket.account_id
            if not account_id or (await adapter.read_account()).account_id != account_id:
                raise AppServerError("The active account changed before continuation.")
            stage = "conversation"
            await adapter.resume_for_continuation(ticket.thread_id)
            while await self.enabled():
                stage = "verification"
                if not await self._local_allows(ticket.thread_id):
                    break
                latest = await adapter.latest_turn(ticket.thread_id)
                if (
                    not latest
                    or latest["id"] != ticket.turn_id
                    or latest.get("status") not in {"failed", "completed"}
                ):
                    raise AppServerError(
                        "Conversation changed; automatic continuation was skipped."
                    )
                stage = "goal"
                goal = await adapter.get_goal(ticket.thread_id)
                if goal is None:
                    raise AppServerError("Could not verify the goal before continuation.")
                if ticket.goal_signature and goal_signature(goal) != ticket.goal_signature:
                    raise AppServerError("The goal changed after the interruption was recorded.")
                if not goal_allows_continuation(goal):
                    break
                stage = "journal"
                message_id = await self._claim(ticket)
                if message_id is None:
                    raise AppServerError(
                        "This continuation was already attempted. Inspect the conversation before retrying."
                    )
                claimed = ticket
                if goal and goal.status == "usageLimited":
                    stage = "goal"
                    await adapter.reactivate_usage_limited_goal(ticket.thread_id, goal)
                    await self.goals.resumed_usage_limited_goal(ticket.thread_id)
                    goal = replace(goal, status="active")
                bus.publish("continuation.status", state="running")
                stage = "execution"

                async def started(
                    turn_id: str, thread_id: str = ticket.thread_id, observed_goal: GoalInfo = goal
                ) -> None:
                    await self.tracker.observe(
                        account_id,
                        thread_id,
                        {"id": turn_id, "status": "inProgress"},
                        observed_goal,
                    )
                    bus.publish("work.observed")

                completed = await adapter.run_continuation_turn(
                    ticket.thread_id,
                    message_id,
                    on_started=started,
                )
                status = completed.get("status")
                if status not in {"completed", "failed", "interrupted"}:
                    raise AppServerError("Codex returned an invalid turn result.")
                await self._record(ticket, str(status))
                claimed = None
                current_goal = await adapter.get_goal(ticket.thread_id)
                if current_goal is not None or status == "interrupted":
                    await self.tracker.observe(
                        account_id,
                        ticket.thread_id,
                        completed,
                        current_goal,
                        limited=has_verified_limit_turn(completed, current_goal),
                    )
                    bus.publish("work.observed")
                if status == "interrupted":
                    await self.report(ticket.thread_id, "stopped", stage)
                    return
                if status != "completed":
                    # The watcher can perform another handoff if the new account
                    # is actually limited. Other failures require user attention.
                    await self.report(ticket.thread_id, "needs_user", stage)
                    return
                stage = "goal"
                goal = current_goal
                if goal is None:
                    raise AppServerError("Could not verify the goal after continuation.")
                if not goal.present or goal.status in {"complete", "completed"}:
                    if goal.present:
                        await self.goals.mark_status(ticket.thread_id, GoalState.COMPLETE)
                    await self.report(ticket.thread_id, "completed", stage)
                    return
                if goal.status != "active" or not goal_allows_continuation(goal):
                    break
                ticket = ContinuationTicket(
                    ticket.thread_id, completed["id"], ticket.account_id, ticket.goal_signature
                )
                # Re-check user settings and persisted turn state before every turn.
                await asyncio.sleep(0)
            await self.report(ticket.thread_id, "stopped", stage)
        except asyncio.CancelledError:
            if claimed:
                await self._record(claimed, "interrupted")
            await self.report(ticket.thread_id, "stopped", stage)
            raise
        except Exception:
            if claimed:
                await self._record(claimed, "uncertain")
            # Server output may contain conversation content: only publish a safe status.
            await self.report(ticket.thread_id, "needs_user", stage)
        finally:
            unsubscribe()
            try:
                await adapter.aclose()
            finally:
                locks.close()

    async def _run_desktop(self, ticket: ContinuationTicket) -> None:
        adapter = self.factory()
        claimed = False
        owner_snapshot: OwnerSnapshot | None = None
        verified_owner: str | None = None
        key = sha256(ticket.thread_id.encode()).hexdigest()
        task = asyncio.current_task()
        loop = asyncio.get_running_loop()

        async def latest_turn(*, wait: bool = False) -> dict:
            nonlocal owner_snapshot, verified_owner
            if ticket.owner_id:
                if not await self.ide_enabled():
                    raise AppServerError("IDE continuation is disabled.")
                turn, owner_snapshot = await self.owner_turn(adapter, ticket.thread_id, wait=wait)
                if verified_owner is not None and owner_snapshot.owner_id != verified_owner:
                    raise AppServerError("The IDE conversation owner changed.")
                verified_owner = owner_snapshot.owner_id
                return turn
            if not await self.desktop_enabled():
                raise AppServerError("Desktop continuation is disabled.")
            return await verified_desktop_turn(self.native, adapter, ticket.thread_id, wait=wait)

        def stop_requested(event) -> None:
            scope = event.payload.get("scope")
            if task and (
                scope not in {"ide", "desktop"}
                or (scope == "ide" and ticket.owner_id)
                or (scope == "desktop" and not ticket.owner_id)
            ):
                loop.call_soon_threadsafe(task.cancel)

        unsubscribe = bus.subscribe("continuation.stop", stop_requested)
        locks = ExitStack()
        stage = "connection"
        try:
            locks.enter_context(
                OperationLock(paths.data_dir / "locks" / f"continuation-{key}.lock")
            )
            locks.enter_context(self._account_session())
            await adapter.start()
            if not await self.enabled() or not await self._local_allows(ticket.thread_id):
                await self.report(ticket.thread_id, "skipped", "checkpoint")
                return
            stage = "account"
            if (
                not ticket.account_id
                or (await adapter.read_account()).account_id != ticket.account_id
            ):
                raise AppServerError("The active account changed before Desktop continuation.")
            stage = "ide" if ticket.owner_id else "desktop"
            latest = await latest_turn(wait=True)
            stage = "verification"
            goal = await adapter.get_goal(ticket.thread_id)
            if (
                latest.get("id") != ticket.turn_id
                or not has_verified_limit_turn(latest, goal)
                or goal is None
                or goal_signature(goal) != ticket.goal_signature
            ):
                raise AppServerError("The Desktop interruption or goal changed.")
            if ticket.owner_id and await SettingsRepository().get("ide_refresh", "false") == "true":
                stage = "ide_refresh"
                threads = await adapter.list_threads(include_subagents=False)
                workspace = next((t.cwd for t in threads if t.id == ticket.thread_id), None)
                if not workspace:
                    raise AppServerError("IDE refresh requires an existing local workspace.")
                if await self.ide_session.needs_refresh(ticket.account_id):
                    for thread in threads:
                        if thread.source != "vscode" or await adapter.is_desktop_thread(thread.id):
                            continue
                        try:
                            owner = await self.ide.inspect(thread.id)
                        except OwnerNotFoundError:
                            continue
                        if owner.blocked or owner.turn.get("status") == "inProgress":
                            raise AppServerError(
                                "IDE refresh is waiting for other conversations or approvals."
                            )
                await self.report(ticket.thread_id, "refreshing_ide", stage)
                await self.ide_session.refresh(ticket.account_id, ticket.thread_id, workspace)
                verified_owner = None
                stage = "ide"
                latest = await latest_turn(wait=True)
                if latest.get("id") != ticket.turn_id or not has_verified_limit_turn(latest, goal):
                    raise AppServerError("The IDE conversation changed during refresh.")
            # Re-read just before the durable claim; never steer a known running turn.
            current = await latest_turn()
            if current.get("id") != ticket.turn_id or current.get("status") != latest.get("status"):
                raise AppServerError("The Desktop conversation is no longer idle.")
            if not await self.enabled() or not await self._local_allows(ticket.thread_id):
                return
            if (await adapter.read_account()).account_id != ticket.account_id:
                raise AppServerError("The account changed during Desktop verification.")
            final_goal = await adapter.get_goal(ticket.thread_id)
            if (
                final_goal is None
                or not has_verified_limit_turn(current, final_goal)
                or goal_signature(final_goal) != ticket.goal_signature
            ):
                raise AppServerError("The goal changed during Desktop verification.")
            stage = "owner_busy"
            if owner_snapshot and (
                owner_snapshot.blocked
                or (
                    final_goal.present
                    and (
                        not owner_snapshot.goal
                        or owner_snapshot.goal.get("status") != final_goal.status
                        or owner_snapshot.goal.get("objective") != final_goal.objective
                        or owner_snapshot.goal.get("tokenBudget") != final_goal.token_budget
                    )
                )
                or (not final_goal.present and owner_snapshot.goal is not None)
            ):
                raise AppServerError("The IDE goal or approval state changed.")
            stage = "journal"
            message_id = await self._claim(ticket)
            if message_id is None:
                raise AppServerError("Desktop continuation was already attempted.")
            claimed = True
            stage = "execution"
            if ticket.owner_id and owner_snapshot:
                await self.ide.send(ticket.thread_id, owner_snapshot, message_id)
            else:
                await self.native.send(ticket.thread_id, ticket.turn_id)
            await self._record(ticket, "submitted")
            claimed = False
            await self.report(
                ticket.thread_id,
                "ide_submitted" if ticket.owner_id else "desktop_submitted",
                "execution",
            )
            stage = "observation"
            # Bound post-send observation; preserve the submitted claim on timeout.
            async with asyncio.timeout(DESKTOP_OBSERVATION_SECONDS):
                for _ in range(10):
                    turn = await latest_turn()
                    if turn.get("id") != ticket.turn_id:
                        observed_goal = await adapter.get_goal(ticket.thread_id)
                        if observed_goal is not None:
                            await self.tracker.observe(
                                ticket.account_id,
                                ticket.thread_id,
                                turn,
                                observed_goal,
                                limited=has_verified_limit_turn(turn, observed_goal),
                            )
                            bus.publish("work.observed")
                        if turn.get("status") in {"failed", "interrupted"}:
                            error = turn.get("error")
                            if isinstance(error, dict) and re.search(
                                r"\b401\b", str(error.get("message", ""))
                            ):
                                stage = "authentication"
                            raise AppServerError(
                                "The submitted continuation did not remain active."
                            )
                        break
                    await asyncio.sleep(0.2)
                else:
                    raise AppServerError("No new turn was observed after continuation submission.")
        except asyncio.CancelledError:
            if claimed:
                await self._record(ticket, "uncertain")
            raise
        except TransactionError as exc:
            if not claimed and stage == "connection":
                raise ConnectionNotReadyError("An account operation is still in progress.") from exc
            if claimed:
                await self._record(ticket, "uncertain")
            await self.report(ticket.thread_id, "needs_user", stage)
        except ConnectionNotReadyError:
            if not claimed and stage in {"desktop", "ide", "verification"}:
                raise
            if claimed:
                await self._record(ticket, "uncertain")
            await self.report(ticket.thread_id, "needs_user", stage)
        except Exception as exc:
            if claimed:
                await self._record(ticket, "uncertain")
            if isinstance(exc, AppServerError) and re.search(r"\b401\b", str(exc)):
                stage = "authentication"
            log.warning(
                "Native continuation stopped (stage=%s, error=%s).", stage, type(exc).__name__
            )
            await self.report(ticket.thread_id, "needs_user", stage)
        finally:
            unsubscribe()
            try:
                await adapter.aclose()
            finally:
                locks.close()
