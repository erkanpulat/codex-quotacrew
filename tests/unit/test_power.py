from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from codex_account_manager.adapters.interfaces import GoalInfo
from codex_account_manager.continuity.tracking import goal_signature
from codex_account_manager.domain.states import QuotaState
from codex_account_manager.monitoring.power import (
    PowerEvidence,
    PowerTarget,
    ShutdownPlan,
    all_accounts_limited,
    work_finished,
)
from tests.fakes import account_identity
from tests.unit.test_watcher import _health as watcher_health


def _health(alias, **kwargs):
    health = watcher_health(alias, **kwargs)
    return replace(
        health,
        primary_window_minutes=300,
        primary_used_percent=100 if kwargs.get("allowed") is False else 0,
    )


@pytest.mark.parametrize("mode", ["limits", "work"])
async def test_internal_guardians_do_not_block_but_running_workers_do(
    migrated_db, monkeypatch, mode
):
    from unittest.mock import AsyncMock

    from codex_account_manager.monitoring import power
    from tests.fakes import FakeAppServer
    from tests.integration.test_limit_continuation import _thread

    guardians = [
        replace(_thread(f"guardian-{i}", i), source="subAgentGuardian") for i in range(2051)
    ]
    root = replace(_thread("root", 2052), source="vscode")
    worker = replace(_thread("worker", 2053), source="subAgentThreadSpawn")
    server = FakeAppServer(threads=[*guardians, root, worker])
    server.is_desktop_thread = AsyncMock(return_value=True)
    server.get_goal = AsyncMock(side_effect=lambda tid: GoalInfo(tid, None, None, False))
    turns = {
        "root": {"id": "turn", "status": "completed"},
        "worker": {"id": "child-turn", "status": "completed"},
    }

    async def stored(tid):
        assert tid in turns, "Internal records must not be inspected as work"
        return turns[tid]

    server.verified_stored_turn = stored
    monkeypatch.setattr(power, "CodexAppServer", lambda *_a, **_k: server)
    accounts = AsyncMock()
    accounts.all_health.side_effect = lambda: [
        _health("a", allowed=False, quota=QuotaState.LIMITED_NO_RESET)
    ]
    checks = power.PowerChecks(accounts)
    target = PowerTarget(mode, "root", None, "turn") if mode == "work" else PowerTarget(mode)
    assert (await checks.check(target)).ready
    turns["worker"]["status"] = "inProgress"
    evidence = await checks.check(target)
    assert not evidence.ready and evidence.values["count"] == 1
    assert "worker" in evidence.values["jobs"]
    turns["worker"] = None
    assert not (await checks.check(target)).ready

    fresh_checks = power.PowerChecks(accounts)
    assert (await fresh_checks.check(target)).ready


async def test_shutdown_choices_exclude_internal_history_without_truncating_roots(
    migrated_db, monkeypatch
):
    from unittest.mock import AsyncMock

    from codex_account_manager.monitoring import power
    from tests.fakes import FakeAppServer
    from tests.integration.test_limit_continuation import _thread

    roots = [replace(_thread(str(i), i), source="cli") for i in range(1002)]
    server = FakeAppServer(threads=[*roots])
    server.observed_turn = AsyncMock(
        side_effect=lambda tid: {
            "id": "turn",
            "status": "inProgress" if tid == "1001" else "completed",
        }
    )
    monkeypatch.setattr(power, "CodexAppServer", lambda *_a, **_k: server)
    choices = await power.PowerChecks(AsyncMock()).conversations()
    assert [tid for tid, _ in choices] == ["1001"]


async def test_running_turn_disappearing_cannot_trigger_shutdown(migrated_db, monkeypatch):
    from unittest.mock import AsyncMock

    from codex_account_manager.monitoring import power
    from tests.fakes import FakeAppServer
    from tests.integration.test_limit_continuation import _thread

    server = FakeAppServer(threads=[replace(_thread("root", 1), source="cli")])
    server.observed_turn = AsyncMock(return_value={"id": "turn", "status": "inProgress"})
    monkeypatch.setattr(power, "CodexAppServer", lambda *_a, **_k: server)
    accounts = AsyncMock()
    accounts.all_health.side_effect = lambda: [
        _health("a", allowed=False, quota=QuotaState.LIMITED_NO_RESET)
    ]
    checks = power.PowerChecks(accounts)
    assert not (await checks.check(PowerTarget("limits"))).ready
    server.observed_turn.return_value = None
    assert not (await checks.check(PowerTarget("limits"))).ready


@pytest.mark.parametrize("mode", ["limits", "work"])
async def test_stored_interruption_cannot_hide_live_desktop_work(migrated_db, monkeypatch, mode):
    from unittest.mock import AsyncMock

    from codex_account_manager.adapters.native_desktop import NativeDesktop
    from codex_account_manager.core.errors import ConnectionNotReadyError
    from codex_account_manager.monitoring import power
    from tests.fakes import FakeAppServer
    from tests.integration.test_limit_continuation import _thread

    threads = [replace(_thread(tid, 100), source="vscode") for tid in ("root", "other")]
    server = FakeAppServer(threads=threads)
    server.is_desktop_thread = AsyncMock(return_value=True)
    server.verified_stored_turn = AsyncMock(return_value={"id": "turn", "status": "interrupted"})
    live = {"root": "completed", "other": "inProgress"}

    async def latest(tid):
        return {"id": "turn", "status": live[tid]}

    native = AsyncMock(side_effect=latest)
    monkeypatch.setattr(NativeDesktop, "latest_turn", native)
    monkeypatch.setattr(power, "CodexAppServer", lambda *_a, **_k: server)
    accounts = AsyncMock()
    accounts.all_health.side_effect = lambda: [
        _health("a", allowed=False, quota=QuotaState.LIMITED_NO_RESET)
    ]
    target = PowerTarget(mode, "root", None, "turn") if mode == "work" else PowerTarget(mode)

    assert not (await power.PowerChecks(accounts).check(target)).ready
    native.side_effect = ConnectionNotReadyError("synthetic unavailable desktop")
    assert not (await power.PowerChecks(accounts).check(target)).ready


async def test_shutdown_reads_ide_work_even_when_continuation_is_disabled(migrated_db, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from codex_account_manager.adapters.native_ide import NativeIDE
    from codex_account_manager.monitoring import power
    from tests.fakes import FakeAppServer
    from tests.integration.test_limit_continuation import _thread

    server = FakeAppServer(threads=[replace(_thread("root", 1), source="vscode")])
    monkeypatch.setattr(
        NativeIDE,
        "inspect",
        AsyncMock(
            return_value=SimpleNamespace(turn={"id": "turn", "status": "inProgress"}, blocked=False)
        ),
    )
    assert (await power.PowerChecks(AsyncMock())._turn(server, "root"))["status"] == "inProgress"


async def test_closed_ide_owner_can_use_verified_finished_history(migrated_db, monkeypatch):
    from unittest.mock import AsyncMock

    from codex_account_manager.adapters.native_ide import NativeIDE
    from codex_account_manager.core.errors import OwnerNotFoundError
    from codex_account_manager.monitoring import power
    from tests.fakes import FakeAppServer
    from tests.integration.test_limit_continuation import _thread

    server = FakeAppServer(threads=[replace(_thread("root", 1), source="vscode")])
    server.verified_stored_turn = AsyncMock(return_value={"id": "turn", "status": "completed"})
    monkeypatch.setattr(
        NativeIDE, "inspect", AsyncMock(side_effect=OwnerNotFoundError("not loaded"))
    )
    assert (await power.PowerChecks(AsyncMock())._turn(server, "root"))["status"] == "completed"


def test_shutdown_requires_arming_and_fresh_continuous_evidence():
    plan = ShutdownPlan(60)
    good = PowerEvidence(True, "done", "accounts-a-b")
    plan.observe(good, 0)
    assert plan.remaining(100) is None
    plan.arm(PowerTarget("limits"))
    plan.observe(good, 0)
    assert plan.remaining(10) == 110
    plan.observe(good, 20)
    plan.observe(good, 40)
    plan.observe(good, 60)
    plan.observe(good, 80)
    plan.observe(good, 100)
    plan.observe(good, 120)
    assert plan.remaining(120) == 0
    plan.cancel()
    assert plan.target is None and plan.remaining(60) is None


def test_sleep_stale_evidence_and_changed_accounts_reset_countdown():
    plan = ShutdownPlan(60)
    plan.arm(PowerTarget("limits"))
    plan.observe(PowerEvidence(True, "done", "a"), 0)
    assert plan.remaining(46) is None
    plan.observe(PowerEvidence(True, "done", "a"), 50)
    assert plan.remaining(50) == 120
    plan.observe(PowerEvidence(True, "done", "a-b"), 60)
    assert plan.remaining(60) == 120
    plan.observe(PowerEvidence(False, "network failed"), 65)
    assert plan.remaining(65) is None


@pytest.mark.parametrize("seconds", [0, 59, 86401, 120.5])
def test_countdown_has_bounds(seconds):
    with pytest.raises(ValueError):
        ShutdownPlan(seconds)


@pytest.mark.parametrize("minutes", [1, 120, 180, 1440])
def test_timer_uses_total_duration_without_fresh_evidence(minutes):
    plan = ShutdownPlan(minutes * 60)
    plan.arm(PowerTarget("timer"), now=100)
    assert plan.remaining(100) == minutes * 60
    plan.observe(PowerEvidence(False, "network failed"), 101)
    assert plan.remaining(101) == minutes * 60 - 1
    assert plan.remaining(100 + minutes * 60) == 0
    plan.cancel()
    assert plan.remaining(100 + minutes * 60) is None


@pytest.mark.parametrize("mode", ["work", "limits"])
def test_conditional_modes_always_use_two_minutes(mode):
    plan = ShutdownPlan(10800)
    plan.arm(PowerTarget(mode, "thread", None, "turn"))
    plan.observe(PowerEvidence(True, "done", "identity"), 0)
    assert plan.remaining(0) == 120


def test_countdown_can_change_before_arming_but_not_during_a_plan():
    plan = ShutdownPlan()
    plan.set_seconds(420)
    assert plan.seconds == 420
    plan.arm(PowerTarget("limits"))
    plan.observe(PowerEvidence(True, "verified", "accounts"), 10)
    assert plan.remaining(10) == 120
    with pytest.raises(ValueError):
        plan.set_seconds(60)
    plan.cancel()
    plan.set_seconds(60)
    assert plan.seconds == 60


def test_empty_stale_unknown_reauth_and_available_accounts_never_trigger():
    now = datetime.now(UTC)
    good = _health("a", allowed=False, quota=QuotaState.LIMITED_WITH_RESET)
    assert all_accounts_limited([good], now + timedelta(seconds=1))
    assert not all_accounts_limited([], now)
    for changes in [
        {"stale": True},
        {"error": "timeout"},
        {"account_match": None},
        {"reauth_required": True},
        {"ordinary_usage_allowed": None},
        {"auth_present": False},
        {"quota_state": QuotaState.UNKNOWN},
        {"last_checked_at": now - timedelta(seconds=61)},
        {"last_checked_at": now + timedelta(seconds=60)},
        {"last_checked_at": None},
    ]:
        assert not all_accounts_limited([replace(good, **changes)], now), changes
    assert not all_accounts_limited([good, _health("b")], now)


def test_unused_reset_credits_do_not_restore_five_hour_capacity():
    from codex_account_manager.domain.models import ResetCredits

    health = _health("a", allowed=False, quota=QuotaState.LIMITED_NO_RESET)
    assert all_accounts_limited(
        [replace(health, reset_credits=ResetCredits(available_count=1))], datetime.now(UTC)
    )


@pytest.mark.parametrize("percent,minutes", [(None, 300), (99, 300), (100, 10080), (100, None)])
def test_only_verified_five_hour_exhaustion_triggers(percent, minutes):
    health = _health("a", allowed=False, quota=QuotaState.LIMITED_NO_RESET)
    assert not all_accounts_limited(
        [replace(health, primary_used_percent=percent, primary_window_minutes=minutes)],
        datetime.now(UTC),
    )


def test_goal_completion_must_match_original_objective_and_budget():
    goal = GoalInfo("a", "finish task", "active", True, 2000, 100)
    target = PowerTarget("work", "a", goal_signature(goal), "turn-1")
    turn = {"id": "turn-2", "status": "completed"}
    assert work_finished(target, turn, replace(goal, status="complete"))
    for status in ("active", "paused", "usageLimited", "blocked", "budgetLimited"):
        assert not work_finished(target, turn, replace(goal, status=status))
    assert not work_finished(target, turn, replace(goal, status="complete", objective="new"))
    assert not work_finished(
        target, {**turn, "status": "interrupted"}, replace(goal, status="complete")
    )
    assert not work_finished(target, turn, None)


@pytest.mark.parametrize("status", ["completed", "complete", "success"])
def test_shutdown_completion_accepts_codex_status_variants(status):
    goal = GoalInfo("a", None, None, False)
    target = PowerTarget("work", "a", None, "turn-1")
    assert work_finished(target, {"id": "turn-1", "status": status}, goal)


def test_without_goal_selected_conversation_completes_after_handoff():
    target = PowerTarget("work", "a", None, "turn-1")
    goal = GoalInfo("a", None, None, False)
    assert work_finished(target, {"id": "turn-1", "status": "completed"}, goal)
    assert work_finished(target, {"id": "turn-2", "status": "completed"}, goal)
    assert not work_finished(target, {"id": "turn-2", "status": "failed"}, goal)
    assert not work_finished(
        target, {"id": "turn-2", "status": "completed"}, replace(goal, thread_id="other")
    )


def test_five_hour_limit_can_be_in_secondary_window():
    now = datetime.now(UTC)
    item = _health("a", allowed=False)
    item = replace(
        item,
        last_checked_at=now,
        primary_window_minutes=10080,
        primary_used_percent=20,
        secondary_window_minutes=300,
        secondary_used_percent=100,
        quota_state=QuotaState.LIMITED_NO_RESET,
    )
    assert all_accounts_limited([item], now)
    assert not all_accounts_limited([replace(item, secondary_used_percent=float("nan"))], now)


async def test_power_checks_reject_unknown_active_and_truncated_work(migrated_db, monkeypatch):
    from unittest.mock import AsyncMock

    from codex_account_manager.domain.models import ThreadInfo
    from codex_account_manager.monitoring import power
    from tests.fakes import ExecutionServer

    server = ExecutionServer()
    server.list_threads = AsyncMock(return_value=[])
    monkeypatch.setattr(power, "CodexAppServer", lambda *_a, **_k: server)
    accounts = AsyncMock()
    accounts.all_health.return_value = [
        _health("a", allowed=False, quota=QuotaState.LIMITED_NO_RESET)
    ]
    checks = power.PowerChecks(accounts)
    assert (await checks.check(PowerTarget("limits"))).ready
    server.list_threads.side_effect = RuntimeError("Synthetic discovery failure")
    evidence = await checks.check(PowerTarget("limits"))
    assert not evidence.ready
    assert evidence.reason == "Conversation discovery is incomplete. Shutdown will wait."
    server.list_threads.side_effect = None
    server.list_threads.return_value = [
        ThreadInfo("thread", None, None, None, None, None, None, None, None, "active")
    ]
    checks._turn = AsyncMock(return_value={"id": "turn", "status": "inProgress"})
    assert not (await checks.check(PowerTarget("limits"))).ready
    checks._turn.return_value = None
    assert not (await checks.check(PowerTarget("limits"))).ready
    checks._turn.return_value = {"id": "turn", "status": "completed"}
    server.get_goal = AsyncMock(return_value=None)
    assert (await checks.check(PowerTarget("limits"))).ready
    assert not (await checks.check(PowerTarget("work", "thread", None, "turn"))).ready


@pytest.mark.parametrize("status", [None, "unknown", "awaitingUser"])
def test_unknown_work_status_is_conservatively_running(status):
    from codex_account_manager.monitoring.power import PowerChecks

    assert PowerChecks._may_be_running(status)


async def test_limits_mode_allows_stopped_work_with_an_unfinished_goal(migrated_db, monkeypatch):
    from unittest.mock import AsyncMock

    from codex_account_manager.domain.models import ThreadInfo
    from codex_account_manager.monitoring import power
    from tests.fakes import ExecutionServer

    server = ExecutionServer()
    server.list_threads = AsyncMock(
        return_value=[
            ThreadInfo("thread", None, None, None, None, None, None, None, None, "active")
        ]
    )
    server.get_goal = AsyncMock(return_value=GoalInfo("thread", "unfinished", "active", True))
    monkeypatch.setattr(power, "CodexAppServer", lambda *_a, **_k: server)
    accounts = AsyncMock()
    accounts.all_health.return_value = [
        _health("a", allowed=False, quota=QuotaState.LIMITED_NO_RESET)
    ]
    checks = power.PowerChecks(accounts)
    checks._turn = AsyncMock(return_value={"id": "turn", "status": "interrupted"})
    assert (await checks.check(PowerTarget("limits"))).ready
    assert not (await checks.check(PowerTarget("work", "thread", None, "turn"))).ready
    checks._turn.return_value = {"id": "turn", "status": "inProgress"}
    assert not (await checks.check(PowerTarget("limits"))).ready


async def test_timer_never_uses_condition_checks():
    from unittest.mock import AsyncMock

    from codex_account_manager.monitoring.power import PowerChecks

    accounts = AsyncMock()
    with pytest.raises(ValueError, match="Timer"):
        await PowerChecks(accounts).check(PowerTarget("timer"))
    accounts.all_health.assert_not_called()


def test_slow_successful_checks_do_not_perpetually_reset_countdown():
    plan = ShutdownPlan()
    plan.arm(PowerTarget("limits"))
    for now in (0, 35, 70, 105, 140):
        plan.observe(PowerEvidence(True, "done", "accounts"), now)
    assert plan.remaining(140) == 0


@pytest.mark.parametrize("count", [43, 1001])
async def test_many_old_conversations_do_not_block_shutdown_but_one_running_does(
    migrated_db, monkeypatch, count
):
    from unittest.mock import AsyncMock

    from codex_account_manager.monitoring import power
    from tests.fakes import ExecutionServer
    from tests.integration.test_limit_continuation import _thread

    server = ExecutionServer()
    server.list_threads = AsyncMock(return_value=[_thread(str(i), i) for i in range(count)])
    monkeypatch.setattr(power, "CodexAppServer", lambda *_a, **_k: server)
    accounts = AsyncMock()
    accounts.all_health.return_value = [
        _health("a", allowed=False, quota=QuotaState.LIMITED_NO_RESET)
    ]
    checks = power.PowerChecks(accounts)
    checks._turn = AsyncMock(return_value={"id": "turn", "status": "completed"})
    assert (await checks.check(PowerTarget("limits"))).ready
    assert checks._turn.await_count == count

    async def turn(_adapter, tid):
        return {"id": "turn", "status": "inProgress" if tid == str(count - 1) else "completed"}

    checks._turn = turn
    evidence = await checks.check(PowerTarget("limits"))
    assert not evidence.ready
    assert evidence.values["count"] == 1

    checks._turn = AsyncMock(return_value=None)
    assert not (await checks.check(PowerTarget("limits"))).ready


@pytest.mark.parametrize("legacy", [False, True])
async def test_completed_work_waits_for_pending_independent_continuation(
    migrated_db, monkeypatch, legacy
):
    from unittest.mock import AsyncMock

    from codex_account_manager.continuity.automation import (
        ContinuationSupervisor,
        ContinuationTicket,
    )
    from codex_account_manager.monitoring import power
    from tests.fakes import ExecutionServer
    from tests.integration.test_limit_continuation import _thread

    server = ExecutionServer()
    server.list_threads = AsyncMock(return_value=[_thread("thread", 1)])
    monkeypatch.setattr(power, "CodexAppServer", lambda *_a, **_k: server)
    checks = power.PowerChecks(AsyncMock())
    checks._turn = AsyncMock(return_value={"id": "complete", "status": "completed"})
    ticket = ContinuationTicket(
        "other",
        "stopped",
        "acc-1",
        owner_key=None if legacy else account_identity("acc-1").fingerprint,
    )
    supervisor = ContinuationSupervisor()
    await supervisor.save_pending([ticket])
    target = PowerTarget("work", "thread", None, "original")
    assert not (await checks.check(target)).ready
    await supervisor.discard_pending([ticket])
    assert (await checks.check(target)).ready


@pytest.mark.parametrize("mode", ["work", "limits"])
@pytest.mark.parametrize("manual_goal", [False, True])
async def test_pending_goal_activation_blocks_conditional_shutdown(
    migrated_db, monkeypatch, mode, manual_goal
):
    from unittest.mock import AsyncMock

    from codex_account_manager.continuity.automation import (
        ContinuationSupervisor,
        ContinuationTicket,
    )
    from codex_account_manager.monitoring import power
    from tests.fakes import ExecutionServer
    from tests.integration.test_limit_continuation import _thread

    server = ExecutionServer()
    server.list_threads = AsyncMock(return_value=[_thread("thread", 1)])
    monkeypatch.setattr(power, "CodexAppServer", lambda *_a, **_k: server)
    accounts = AsyncMock()
    accounts.all_health.side_effect = lambda: [
        _health("a", allowed=False, quota=QuotaState.LIMITED_NO_RESET)
    ]
    checks = power.PowerChecks(accounts)
    checks._turn = AsyncMock(return_value={"id": "complete", "status": "completed"})
    ticket = ContinuationTicket(
        "other",
        "stopped",
        "acc-1",
        "signature",
        desktop=True,
        resume_active_goal=manual_goal,
        owner_key=account_identity("acc-1").fingerprint,
    )
    owner = ContinuationSupervisor()
    await owner._claim(ticket)
    await owner.goal_observer.opened(ticket)
    target = PowerTarget(mode, "thread", None, "original") if mode == "work" else PowerTarget(mode)
    evidence = await checks.check(target)
    assert not evidence.ready and evidence.reason == "Waiting for {count} goals to resume."
    await owner.goal_observer._finish(ticket, "goal_unconfirmed", "needs_user")
    assert (await checks.check(target)).ready


async def test_other_active_goal_between_turns_blocks_work_shutdown(migrated_db, monkeypatch):
    from unittest.mock import AsyncMock

    from codex_account_manager.adapters.interfaces import GoalInfo
    from codex_account_manager.monitoring import power
    from tests.fakes import ExecutionServer
    from tests.integration.test_limit_continuation import _thread

    server = ExecutionServer()
    server.list_threads = AsyncMock(return_value=[_thread("selected", 1), _thread("other", 2)])

    async def goal(tid):
        return GoalInfo(
            tid,
            "unfinished" if tid == "other" else None,
            "active" if tid == "other" else None,
            tid == "other",
        )

    server.get_goal = goal
    monkeypatch.setattr(power, "CodexAppServer", lambda *_a, **_k: server)
    checks = power.PowerChecks(AsyncMock())
    checks._turn = AsyncMock(return_value={"id": "complete", "status": "completed"})
    evidence = await checks.check(PowerTarget("work", "selected", None, "original"))
    assert not evidence.ready


async def test_large_shutdown_scan_bounds_tasks_and_cleans_up_on_cancel(migrated_db, monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock

    from codex_account_manager.monitoring import power
    from tests.fakes import ExecutionServer
    from tests.integration.test_limit_continuation import _thread

    server = ExecutionServer()
    server.list_threads = AsyncMock(return_value=[_thread(str(i), i) for i in range(2000)])
    monkeypatch.setattr(power, "CodexAppServer", lambda *_a, **_k: server)
    checks = power.PowerChecks(AsyncMock())
    entered = asyncio.Event()
    hold = asyncio.Event()
    active = 0
    baseline = set(asyncio.all_tasks())

    async def blocked_turn(_adapter, _tid):
        nonlocal active
        active += 1
        if active == 4:
            entered.set()
        try:
            await hold.wait()
        finally:
            active -= 1

    checks._turn = blocked_turn
    scan = asyncio.create_task(checks.check(PowerTarget("limits")))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert len(set(asyncio.all_tasks()) - baseline) <= 5
    finally:
        scan.cancel()
        with pytest.raises(asyncio.CancelledError):
            await scan
    assert active == 0
    assert not (set(asyncio.all_tasks()) - baseline)


async def test_interrupted_scans_eventually_visit_every_conversation(migrated_db, monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock

    from codex_account_manager.monitoring import power
    from tests.fakes import ExecutionServer
    from tests.integration.test_limit_continuation import _thread

    server = ExecutionServer()
    server.list_threads = AsyncMock(return_value=[_thread(str(i), i) for i in range(12)])
    monkeypatch.setattr(power, "CodexAppServer", lambda *_a, **_k: server)
    checks = power.PowerChecks(AsyncMock())
    visited = set()
    for _ in range(3):
        started = []
        entered = asyncio.Event()
        hold = asyncio.Event()

        async def slow_turn(_adapter, tid, started=started, entered=entered, hold=hold):
            visited.add(tid)
            started.append(tid)
            if len(started) == 4:
                entered.set()
            await hold.wait()

        checks._turn = slow_turn
        scan = asyncio.create_task(checks.check(PowerTarget("limits")))
        try:
            await asyncio.wait_for(entered.wait(), timeout=2)
        finally:
            scan.cancel()
            with pytest.raises(asyncio.CancelledError):
                await scan
    assert visited == {str(i) for i in range(12)}
    checks._turn = AsyncMock(return_value=None)
    evidence = await checks.check(PowerTarget("limits"))
    assert not evidence.ready
    assert evidence.values["count"] == 12


@pytest.mark.parametrize(
    "state, expected",
    [
        (
            "blocked",
            "The selected goal is blocked. Open the conversation in Codex to review what it needs.",
        ),
        ("paused", "The selected goal is paused. Shutdown will wait for it to complete."),
        (
            "usageLimited",
            "The selected goal reached its usage limit. Shutdown will wait for it to resume and complete.",
        ),
        ("active", "The selected goal is still active. Shutdown will wait for it to complete."),
        ("changed", "The selected goal changed. Cancel this plan and select the work again."),
        (
            "missing",
            "The selected goal is no longer present. Cancel this plan and select the work again.",
        ),
        (
            "unknown",
            "The selected goal could not be verified. Shutdown is waiting for a fresh check.",
        ),
    ],
)
async def test_shutdown_explains_selected_goal_state(migrated_db, monkeypatch, state, expected):
    from unittest.mock import AsyncMock

    from codex_account_manager.monitoring import power
    from tests.fakes import ExecutionServer
    from tests.integration.test_limit_continuation import _thread

    original = GoalInfo("thread", "original", "active", True)
    goal = replace(original, status=state)
    if state == "changed":
        goal = replace(original, objective="different")
    elif state == "missing":
        goal = GoalInfo("thread", None, None, False)
    elif state == "unknown":
        goal = None
    server = ExecutionServer()
    server.list_threads = AsyncMock(return_value=[_thread("thread", 1)])
    server.get_goal = AsyncMock(return_value=goal)
    monkeypatch.setattr(power, "CodexAppServer", lambda *_a, **_k: server)
    checks = power.PowerChecks(AsyncMock())
    checks._turn = AsyncMock(return_value={"id": "turn", "status": "completed"})
    target = PowerTarget("work", "thread", goal_signature(original), "turn")
    evidence = await checks.check(target)
    assert not evidence.ready
    assert evidence.reason == expected


async def test_work_countdown_restarts_when_same_workspace_user_changes(migrated_db, monkeypatch):
    from unittest.mock import AsyncMock

    from codex_account_manager.monitoring import power
    from tests.fakes import ExecutionServer
    from tests.integration.test_limit_continuation import _thread

    server = ExecutionServer()
    server.identity = account_identity("W1", "U1")
    server.account_id = "W1"
    server.list_threads = AsyncMock(return_value=[_thread("thread", 1)])
    monkeypatch.setattr(power, "CodexAppServer", lambda *_a, **_k: server)
    checks = power.PowerChecks(AsyncMock())
    checks._turn = AsyncMock(return_value={"id": "complete", "status": "completed"})
    target = PowerTarget("work", "thread", None, "original")
    first = await checks.check(target)
    plan = ShutdownPlan()
    plan.arm(target, now=0)
    plan.observe(first, 0)
    server.identity = account_identity("W1", "U2")
    second = await checks.check(target)
    assert first.ready and second.ready
    assert first.identity != second.identity
    plan.observe(second, 20)
    assert plan.remaining(20) == 120


async def test_limits_shutdown_explains_missing_profile_owner(migrated_db, monkeypatch):
    from unittest.mock import AsyncMock

    from codex_account_manager.monitoring import power
    from tests.fakes import ExecutionServer

    server = ExecutionServer()
    monkeypatch.setattr(power, "CodexAppServer", lambda *_a, **_k: server)
    accounts = AsyncMock()
    accounts.all_health.return_value = [
        replace(_health("legacy", allowed=False, quota=QuotaState.LIMITED_NO_RESET), identity=None)
    ]
    result = await power.PowerChecks(accounts).check(PowerTarget("limits"))
    assert not result.ready
    assert result.values == {"count": 1}
    assert "owner" in result.reason
