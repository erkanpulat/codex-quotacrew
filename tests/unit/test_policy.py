from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone

import pytest

from codex_account_manager.continuity.policy import (
    AvailabilityFailoverPolicy,
    ConfirmPolicy,
    SmartQuotaPolicy,
    SwitchPolicy,
    resolve_policy,
)
from codex_account_manager.domain.models import ProfileHealth
from codex_account_manager.domain.states import QuotaState, SwitchPolicyKind
from tests.fakes import account_identity


def _health(
    alias,
    *,
    active=False,
    allowed=True,
    quota=QuotaState.AVAILABLE,
    match=True,
    primary=0.0,
    secondary=0.0,
):
    return ProfileHealth(
        alias=alias,
        profile_id=alias,
        identity=account_identity(alias),
        plan_type="plus",
        primary_used_percent=primary,
        secondary_used_percent=secondary,
        primary_resets_at=None,
        secondary_resets_at=None,
        ordinary_usage_allowed=allowed,
        auth_present=True,
        account_match=match,
        is_active=active,
        quota_state=quota,
        last_checked_at=datetime.now(UTC),
    )


def test_manual_never_switches():
    policy = SwitchPolicy()
    current = _health("a", active=True, allowed=False, quota=QuotaState.LIMITED_WITH_RESET)
    decision = policy.decide(current, [current, _health("b")])
    assert decision.should_switch is False


def test_confirm_suggests_when_limited():
    policy = ConfirmPolicy()
    current = _health("a", active=True, allowed=False, quota=QuotaState.LIMITED_WITH_RESET)
    other = _health("b", secondary=10.0)
    decision = policy.decide(current, [current, other])
    assert decision.should_switch is True
    assert decision.requires_confirmation is True
    assert decision.target.alias == "b"


def test_failover_switches_without_confirmation():
    policy = AvailabilityFailoverPolicy()
    current = _health("a", active=True, allowed=False, quota=QuotaState.LIMITED_NO_RESET)
    good = _health("b", secondary=5.0)
    busy = _health("c", secondary=95.0, allowed=False, quota=QuotaState.LIMITED_WITH_RESET)
    decision = policy.decide(current, [current, busy, good])
    assert decision.should_switch is True
    assert decision.requires_confirmation is False
    assert decision.target.alias == "b"  # most headroom


def test_failover_no_target_when_all_limited():
    policy = AvailabilityFailoverPolicy()
    current = _health("a", active=True, allowed=False, quota=QuotaState.LIMITED_WITH_RESET)
    other = _health("b", allowed=False, quota=QuotaState.LIMITED_WITH_RESET)
    decision = policy.decide(current, [current, other])
    assert decision.should_switch is False


def test_resolve_policy_by_kind():
    assert isinstance(resolve_policy(SwitchPolicyKind.MANUAL), SwitchPolicy)
    assert isinstance(resolve_policy("confirm"), ConfirmPolicy)
    assert isinstance(resolve_policy("availability_failover"), AvailabilityFailoverPolicy)


@pytest.mark.parametrize(
    "policy", [ConfirmPolicy(), AvailabilityFailoverPolicy(), SmartQuotaPolicy()]
)
@pytest.mark.parametrize(
    "changes",
    [
        {"plan_type": "free"},
        {"plan_type": " FREE "},
        {"plan_type": None},
        {"plan_type": "new-unverified-plan"},
        {"primary_used_percent": None},
        {"secondary_used_percent": None},
        {"primary_used_percent": float("nan")},
        {"secondary_used_percent": float("inf")},
        {"primary_used_percent": -1},
        {"secondary_used_percent": 101},
        {"ordinary_usage_allowed": None},
        {"primary_used_percent": True},
    ],
)
def test_automatic_and_suggested_targets_reject_free_or_unknown_capacity(policy, changes):
    current = _health("current", active=True, allowed=False, quota=QuotaState.LIMITED_NO_RESET)
    uncertain = replace(_health("apparently-empty"), **changes)
    paid = _health("verified-paid", primary=60, secondary=80)
    assert policy.decide(current, [uncertain, paid]).target == paid
    assert not policy.decide(current, [uncertain]).should_switch


def test_free_remains_manually_usable_and_actual_zero_usage_remains_valid():
    free = replace(_health("free"), plan_type="free")
    assert SwitchPolicy._is_available(free)
    paid_zero = _health("paid-zero")
    assert SwitchPolicy()._best_candidate(None, [free, paid_zero]) == paid_zero


NOW = datetime(2026, 1, 1, tzinfo=UTC)


def smart_health(alias, primary_remaining, weekly_remaining, weekly_hours, primary_hours=3):
    return replace(
        _health(alias, primary=100 - primary_remaining, secondary=100 - weekly_remaining),
        primary_resets_at=int((NOW + timedelta(hours=primary_hours)).timestamp()),
        secondary_resets_at=int((NOW + timedelta(hours=weekly_hours)).timestamp()),
        primary_window_minutes=300,
        secondary_window_minutes=10080,
    )


def smart_decision(*candidates, clock=NOW):
    current = _health("current", active=True, allowed=False, quota=QuotaState.LIMITED_NO_RESET)
    return SmartQuotaPolicy(clock=lambda: clock).decide(current, list(candidates))


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ((75, 20, 2), (75, 60, 72), "a"),
        ((75, 1, 1), (75, 60, 72), "b"),
        ((3, 40, 2), (70, 50, 48), "b"),
        ((75, 35, 50), (75, 60, 51), "b"),
    ],
)
def test_smart_selection_scenarios(a, b, expected):
    assert smart_decision(smart_health("a", *a), smart_health("b", *b)).target.alias == expected


@pytest.mark.parametrize(
    "bad_reset",
    [
        None,
        0,
        int(NOW.timestamp()),
        int(NOW.timestamp()) - 1,
        int(NOW.timestamp()) * 1000,
        float("nan"),
        10**400,
        True,
    ],
)
def test_smart_missing_or_invalid_time_falls_back_to_headroom(bad_reset):
    a = replace(smart_health("a", 75, 20, 2), secondary_resets_at=bad_reset)
    b = smart_health("b", 75, 60, 72)
    decision = smart_decision(a, b)
    assert decision.target == b
    assert "headroom" in decision.reason


def test_smart_reserve_fallback_and_equal_boundary():
    a = smart_health("a", 90, 14, 1)
    b = smart_health("b", 90, 10, 72)
    assert smart_decision(a, b).target == a
    eligible = smart_health("eligible", 15, 15, 2)
    assert smart_decision(a, eligible).target == eligible


def test_smart_no_switch_on_limited_or_active_current():
    policy = SmartQuotaPolicy(clock=lambda: NOW)
    limited = _health("limited", active=True, allowed=False, quota=QuotaState.LIMITED_NO_RESET)
    other_limited = _health("other", allowed=False, quota=QuotaState.LIMITED_NO_RESET)
    assert not policy.decide(limited, [limited, other_limited]).should_switch
    assert not policy.decide(
        _health("current", active=True), [smart_health("a", 75, 20, 1)]
    ).should_switch


def test_smart_determinism_timezone_and_window_validation():
    a = smart_health("a", 75, 50, 2)
    b = replace(a, alias="b", profile_id="b")
    for candidates in ([a, b], [b, a]):
        assert (
            smart_decision(*candidates, clock=NOW.astimezone(timezone(timedelta(hours=3)))).target
            == a
        )
    invalid = replace(a, primary_window_minutes=10080)
    assert "headroom" in smart_decision(invalid, b).reason
    assert "headroom" in smart_decision(a, b, clock=NOW.replace(tzinfo=None)).reason
    assert isinstance(resolve_policy("smart_quota"), SmartQuotaPolicy)


@pytest.mark.parametrize(
    "changes",
    [
        {"stale": True},
        {"error": "bad"},
        {"reauth_required": True},
        {"identity": None},
        {"auth_present": False},
        {"account_match": False},
        {"ordinary_usage_allowed": False},
        {"quota_state": QuotaState.UNKNOWN},
    ],
)
def test_smart_preserves_safety_filters(changes):
    unsafe = replace(smart_health("unsafe", 90, 80, 1), **changes)
    safe = smart_health("safe", 75, 60, 72)
    assert smart_decision(unsafe, safe).target == safe
    assert not smart_decision(unsafe).should_switch


def test_frequent_primary_reset_does_not_override_weekly_urgency():
    weekly = smart_health("weekly", 75, 30, 2, primary_hours=5)
    primary = smart_health("primary", 75, 55, 72, primary_hours=0.01)
    assert smart_decision(weekly, primary).target == weekly
