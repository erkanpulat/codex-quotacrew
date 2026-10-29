"""Choose verified accounts according to the configured switching policy."""

from __future__ import annotations

import math
from dataclasses import dataclass

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
        available = [
            c
            for c in candidates
            if self._is_available(c)
            and c.ordinary_usage_allowed is True
            and automatic_exclusion_reason(c) is None
            and (current is None or c.profile_id != current.profile_id)
        ]
        if not available:
            return None
        # Prefer the profile with the most headroom (lowest peak usage).
        return min(available, key=lambda h: (_peak_usage(h), h.alias.casefold(), h.profile_id))


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
}


def resolve_policy(kind: SwitchPolicyKind | str) -> SwitchPolicy:
    if isinstance(kind, str):
        kind = SwitchPolicyKind(kind)
    return _POLICIES[kind]()
