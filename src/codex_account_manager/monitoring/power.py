"""Session-only shutdown plans with fresh evidence and a cancellable countdown."""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from codex_account_manager.accounts.service import AccountService
from codex_account_manager.adapters.app_server import CodexAppServer
from codex_account_manager.adapters.interfaces import GoalInfo
from codex_account_manager.adapters.native_desktop import NativeDesktop, verified_desktop_turn
from codex_account_manager.continuity.tracking import WorkTracker, goal_signature
from codex_account_manager.core.paths import paths
from codex_account_manager.domain.models import ProfileHealth
from codex_account_manager.domain.states import QuotaState


@dataclass(frozen=True)
class PowerEvidence:
    ready: bool
    reason: str
    identity: str = ""


MAX_COUNTDOWN_MINUTES = 1440
CONDITION_COUNTDOWN_SECONDS = 120


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
        or turn.get("status") != "completed"
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

    def observe(self, evidence: PowerEvidence, now: float) -> None:
        if self.target is None or self.target.mode == "timer":
            return
        if self.checked_at is None or now - self.checked_at > 30:
            self.deadline = None
        self.checked_at = now
        self.reason = evidence.reason
        if not evidence.ready or not evidence.identity:
            self.deadline = None
            self.identity = ""
        elif self.deadline is None or self.identity != evidence.identity:
            self.identity = evidence.identity
            self.deadline = now + CONDITION_COUNTDOWN_SECONDS

    def remaining(self, now: float) -> int | None:
        if self.target is not None and self.target.mode == "timer":
            return None if self.deadline is None else max(0, math.ceil(self.deadline - now))
        if self.checked_at is not None and now - self.checked_at > 30:
            self.deadline = None
            self.reason = "Waiting for a fresh check."
        return None if self.deadline is None else max(0, math.ceil(self.deadline - now))


class PowerChecks:
    def __init__(self, accounts: AccountService):
        self.accounts = accounts

    async def conversations(self) -> list[tuple[str, str]]:
        adapter = CodexAppServer(paths.shared_codex_home, experimental=True)
        try:
            async with asyncio.timeout(15):
                await adapter.start()
                threads = await adapter.list_threads(max_items=1000)
                tracked = {work.thread_id for work in await WorkTracker().visible()}
                return [
                    (thread.id, thread.title or thread.preview or thread.id[:12])
                    for thread in threads
                    if thread.id in tracked or thread.status not in {"idle", "notLoaded"}
                ]
        finally:
            await adapter.aclose()

    async def _turn(self, adapter: CodexAppServer, thread_id: str) -> dict | None:
        source = await adapter.thread_source(thread_id)
        if source == "vscode":
            from codex_account_manager.adapters.native_ide import NativeIDE
            from codex_account_manager.storage.repositories import SettingsRepository

            if await SettingsRepository().get(
                "ide_continue", "false"
            ) == "true" and not await adapter.is_desktop_thread(thread_id):
                state = await NativeIDE().inspect(thread_id)
                return {**state.turn, "blocked": state.blocked}
            return await verified_desktop_turn(NativeDesktop(), adapter, thread_id)
        if source in {"cli", "exec", "appServer"}:
            return await adapter.observed_turn(thread_id)
        return None

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
                    or turn.get("status") != "inProgress"
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
                # A full bounded list avoids treating a truncated history as proof of idle.
                threads = await adapter.list_threads(max_items=1001)
                if len(threads) >= 1001:
                    return PowerEvidence(False, "Too many conversations to verify safely.")
                ids = {work.thread_id for work in await WorkTracker().visible()}
                ids.update(t.id for t in threads if t.status not in {"idle", "notLoaded"})
                if target.thread_id:
                    ids.add(target.thread_id)
                if len(ids) > 20:
                    return PowerEvidence(False, "Too many conversations to verify safely.")
                selected_turn = None
                for thread_id in ids:
                    turn = await self._turn(adapter, thread_id)
                    if (
                        not turn
                        or turn.get("blocked")
                        or turn.get("status") not in {"completed", "failed", "interrupted"}
                    ):
                        return PowerEvidence(False, "Work is active or could not be verified.")
                    goal = await adapter.get_goal(thread_id)
                    if goal is None or (
                        target.mode == "work" and goal.present and goal.status == "active"
                    ):
                        return PowerEvidence(False, "Work is active or could not be verified.")
                    if thread_id == target.thread_id:
                        selected_turn = turn
                if target.mode == "work":
                    goal = await adapter.get_goal(target.thread_id or "")
                    if not work_finished(target, selected_turn, goal):
                        return PowerEvidence(False, "The selected work has not completed.")
                    return PowerEvidence(True, "Selected work completed.", target.thread_id or "")
                health = await self.accounts.all_health()
                if not all_accounts_limited(health, datetime.now(UTC)):
                    return PowerEvidence(False, "Accounts have capacity or need verification.")
                identity = "|".join(sorted(h.profile_id for h in health))
                return PowerEvidence(
                    True, "All saved accounts have verified usage limits.", identity
                )
        except Exception:
            return PowerEvidence(False, "Waiting for a fresh check.")
        finally:
            await adapter.aclose()
