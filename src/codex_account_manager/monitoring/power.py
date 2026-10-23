"""Session-only shutdown plans with fresh evidence and a cancellable countdown."""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from codex_account_manager.accounts.service import AccountService
from codex_account_manager.adapters.app_server import CodexAppServer
from codex_account_manager.adapters.interfaces import GoalInfo
from codex_account_manager.adapters.native_desktop import NativeDesktop, verified_desktop_turn
from codex_account_manager.codex.status import normalize_turn_status
from codex_account_manager.continuity.scan import thread_revision
from codex_account_manager.continuity.tracking import WorkTracker, goal_signature
from codex_account_manager.core.errors import OwnerNotFoundError
from codex_account_manager.core.paths import paths
from codex_account_manager.domain.models import ProfileHealth
from codex_account_manager.domain.states import QuotaState


@dataclass(frozen=True)
class PowerEvidence:
    ready: bool
    reason: str
    identity: str = ""
    values: dict = field(default_factory=dict)


MAX_COUNTDOWN_MINUTES = 1440
CONDITION_COUNTDOWN_SECONDS = 120
EVIDENCE_MAX_AGE_SECONDS = 45
_TERMINAL_STATUSES = frozenset({"completed", "failed", "interrupted", "cancelled"})


@dataclass(frozen=True)
class PowerTarget:
    mode: Literal["limits", "work", "timer"]
    thread_id: str | None = None
    goal_signature: str | None = None
    turn_id: str | None = None


def all_accounts_limited(health: list[ProfileHealth], now: datetime) -> bool:
    return bool(health) and all(
        h.auth_present
        and h.account_match is True
        and not h.error
        and not h.stale
        and not h.reauth_required
        and any(
            minutes == 300 and used is not None and math.isfinite(used) and used >= 100
            for minutes, used in (
                (h.primary_window_minutes, h.primary_used_percent),
                (h.secondary_window_minutes, h.secondary_used_percent),
            )
        )
        and h.ordinary_usage_allowed is False
        and h.quota_state in {QuotaState.LIMITED_WITH_RESET, QuotaState.LIMITED_NO_RESET}
        and h.last_checked_at is not None
        and h.last_checked_at.tzinfo is not None
        and 0 <= (now - h.last_checked_at).total_seconds() <= 60
        for h in health
    )


def work_finished(target: PowerTarget, turn: dict | None, goal: GoalInfo | None) -> bool:
    if (
        not turn
        or normalize_turn_status(turn.get("status")) != "completed"
        or goal is None
        or goal.thread_id != target.thread_id
    ):
        return False
    if target.goal_signature is not None:
        return (
            goal.present
            and goal_signature(goal) == target.goal_signature
            and goal.status in {"complete", "completed"}
        )
    # Continuation creates a new turn in the same selected conversation.
    return not goal.present and bool(turn.get("id"))


class ShutdownPlan:
    """No timers, processes or persisted state; GUI owns its lifetime and execution."""

    def __init__(self, seconds: int = 120):
        self.seconds = 120
        self.target: PowerTarget | None = None
        self.deadline: float | None = None
        self.checked_at: float | None = None
        self.identity = ""
        self.generation = 0
        self.reason = "Shutdown is off."
        self.reason_values: dict = {}
        self.set_seconds(seconds)

    def set_seconds(self, seconds: int) -> None:
        if not isinstance(seconds, int) or not 60 <= seconds <= MAX_COUNTDOWN_MINUTES * 60:
            raise ValueError("Shutdown countdown must be between 1 and 1440 minutes.")
        if self.target is not None:
            raise ValueError("Cancel the current shutdown plan before changing its countdown.")
        self.seconds = seconds

    def arm(self, target: PowerTarget, *, now: float = 0.0) -> None:
        if target.mode not in {"limits", "work", "timer"} or (
            target.mode == "work" and (not target.thread_id or not target.turn_id)
        ):
            raise ValueError("Select a verified running conversation first.")
        self.cancel()
        self.target = target
        if target.mode == "timer":
            self.deadline = now + self.seconds
        self.reason = "Waiting for the selected condition."

    def cancel(self) -> None:
        self.generation += 1
        self.target = None
        self.deadline = self.checked_at = None
        self.identity = ""
        self.reason = "Shutdown is off."
        self.reason_values = {}

    def observe(self, evidence: PowerEvidence, now: float) -> None:
        if self.target is None or self.target.mode == "timer":
            return
        if self.checked_at is None or now - self.checked_at > EVIDENCE_MAX_AGE_SECONDS:
            self.deadline = None
        self.checked_at = now
        self.reason = evidence.reason
        self.reason_values = evidence.values
        if not evidence.ready or not evidence.identity:
            self.deadline = None
            self.identity = ""
        elif self.deadline is None or self.identity != evidence.identity:
            self.identity = evidence.identity
            self.deadline = now + CONDITION_COUNTDOWN_SECONDS

    def remaining(self, now: float) -> int | None:
        if self.target is not None and self.target.mode == "timer":
            return None if self.deadline is None else max(0, math.ceil(self.deadline - now))
        if self.checked_at is not None and now - self.checked_at > EVIDENCE_MAX_AGE_SECONDS:
            self.deadline = None
            self.reason = "Waiting for a fresh check."
        return None if self.deadline is None else max(0, math.ceil(self.deadline - now))


class PowerChecks:
    def __init__(self, accounts: AccountService):
        self.accounts = accounts
        self._idle: dict[str, tuple] = {}
        self._last_attempt: dict[str, float] = {}
        self._seen_turns: set[str] = set()

    @staticmethod
    def _may_be_running(status: str | None) -> bool:
        # Missing or unfamiliar status is not proof that a conversation is idle.
        # Shutdown must wait for fresh evidence instead of risking active work.
        return status is None or status not in _TERMINAL_STATUSES

    async def conversations(self) -> list[tuple[str, str]]:
        adapter = CodexAppServer(paths.shared_codex_home, experimental=True)
        try:
            async with asyncio.timeout(15):
                await adapter.start()
                threads = await adapter.list_threads(include_subagents=False)
                choices: dict[str, str] = {}
                pending = iter(threads)

                async def worker() -> None:
                    for thread in pending:
                        try:
                            async with asyncio.timeout(3):
                                turn = await self._turn(adapter, thread.id)
                            if (
                                turn
                                and not turn.get("blocked")
                                and normalize_turn_status(turn.get("status")) == "inProgress"
                            ):
                                choices[thread.id] = thread.title or thread.id[:12]
                        except Exception:
                            continue

                async with asyncio.TaskGroup() as group:
                    for _ in range(min(4, len(threads))):
                        group.create_task(worker())
                return [
                    (thread.id, choices[thread.id]) for thread in threads if thread.id in choices
                ]
        finally:
            await adapter.aclose()

    async def _turn(self, adapter: CodexAppServer, thread_id: str) -> dict | None:
        source = await adapter.thread_source(thread_id)
        if source == "vscode":
            from codex_account_manager.adapters.native_ide import NativeIDE

            if not await adapter.is_desktop_thread(thread_id):
                try:
                    state = await NativeIDE().inspect(thread_id)
                    return {**state.turn, "blocked": state.blocked}
                except OwnerNotFoundError:
                    turn = await adapter.verified_stored_turn(thread_id)
            else:
                turn = await verified_desktop_turn(
                    NativeDesktop(), adapter, thread_id, prefer_stored=True
                )
        elif source in {"cli", "exec", "appServer"}:
            turn = await adapter.observed_turn(thread_id)
        elif source.startswith("subAgent") and source != "subAgentGuardian":
            turn = await adapter.verified_stored_turn(thread_id)
        else:
            return None
        return turn if turn is not None else {"id": None, "status": "notStarted"}

    async def work_target(self, thread_id: str) -> PowerTarget:
        adapter = CodexAppServer(paths.shared_codex_home, experimental=True)
        try:
            async with asyncio.timeout(15):
                await adapter.start()
                turn = await self._turn(adapter, thread_id)
                goal = await adapter.get_goal(thread_id)
                if (
                    not turn
                    or turn.get("blocked")
                    or normalize_turn_status(turn.get("status")) != "inProgress"
                    or goal is None
                ):
                    raise ValueError("Select a verified running conversation first.")
                if goal.present and goal.status != "active":
                    raise ValueError("Select a verified running conversation first.")
                return PowerTarget(
                    "work",
                    thread_id,
                    goal_signature(goal) if goal.present else None,
                    turn["id"],
                )
        finally:
            await adapter.aclose()

    async def check(self, target: PowerTarget) -> PowerEvidence:
        if target.mode == "timer":
            raise ValueError("Timer plans do not use work or account checks.")
        adapter = CodexAppServer(paths.shared_codex_home, experimental=True)
        try:
            async with asyncio.timeout(20):
                await adapter.start()
                try:
                    threads = await adapter.list_threads()
                except Exception:
                    return PowerEvidence(
                        False, "Conversation discovery is incomplete. Shutdown will wait."
                    )
                listed = {thread.id: thread for thread in threads}
                self._idle = {key: value for key, value in self._idle.items() if key in listed}
                tracked = {work.thread_id for work in await WorkTracker().visible()}
                internal = {t.id for t in threads if t.source == "subAgentGuardian"}
                ids = (
                    tracked | {t.id for t in threads if self._may_be_running(t.status)}
                ) - internal
                if target.thread_id:
                    ids.add(target.thread_id)
                self._seen_turns.intersection_update(ids)
                self._seen_turns.update(tracked & ids)
                self._last_attempt = {
                    key: value for key, value in self._last_attempt.items() if key in ids
                }
                selected_turn = None
                running: set[str] = set()
                unavailable: set[str] = set()

                async def inspect(thread_id: str) -> None:
                    nonlocal selected_turn
                    thread = listed.get(thread_id)
                    revision = thread_revision(thread) if thread else None
                    if (
                        target.mode != "work"
                        and revision is not None
                        and revision[-1] is not None
                        and thread_id != target.thread_id
                        and thread_id not in tracked
                        and self._idle.get(thread_id) == revision
                    ):
                        return
                    try:
                        async with asyncio.timeout(3):
                            turn = await self._turn(adapter, thread_id)
                            if not turn or turn.get("blocked"):
                                unavailable.add(thread_id)
                                return
                            status = normalize_turn_status(turn.get("status"))
                            if status == "inProgress":
                                self._seen_turns.add(thread_id)
                                running.add(thread_id)
                                return
                            if status == "notStarted" and thread_id in self._seen_turns:
                                unavailable.add(thread_id)
                                return
                            if status not in {"completed", "failed", "interrupted", "notStarted"}:
                                unavailable.add(thread_id)
                                return
                            if status != "notStarted":
                                self._seen_turns.add(thread_id)
                            if target.mode == "work" and thread_id != target.thread_id:
                                goal = await adapter.get_goal(thread_id)
                                if goal is None:
                                    unavailable.add(thread_id)
                                    return
                                if goal.present and goal.status == "active":
                                    running.add(thread_id)
                                    return
                            if thread_id == target.thread_id:
                                selected_turn = turn
                            elif revision is not None:
                                self._idle[thread_id] = revision
                    except Exception:
                        unavailable.add(thread_id)

                def job_names(job_ids: set[str]) -> str:
                    return ", ".join(
                        (listed[tid].title or tid[:12])[:100] if tid in listed else tid[:12]
                        for tid in sorted(job_ids)[:3]
                    )

                pending_ids = iter(sorted(ids, key=lambda key: self._last_attempt.get(key, 0)))

                async def worker() -> None:
                    for thread_id in pending_ids:
                        self._last_attempt[thread_id] = time.monotonic()
                        await inspect(thread_id)

                async with asyncio.TaskGroup() as group:
                    for _ in range(min(4, len(ids))):
                        group.create_task(worker())
                if running:
                    return PowerEvidence(
                        False,
                        "Waiting for {count} running jobs or active goals. Examples: {jobs}.",
                        values={"count": len(running), "jobs": job_names(running)},
                    )
                if unavailable:
                    return PowerEvidence(
                        False,
                        "Could not verify {count} jobs. Examples: {jobs}. Shutdown is waiting; check these jobs in Codex.",
                        values={"count": len(unavailable), "jobs": job_names(unavailable)},
                    )
                from codex_account_manager.continuity.desktop_goal import OBSERVATION_SECONDS
                from codex_account_manager.storage.database import connect

                now = time.time()
                async with connect() as db:
                    activating = await (
                        await db.execute(
                            """SELECT COUNT(*) FROM pending_continuations AS pending
                            JOIN continuation_attempts AS attempt
                              ON attempt.thread_id=pending.thread_id
                             AND attempt.source_turn_id=pending.turn_id
                            WHERE pending.created_at BETWEEN ? AND ?
                              AND attempt.status IN ('goal_opened','goal_reopened')""",
                            (now - OBSERVATION_SECONDS, now),
                        )
                    ).fetchone()
                if activating and activating[0]:
                    return PowerEvidence(
                        False,
                        "Waiting for {count} goals to resume.",
                        values={"count": activating[0]},
                    )
                if target.mode == "work":
                    account_id = (await adapter.read_account()).account_id
                    now = time.time()
                    async with connect() as db:
                        pending = await (
                            await db.execute(
                                """SELECT COUNT(*) FROM pending_continuations AS pending
                            WHERE account_id=? AND created_at > ? AND created_at <= ?
                              AND NOT EXISTS (
                                SELECT 1 FROM continuation_attempts AS attempt
                                WHERE attempt.thread_id=pending.thread_id
                                  AND attempt.source_turn_id=pending.turn_id
                              )""",
                                (account_id, now - 600, now),
                            )
                        ).fetchone()
                    if pending and pending[0]:
                        return PowerEvidence(
                            False,
                            "Waiting for {count} queued continuations.",
                            values={"count": pending[0]},
                        )
                    goal = await adapter.get_goal(target.thread_id or "")
                    if not work_finished(target, selected_turn, goal):
                        if goal is None or goal.thread_id != target.thread_id:
                            return PowerEvidence(
                                False,
                                "The selected goal could not be verified. Shutdown is waiting for a fresh check.",
                            )
                        if target.goal_signature is not None and not goal.present:
                            return PowerEvidence(
                                False,
                                "The selected goal is no longer present. Cancel this plan and select the work again.",
                            )
                        if goal.present:
                            if goal_signature(goal) != target.goal_signature:
                                return PowerEvidence(
                                    False,
                                    "The selected goal changed. Cancel this plan and select the work again.",
                                )
                            reasons = {
                                "blocked": "The selected goal is blocked. Open the conversation in Codex to review what it needs.",
                                "paused": "The selected goal is paused. Shutdown will wait for it to complete.",
                                "usageLimited": "The selected goal reached its usage limit. Shutdown will wait for it to resume and complete.",
                                "active": "The selected goal is still active. Shutdown will wait for it to complete.",
                            }
                            if goal.status in reasons:
                                return PowerEvidence(False, reasons[goal.status])
                        return PowerEvidence(
                            False,
                            "The selected turn has not completed successfully. Shutdown is waiting.",
                        )
                    return PowerEvidence(True, "Selected work completed.", target.thread_id or "")
                health = await self.accounts.all_health()
                if not all_accounts_limited(health, datetime.now(UTC)):
                    available = sum(
                        h.ordinary_usage_allowed is True and not h.stale and not h.error
                        for h in health
                    )
                    if available:
                        return PowerEvidence(
                            False,
                            "{count} accounts still have capacity. Shutdown requires exhausted five-hour quotas and no running work.",
                            values={"count": available},
                        )
                    return PowerEvidence(
                        False,
                        "Not all five-hour quotas are confirmed exhausted. Check Accounts for missing or expired sign-ins.",
                    )
                identity = "|".join(sorted(h.profile_id for h in health))
                return PowerEvidence(
                    True, "All saved accounts have verified usage limits.", identity
                )
        except Exception:
            return PowerEvidence(False, "Waiting for a fresh check.")
        finally:
            await adapter.aclose()
