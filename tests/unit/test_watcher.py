"""Watcher failover behaviour under each switch policy."""

from __future__ import annotations

from datetime import UTC, datetime

from codex_account_manager.continuity.policy import resolve_policy
from codex_account_manager.core.events import bus
from codex_account_manager.domain.models import ProfileHealth
from codex_account_manager.domain.states import QuotaState, SwitchPolicyKind
from codex_account_manager.monitoring.watcher import Watcher


def _health(alias, *, active=False, allowed=True, quota=QuotaState.AVAILABLE, secondary=0.0):
    return ProfileHealth(
        alias=alias,
        profile_id=alias,
        plan_type="plus",
        primary_used_percent=0.0,
        secondary_used_percent=secondary,
        primary_resets_at=None,
        secondary_resets_at=None,
        ordinary_usage_allowed=allowed,
        auth_present=True,
        account_match=True,
        is_active=active,
        quota_state=quota,
        last_checked_at=datetime.now(UTC),
    )


class _FakeContinuity:
    def __init__(self):
        from unittest.mock import AsyncMock

        from codex_account_manager.continuity.automation import ContinuationSupervisor

        self.automation = ContinuationSupervisor()
        self.automation.recover_pending = AsyncMock()
        self._turn_baselines = {}
        self.continued: list[str] = []

    async def observe_work(self):
        return {}

    async def continue_on_limit(self, alias: str):
        self.continued.append(alias)


class _StubAccounts:
    def __init__(self, health):
        self._health = health

    async def all_health(self):
        return self._health


async def test_health_finishes_before_pending_worker_launches(migrated_db):
    from unittest.mock import AsyncMock

    order = []
    continuity = _FakeContinuity()

    async def recover(_account):
        order.append("recovery")

    class Accounts:
        async def all_health(self):
            order.append("health")
            return []

    continuity.automation.recover_pending = AsyncMock(side_effect=recover)
    watcher = Watcher(accounts=Accounts(), continuity=continuity)
    await watcher.poll_once()
    assert order == ["health", "recovery"]


async def test_failover_continues_limited_conversation():
    health = [
        _health("ana", active=True, allowed=False, quota=QuotaState.LIMITED_WITH_RESET),
        _health("hesap2", secondary=5.0),
    ]
    continuity = _FakeContinuity()
    watcher = Watcher(
        accounts=_StubAccounts(health),
        continuity=continuity,
        policy=resolve_policy(SwitchPolicyKind.AVAILABILITY_FAILOVER),
    )
    await watcher.poll_once()
    assert continuity.continued == ["hesap2"]


async def test_free_zero_usage_never_triggers_a_failover_or_fallback():
    from dataclasses import replace

    health = [
        _health("limited", active=True, allowed=False, quota=QuotaState.LIMITED_WITH_RESET),
        replace(_health("free-zero"), plan_type="free"),
        _health("paid", secondary=65),
    ]
    continuity = _FakeContinuity()
    watcher = Watcher(
        accounts=_StubAccounts(health),
        continuity=continuity,
        policy=resolve_policy(SwitchPolicyKind.AVAILABILITY_FAILOVER),
    )
    await watcher.poll_once()
    assert continuity.continued == ["paid"]
    health.pop()
    continuity.continued.clear()
    await watcher.poll_once()
    assert not continuity.continued


async def test_recovery_error_does_not_suppress_independent_failover():
    from unittest.mock import AsyncMock

    continuity = _FakeContinuity()
    continuity.automation.recover_pending = AsyncMock(side_effect=OSError("unreadable record"))
    watcher = Watcher(
        accounts=_StubAccounts(
            [
                _health("ana", active=True, allowed=False, quota=QuotaState.LIMITED_WITH_RESET),
                _health("hesap2"),
            ]
        ),
        continuity=continuity,
        policy=resolve_policy(SwitchPolicyKind.AVAILABILITY_FAILOVER),
    )
    await watcher.poll_once()
    assert continuity.continued == ["hesap2"]


async def test_confirm_policy_only_suggests():
    health = [
        _health("ana", active=True, allowed=False, quota=QuotaState.LIMITED_WITH_RESET),
        _health("hesap2", secondary=5.0),
    ]
    continuity = _FakeContinuity()
    suggestions: list[str] = []
    unsubscribe = bus.subscribe(
        "switch.suggested", lambda e: suggestions.append(e.payload["target"])
    )
    try:
        watcher = Watcher(
            accounts=_StubAccounts(health),
            continuity=continuity,
            policy=resolve_policy(SwitchPolicyKind.CONFIRM),
        )
        await watcher.poll_once()
    finally:
        unsubscribe()
    assert continuity.continued == []  # never switches automatically
    assert suggestions == ["hesap2"]


async def test_manual_policy_does_nothing():
    health = [
        _health("ana", active=True, allowed=False, quota=QuotaState.LIMITED_WITH_RESET),
        _health("hesap2", secondary=5.0),
    ]
    continuity = _FakeContinuity()
    watcher = Watcher(
        accounts=_StubAccounts(health),
        continuity=continuity,
        policy=resolve_policy(SwitchPolicyKind.MANUAL),
    )
    await watcher.poll_once()
    assert continuity.continued == []


async def test_automatic_failure_is_visible_and_redacted():
    class FailingContinuity(_FakeContinuity):
        async def continue_on_limit(self, _alias):
            raise RuntimeError("password=private-value failed")

    health = [
        _health("limited", active=True, allowed=False, quota=QuotaState.LIMITED_WITH_RESET),
        _health("ready"),
    ]
    events = []
    unsubscribe = bus.subscribe("switch.failed", lambda event: events.append(event))
    try:
        watcher = Watcher(
            accounts=_StubAccounts(health),
            continuity=FailingContinuity(),
            policy=resolve_policy(SwitchPolicyKind.AVAILABILITY_FAILOVER),
        )
        await watcher.poll_once()
    finally:
        unsubscribe()
    assert len(events) == 1
    assert "private-value" not in events[0].payload["detail"]
    assert "failed" in events[0].payload["detail"]


async def test_deferred_switch_rechecks_on_next_poll_without_failure_backoff():
    from codex_account_manager.core.errors import HandoffDeferredError

    class WaitingContinuity(_FakeContinuity):
        async def continue_on_limit(self, alias):
            self.continued.append(alias)
            if len(self.continued) == 1:
                raise HandoffDeferredError("Work is still running.")

    continuity = WaitingContinuity()
    health = [
        _health("limited", active=True, allowed=False, quota=QuotaState.LIMITED_WITH_RESET),
        _health("ready"),
    ]
    watcher = Watcher(
        accounts=_StubAccounts(health),
        continuity=continuity,
        policy=resolve_policy(SwitchPolicyKind.AVAILABILITY_FAILOVER),
    )
    events = []
    unsubscribe = bus.subscribe("switch.deferred", events.append)
    try:
        await watcher.poll_once()
        await watcher.poll_once()
    finally:
        unsubscribe()
    assert len(events) == 1
    assert continuity.continued == ["ready", "ready"]
    assert watcher._retry_at == 0


async def test_temporary_account_lock_does_not_cause_five_minute_backoff():
    from codex_account_manager.core.errors import OperationBusyError

    class BusyContinuity(_FakeContinuity):
        async def continue_on_limit(self, alias):
            self.continued.append(alias)
            if len(self.continued) == 1:
                raise OperationBusyError("Another operation is in progress.", stage="prepare")

    continuity = BusyContinuity()
    watcher = Watcher(
        accounts=_StubAccounts(
            [
                _health("limited", active=True, allowed=False, quota=QuotaState.LIMITED_WITH_RESET),
                _health("ready"),
            ]
        ),
        continuity=continuity,
        policy=resolve_policy(SwitchPolicyKind.AVAILABILITY_FAILOVER),
    )
    await watcher.poll_once()
    assert watcher._retry_at == 0
    await watcher.poll_once()
    assert continuity.continued == ["ready", "ready"]


async def test_new_install_defaults_to_automatic_and_sixty_seconds(migrated_db):
    continuity = _FakeContinuity()
    watcher = Watcher(
        accounts=_StubAccounts(
            [
                _health("limited", active=True, allowed=False, quota=QuotaState.LIMITED_WITH_RESET),
                _health("ready"),
            ]
        ),
        continuity=continuity,
    )
    await watcher.poll_once()
    assert continuity.continued == ["ready"]
    assert watcher.poll_seconds == 60


async def test_saved_manual_mode_and_custom_interval_are_respected(migrated_db):
    from codex_account_manager.storage.repositories import SettingsRepository

    settings = SettingsRepository()
    await settings.set("switch_policy", "manual")
    await settings.set("poll_seconds", "120")
    continuity = _FakeContinuity()
    watcher = Watcher(
        accounts=_StubAccounts(
            [
                _health("limited", active=True, allowed=False, quota=QuotaState.LIMITED_WITH_RESET),
                _health("ready"),
            ]
        ),
        continuity=continuity,
    )
    await watcher.poll_once()
    assert continuity.continued == []
    assert watcher.poll_seconds == 120


async def test_settings_event_wakes_monitor_without_waiting_old_interval(migrated_db):
    import asyncio

    from codex_account_manager.storage.repositories import SettingsRepository

    await SettingsRepository().set("monitor_enabled", "true")
    seen = asyncio.Queue()

    class Accounts:
        async def all_health(self):
            seen.put_nowait(True)
            return []

    watcher = Watcher(accounts=Accounts(), continuity=_FakeContinuity())
    task = asyncio.create_task(watcher.run())
    try:
        await asyncio.wait_for(seen.get(), 2)
        await SettingsRepository().set("poll_seconds", "90")
        bus.publish("monitor.settings_changed")
        await asyncio.wait_for(seen.get(), 2)
        assert watcher.poll_seconds == 90
    finally:
        watcher.stop()
        await asyncio.wait_for(task, 2)


def test_invalid_intervals_have_safe_default():
    from codex_account_manager.monitoring.settings import poll_interval

    for value in (None, "invalid", "", -1, 0, 29, 3601, True):
        assert poll_interval(value) == 60
    for value in (30, "120", 3600):
        assert poll_interval(value) == int(value)


async def test_failed_automatic_switch_uses_backoff_until_another_target(monkeypatch):
    from codex_account_manager.monitoring import watcher as module

    now = [100.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])

    class FailingContinuity(_FakeContinuity):
        def __init__(self):
            super().__init__()
            self.calls = []

        async def continue_on_limit(self, alias):
            self.calls.append(alias)
            raise RuntimeError("Desktop did not start")

    health = [
        _health("limited", active=True, allowed=False, quota=QuotaState.LIMITED_WITH_RESET),
        _health("first"),
    ]
    continuity = FailingContinuity()
    watcher = Watcher(
        accounts=_StubAccounts(health),
        continuity=continuity,
        policy=resolve_policy(SwitchPolicyKind.AVAILABILITY_FAILOVER),
    )
    await watcher.poll_once()
    assert continuity.calls == ["first"]
    now[0] += 60
    await watcher.poll_once()
    assert continuity.calls == ["first"]
    now[0] += 240
    await watcher.poll_once()
    assert continuity.calls == ["first", "first"]
    now[0] += 60
    await watcher.poll_once()
    assert continuity.calls == ["first", "first"]
    health[1] = _health("second")
    await watcher.poll_once()
    assert continuity.calls == ["first", "first", "second"]


async def test_paused_monitor_refreshes_health_without_switching_or_tracking(migrated_db):
    import asyncio
    from unittest.mock import AsyncMock

    from codex_account_manager.storage.repositories import SettingsRepository

    await SettingsRepository().set("monitor_enabled", "false")
    watcher = Watcher(continuity=_FakeContinuity())
    watcher.poll_once = AsyncMock(return_value=[])
    task = asyncio.create_task(watcher.run())
    try:
        await asyncio.sleep(0.08)
        watcher.poll_once.assert_awaited_once_with(allow_switch=False, track_work=False)
        watcher.poll_once.reset_mock()
        await SettingsRepository().set("monitor_enabled", "true")
        bus.publish("monitor.settings_changed")
        async with asyncio.timeout(2):
            while not watcher.poll_once.called:
                await asyncio.sleep(0.01)
        watcher.poll_once.assert_awaited_once_with(allow_switch=True, track_work=True)
    finally:
        watcher.stop()
        await task
