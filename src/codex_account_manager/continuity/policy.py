"""Choose verified accounts according to the configured switching policy."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from codex_account_manager.domain.models import ProfileHealth
from codex_account_manager.domain.states import QuotaState, SwitchPolicyKind

# This is our conservative automatic-selection policy, not a model entitlement
# table. New/unknown plans need explicit support; users can still switch manually.
AUTOMATIC_PLANS = frozenset({"plus", "pro", "team", "business", "enterprise", "edu"})


def automatic_exclusion_reason(h: ProfileHealth) -> str | None:
    plan = h.plan_type.strip().casefold() if isinstance(h.plan_type, str) else ""
    if plan == "free":
        return (
            "Free plan: manual switching only; quota percentages do not prove model compatibility."
        )
    if plan not in AUTOMATIC_PLANS:
        return "Plan not verified for automatic selection. You can switch manually."
    percentages = (h.primary_used_percent, h.secondary_used_percent)
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 <= value <= 100
        for value in percentages
    ):
        return "Quota figures are incomplete or invalid. Automatic selection waits for verified limits."
    return None


@dataclass(frozen=True)
class SwitchDecision:
    should_switch: bool
    requires_confirmation: bool
    target: ProfileHealth | None
    reason: str


class SwitchPolicy:
    """Base policy: never switch automatically."""

    kind = SwitchPolicyKind.MANUAL

    def decide(
        self, current: ProfileHealth | None, candidates: list[ProfileHealth]
    ) -> SwitchDecision:
        return SwitchDecision(False, False, None, "Manual policy: no automatic switch.")

    @staticmethod
    def _is_available(h: ProfileHealth) -> bool:
        if not h.identity or not h.identity.owner_key:
            return False
        if h.error is not None or h.stale or h.reauth_required:
            return False
        if h.account_match is not True:
            return False
        if not h.auth_present:
            return False
        if h.quota_state in (QuotaState.LIMITED_WITH_RESET, QuotaState.LIMITED_NO_RESET):
            return False
        if h.ordinary_usage_allowed is False:
            return False
        return h.quota_state == QuotaState.AVAILABLE

    @staticmethod
    def _current_limited(current: ProfileHealth | None) -> bool:
        if current is None:
            return False
        return (
            current.reauth_required
            or current.ordinary_usage_allowed is False
            or current.quota_state
            in (
                QuotaState.LIMITED_WITH_RESET,
                QuotaState.LIMITED_NO_RESET,
            )
        )

    def _best_candidate(
        self, current: ProfileHealth | None, candidates: list[ProfileHealth]
    ) -> ProfileHealth | None:
        available = self._available_candidates(current, candidates)
        if not available:
            return None
        # Prefer the profile with the most headroom (lowest peak usage).
        return min(available, key=lambda h: (_peak_usage(h), h.alias.casefold(), h.profile_id))

    def _available_candidates(
        self, current: ProfileHealth | None, candidates: list[ProfileHealth]
    ) -> list[ProfileHealth]:
        return [
            c
            for c in candidates
            if self._is_available(c)
            and c.ordinary_usage_allowed is True
            and automatic_exclusion_reason(c) is None
            and (current is None or c.profile_id != current.profile_id)
        ]


class ConfirmPolicy(SwitchPolicy):
    """Propose a switch when the current profile is limited; wait for the user."""

    kind = SwitchPolicyKind.CONFIRM

    def decide(
        self, current: ProfileHealth | None, candidates: list[ProfileHealth]
    ) -> SwitchDecision:
        if not self._current_limited(current):
            return SwitchDecision(False, False, None, "Current profile still has capacity.")
        target = self._best_candidate(current, candidates)
        if target is None:
            return SwitchDecision(
                False, False, None, "No authorized, available profile to switch to."
            )
        return SwitchDecision(
            True, True, target, f"Current profile is usage-limited; suggest '{target.alias}'."
        )


class AvailabilityFailoverPolicy(SwitchPolicy):
    """Automatically fail over to an available authorized profile when limited."""

    kind = SwitchPolicyKind.AVAILABILITY_FAILOVER

    def decide(
        self, current: ProfileHealth | None, candidates: list[ProfileHealth]
    ) -> SwitchDecision:
        if not self._current_limited(current):
            return SwitchDecision(False, False, None, "Current profile still has capacity.")
        target = self._best_candidate(current, candidates)
        if target is None:
            return SwitchDecision(
                False, False, None, "No authorized, available profile to fail over to."
            )
        return SwitchDecision(
            True, False, target, f"Failing over to available profile '{target.alias}'."
        )


# A 15% reserve in each window reduces immediate re-switching. This is a
# conservative policy margin, not an estimate of per-task consumption.
SMART_MIN_HEADROOM = 15.0
# Heuristic priorities, not measured task consumption. The weekly weight can
# outweigh a moderate headroom gap; the smaller five-hour weight cannot eclipse it.
SMART_WEEKLY_URGENCY_HOURS = 24.0
SMART_WEEKLY_MAX_BONUS = 60.0
SMART_PRIMARY_MAX_BONUS = 8.0
PRIMARY_WINDOW_MINUTES = 5 * 60
WEEKLY_WINDOW_MINUTES = 7 * 24 * 60


def _reset_hours(
    reset: object, window_minutes: object, expected_minutes: int, now: float
) -> float | None:
    # App Server reports Unix seconds. Reject milliseconds, expired values and
    # incompatible window metadata instead of guessing units or window roles.
    if window_minutes is not None and (
        isinstance(window_minutes, bool) or window_minutes != expected_minutes
    ):
        return None
    if isinstance(reset, bool) or not isinstance(reset, (int, float)):
        return None
    try:
        if not math.isfinite(reset):
            return None
        seconds = reset - now
    except OverflowError:
        return None
    if not 0 < seconds <= expected_minutes * 60:
        return None
    return seconds / 3600


def _smart_score(h: ProfileHealth, primary_hours: float, weekly_hours: float) -> float:
    # Start with the limiting headroom. A weekly reset within 24 hours earns
    # up to 60 points, enough to prefer expiring capacity over preserving a
    # larger weekly reserve. The five-hour bonus is capped at 8 points so its
    # frequent resets cannot dominate the weekly decision.
    headroom = 100 - _peak_usage(h)
    weekly_bonus = SMART_WEEKLY_MAX_BONUS * max(0.0, 1 - weekly_hours / SMART_WEEKLY_URGENCY_HOURS)
    primary_bonus = SMART_PRIMARY_MAX_BONUS * max(
        0.0, 1 - primary_hours / (PRIMARY_WINDOW_MINUTES / 60)
    )
    return headroom + weekly_bonus + primary_bonus


class SmartQuotaPolicy(SwitchPolicy):
    """Fail over only when limited, balancing headroom and expiring quota."""

    kind = SwitchPolicyKind.SMART_QUOTA

    def __init__(self, clock: Callable[[], datetime] | None = None):
        self._clock = clock or (lambda: datetime.now(UTC))

    def decide(
        self, current: ProfileHealth | None, candidates: list[ProfileHealth]
    ) -> SwitchDecision:
        if not self._current_limited(current):
            return SwitchDecision(False, False, None, "Current profile still has capacity.")
        available = self._available_candidates(current, candidates)
        if not available:
            return SwitchDecision(
                False, False, None, "No authorized, available profile to fail over to."
            )
        enough = [h for h in available if 100 - _peak_usage(h) >= SMART_MIN_HEADROOM]
        pool = enough or available
        now = self._clock()
        # Naive datetimes have no reliable timezone; never interpret them as local time.
        timestamp = (
            now.timestamp() if now.tzinfo is not None and now.utcoffset() is not None else None
        )
        times = []
        for h in pool:
            primary = (
                _reset_hours(
                    h.primary_resets_at, h.primary_window_minutes, PRIMARY_WINDOW_MINUTES, timestamp
                )
                if timestamp is not None
                else None
            )
            weekly = (
                _reset_hours(
                    h.secondary_resets_at,
                    h.secondary_window_minutes,
                    WEEKLY_WINDOW_MINUTES,
                    timestamp,
                )
                if timestamp is not None
                else None
            )
            times.append((primary, weekly))
        if not enough or any(p is None or w is None for p, w in times):
            target = min(
                pool if enough else available,
                key=lambda h: (_peak_usage(h), h.alias.casefold(), h.profile_id),
            )
            reason = "Smart switching used available headroom because reset data or reserve was insufficient."
        else:

            def rank(item: tuple[ProfileHealth, tuple[float | None, float | None]]):
                health, (primary, weekly) = item
                assert primary is not None and weekly is not None
                return (
                    -_smart_score(health, primary, weekly),
                    _peak_usage(health),
                    health.alias.casefold(),
                    health.profile_id,
                )

            target = min(zip(pool, times, strict=True), key=rank)[0]
            reason = "Smart switching balanced available quota with upcoming resets."
        return SwitchDecision(True, False, target, reason)


def _peak_usage(h: ProfileHealth) -> float:
    # Unknown values must never win a headroom comparison as zero usage.
    return max(
        h.primary_used_percent if h.primary_used_percent is not None else math.inf,
        h.secondary_used_percent if h.secondary_used_percent is not None else math.inf,
    )


_POLICIES: dict[SwitchPolicyKind, type[SwitchPolicy]] = {
    SwitchPolicyKind.MANUAL: SwitchPolicy,
    SwitchPolicyKind.CONFIRM: ConfirmPolicy,
    SwitchPolicyKind.AVAILABILITY_FAILOVER: AvailabilityFailoverPolicy,
    SwitchPolicyKind.SMART_QUOTA: SmartQuotaPolicy,
}


def resolve_policy(kind: SwitchPolicyKind | str) -> SwitchPolicy:
    if isinstance(kind, str):
        kind = SwitchPolicyKind(kind)
    return _POLICIES[kind]()
