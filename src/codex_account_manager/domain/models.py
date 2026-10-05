"""Domain models for QuotaCrew.

Pydantic models for persisted/serialised entities and plain dataclasses for
in-memory snapshots read from the Codex App Server. None of these ever hold
credential material.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import BaseModel, Field

from codex_account_manager.domain.states import GoalState, HandoffReason, QuotaState


def _now() -> datetime:
    return datetime.now(UTC)


def _uuid() -> str:
    return str(uuid4())


class Profile(BaseModel):
    id: str = Field(default_factory=_uuid)
    alias: str
    codex_home: str
    bound_account_id: str | None = None
    created_at: datetime = Field(default_factory=_now)


class ResetCredit(BaseModel):
    status: str
    reset_type: str
    granted_at: int
    expires_at: int | None = None
    title: str | None = None
    description: str | None = None


class ResetCredits(BaseModel):
    available_count: int = Field(ge=0, strict=True)
    credits: tuple[ResetCredit, ...] | None = None


@dataclass(frozen=True)
class AccountSnapshot:
    """A point-in-time read of an account via the App Server. No secrets."""

    account_id: str | None
    account_type: str | None
    email: str | None = field(repr=False)
    plan_type: str | None
    ordinary_usage_allowed: bool | None
    primary_used_percent: float | None
    primary_resets_at: int | None
    primary_window_minutes: int | None
    secondary_used_percent: float | None
    secondary_resets_at: int | None
    secondary_window_minutes: int | None
    rate_limit_reached_type: str | None = None
    has_credits: bool | None = None
    subscription_until: datetime | None = None
    subscription_checked_at: datetime | None = None
    reset_credits: ResetCredits | None = None


@dataclass(frozen=True)
class ProfileHealth:
    """Aggregated health for a profile shown on the dashboard."""

    alias: str
    profile_id: str
    plan_type: str | None
    primary_used_percent: float | None
    secondary_used_percent: float | None
    primary_resets_at: int | None
    secondary_resets_at: int | None
    ordinary_usage_allowed: bool | None
    auth_present: bool
    account_match: bool | None
    is_active: bool
    quota_state: QuotaState
    last_checked_at: datetime | None
    error: str | None = None
    subscription_until: datetime | None = None
    subscription_checked_at: datetime | None = None
    reset_credits: ResetCredits | None = None
    email: str | None = field(default=None, repr=False)
    stale: bool = False
    reauth_required: bool = False
    primary_window_minutes: int | None = None
    secondary_window_minutes: int | None = None


@dataclass(frozen=True)
class ThreadInfo:
    """A thread as reported by the App Server ``thread/list``."""

    id: str
    preview: str | None
    cwd: str | None
    path: str | None
    model: str | None
    reasoning_effort: str | None
    created_at: int | None
    updated_at: int | None
    recency_at: int | None
    status: str | None
    title: str | None = None
    source: str | None = None
    project_id: str | None = None
    model_provider: str | None = None


class ThreadRecord(BaseModel):
    """QuotaCrew's tracked continuity record for a thread."""

    id: str
    profile_id: str | None = None
    workspace: str | None = None
    cwd: str | None = None
    preview: str | None = None
    last_turn_id: str | None = None
    title: str | None = None
    source: str | None = None
    project_id: str | None = None
    model_provider: str | None = None
    last_seen_at: datetime = Field(default_factory=_now)
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class GoalCheckpoint(BaseModel):
    """QuotaCrew's own goal checkpoint, independent of native Codex goal.

    ``revision`` increments on every meaningful change so we can distinguish a
    goal the user intentionally cleared from one lost during a handoff.
    """

    id: str = Field(default_factory=_uuid)
    thread_id: str
    objective: str
    local_status: GoalState = GoalState.ACTIVE
    native_goal_status: str | None = None
    native_goal_present: bool | None = None
    workspace: str | None = None
    profile_id: str | None = None
    revision: int = 1
    user_cleared: bool = False
    created_at: datetime = Field(default_factory=_now)
    last_seen_at: datetime = Field(default_factory=_now)
    last_handoff_at: datetime | None = None


class HandoffRecord(BaseModel):
    """One transactional switch/handoff, for the explainable timeline."""

    id: str = Field(default_factory=_uuid)
    thread_id: str | None = None
    from_profile_id: str | None = None
    to_profile_id: str | None = None
    reason: HandoffReason = HandoffReason.MANUAL
    started_at: datetime = Field(default_factory=_now)
    finished_at: datetime | None = None
    success: bool | None = None
    rolled_back: bool = False
    final_stage: str | None = None
    detail: str | None = None
