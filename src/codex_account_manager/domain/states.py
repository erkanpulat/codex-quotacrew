"""State enumerations for QuotaCrew."""

from __future__ import annotations

from enum import StrEnum


class QuotaState(StrEnum):
    AVAILABLE = "available"
    LIMITED_WITH_RESET = "limited_with_reset"
    LIMITED_NO_RESET = "limited_no_reset"
    UNKNOWN = "unknown"


class GoalState(StrEnum):
    """QuotaCrew's view of a goal's lifecycle."""

    IDLE = "idle"
    ACTIVE = "active"
    PAUSED = "paused"
    BLOCKED = "blocked"
    HANDOFF_PENDING = "handoff_pending"
    SWITCHING = "switching"
    RESUMING = "resuming"
    ACTIVE_AFTER_HANDOFF = "active_after_handoff"
    COMPLETE = "complete"
    FAILED = "failed"
    NEEDS_USER = "needs_user"


#: Terminal goal states — QuotaCrew must not auto-continue past these.
TERMINAL_GOAL_STATES = frozenset({GoalState.COMPLETE, GoalState.FAILED})
#: States that require a human before any automation proceeds.
USER_BLOCKING_GOAL_STATES = frozenset({GoalState.BLOCKED, GoalState.NEEDS_USER})


class TransactionStage(StrEnum):
    """Stages of the transactional auth switch. Recorded in the journal."""

    PREPARE = "prepare"
    STOP_DESKTOP = "stop_desktop"
    BACKUP_ACTIVE_AUTH = "backup_active_auth"
    WRITE_TEMP_AUTH = "write_temp_auth"
    ATOMIC_REPLACE = "atomic_replace"
    VERIFY_ACCOUNT = "verify_account"
    START_DESKTOP = "start_desktop"
    WAIT_READY = "wait_ready"
    RESUME_THREAD = "resume_thread"
    VERIFY_GOAL = "verify_goal"
    COMMIT = "commit"
    ROLLBACK = "rollback"


class SwitchPolicyKind(StrEnum):
    """How QuotaCrew is allowed to initiate account switches."""

    MANUAL = "manual"
    CONFIRM = "confirm"
    AVAILABILITY_FAILOVER = "availability_failover"
    SMART_QUOTA = "smart_quota"


class HandoffReason(StrEnum):
    MANUAL = "manual"
    USAGE_LIMITED = "usage_limited"
    AUTH_EXPIRED = "auth_expired"
    ACCOUNT_MISMATCH = "account_mismatch"
    USER_REQUEST = "user_request"
    RECOVERY = "recovery"
