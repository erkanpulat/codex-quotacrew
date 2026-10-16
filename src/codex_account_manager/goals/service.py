"""Local goal checkpoints and conservative native-goal reconciliation."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import wraps
from hashlib import sha256
from typing import Any, ParamSpec, TypeVar, cast

from codex_account_manager.adapters.interfaces import AppServerAdapter
from codex_account_manager.core.operation_lock import OperationLock
from codex_account_manager.core.paths import paths
from codex_account_manager.domain.models import GoalCheckpoint
from codex_account_manager.domain.states import (
    TERMINAL_GOAL_STATES,
    GoalState,
)
from codex_account_manager.storage.repositories import EventRepository, GoalRepository


@dataclass(frozen=True)
class ReconcileResult:
    action: str
    goal: GoalCheckpoint | None
    detail: str


P = ParamSpec("P")
T = TypeVar("T")


def _goal_operation(
    method: Callable[P, Coroutine[Any, Any, T]],
) -> Callable[P, Coroutine[Any, Any, T]]:
    @wraps(method)
    async def guarded(*args: P.args, **kwargs: P.kwargs) -> T:
        thread_id = cast(str, args[1] if len(args) > 1 else kwargs["thread_id"])
        key = sha256(thread_id.encode()).hexdigest()
        with OperationLock(paths.data_dir / "locks" / f"goal-{key}.lock"):
            return await method(*args, **kwargs)

    return guarded


class GoalService:
    def __init__(
        self,
        goals: GoalRepository | None = None,
        events: EventRepository | None = None,
    ):
        self.goals = goals or GoalRepository()
        self.events = events or EventRepository()

    async def get(self, thread_id: str) -> GoalCheckpoint | None:
        return await self.goals.get_for_thread(thread_id)

    @_goal_operation
    async def set_objective(
        self, thread_id: str, objective: str, *, profile_id: str | None = None
    ) -> GoalCheckpoint:
        if not thread_id.strip() or not objective.strip() or len(objective) > 4000:
            raise ValueError("Provide a conversation ID and an objective of 1–4000 characters.")
        existing = await self.goals.get_for_thread(thread_id)
        revision = (existing.revision + 1) if existing else 1
        goal = GoalCheckpoint(
            thread_id=thread_id,
            objective=objective,
            local_status=GoalState.ACTIVE,
            profile_id=profile_id or (existing.profile_id if existing else None),
            revision=revision,
            user_cleared=False,
            created_at=existing.created_at if existing else datetime.now(UTC),
        )
        if existing:
            goal.id = existing.id
        await self.goals.upsert(goal)
        await self.events.append("goal.set", thread_id=thread_id, payload={"revision": revision})
        return goal

    @_goal_operation
    async def mark_status(self, thread_id: str, status: GoalState) -> GoalCheckpoint | None:
        return await self._mark_status(thread_id, status)

    async def _mark_status(self, thread_id: str, status: GoalState) -> GoalCheckpoint | None:
        goal = await self.goals.get_for_thread(thread_id)
        if not goal:
            return None
        if (
            goal.user_cleared
            or goal.local_status in TERMINAL_GOAL_STATES
            or goal.local_status in {GoalState.PAUSED, GoalState.BLOCKED}
        ):
            return goal
        goal.local_status = status
        goal.last_seen_at = datetime.now(UTC)
        await self.goals.upsert(goal)
        await self.events.append(
            "goal.status", thread_id=thread_id, payload={"status": status.value}
        )
        return goal

    @_goal_operation
    async def resumed_usage_limited_goal(self, thread_id: str) -> None:
        checkpoint = await self.goals.get_for_thread(thread_id)
        if (
            checkpoint
            and not checkpoint.user_cleared
            and checkpoint.local_status == GoalState.BLOCKED
            and checkpoint.native_goal_status == "usageLimited"
        ):
            checkpoint.local_status = GoalState.ACTIVE_AFTER_HANDOFF
            checkpoint.native_goal_status = "active"
            await self.goals.upsert(checkpoint)

    @_goal_operation
    async def user_clear(self, thread_id: str) -> None:
        """Record that the user intentionally cleared the goal."""
        goal = await self.goals.get_for_thread(thread_id)
        if not goal:
            return
        goal.user_cleared = True
        goal.local_status = GoalState.IDLE
        goal.revision += 1
        await self.goals.upsert(goal)
        await self.events.append("goal.user_cleared", thread_id=thread_id)

    @_goal_operation
    async def reconcile_after_resume(
        self, thread_id: str, adapter: AppServerAdapter
    ) -> ReconcileResult:
        """Reconcile the local note without recreating or changing a native goal."""
        checkpoint = await self.goals.get_for_thread(thread_id)
        native = await adapter.get_goal(thread_id)

        if checkpoint is None:
            return ReconcileResult("none", None, "No QuotaCrew goal checkpoint for this thread.")

        checkpoint.native_goal_present = native.present if native is not None else None
        checkpoint.native_goal_status = native.status if native else None
        checkpoint.last_seen_at = datetime.now(UTC)
        await self.goals.upsert(checkpoint)

        if checkpoint.local_status in TERMINAL_GOAL_STATES:
            return ReconcileResult("terminal", checkpoint, "Goal is terminal; not continuing.")

        if checkpoint.local_status == GoalState.BLOCKED:
            return ReconcileResult("suspended", checkpoint, "Goal is blocked; not restoring.")

        if checkpoint.local_status == GoalState.PAUSED:
            return ReconcileResult("paused", checkpoint, "Goal is paused; not restoring.")

        if checkpoint.user_cleared:
            return ReconcileResult(
                "skipped_user_cleared", checkpoint, "Goal was cleared by the user; not restoring."
            )

        native_present = bool(native and native.present)
        if (
            native_present
            and native is not None
            and native.status
            in {
                "complete",
                "completed",
                "failed",
                "paused",
                "blocked",
                "budgetLimited",
                "usageLimited",
            }
        ):
            checkpoint.local_status = (
                GoalState.COMPLETE
                if native.status == "completed"
                else GoalState.BLOCKED
                if native.status in {"budgetLimited", "usageLimited"}
                else GoalState(native.status)
            )
            await self.goals.upsert(checkpoint)
            return ReconcileResult("native_status", checkpoint, "Preserved native goal status.")
        if native_present:
            checkpoint.local_status = GoalState.ACTIVE_AFTER_HANDOFF
            checkpoint.last_handoff_at = datetime.now(UTC)
            await self.goals.upsert(checkpoint)
            return ReconcileResult("none", checkpoint, "Native goal is present; nothing to do.")

        checkpoint = await self._mark_status(thread_id, GoalState.NEEDS_USER)
        return ReconcileResult(
            "blocked",
            checkpoint,
            "Native goal could not be verified; the local note was left unchanged."
            if native is None
            else "No native goal is present. Review the goal in Codex; local notes do not recreate goals or reset their budgets.",
        )
