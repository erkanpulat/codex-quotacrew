import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from codex_account_manager.adapters.interfaces import GoalInfo
from codex_account_manager.continuity import desktop_goal
from codex_account_manager.continuity.tracking import goal_signature
from codex_account_manager.core.errors import ConnectionNotReadyError
from codex_account_manager.core.events import bus
from codex_account_manager.storage.database import connect
from tests.unit.test_native_desktop import setup_native


@pytest.fixture
def goal_setup(monkeypatch):
    from codex_account_manager.platform import windows

    owner, server, source, ticket = setup_native()
    server.native = GoalInfo("thread", "finish", "active", True, 2000, 100)
    ticket = replace(ticket, goal_signature=goal_signature(server.native))
    server.verified_stored_turn = AsyncMock(return_value=source)
    server.latest = source
    opened = []
    monkeypatch.setattr(windows, "open_conversation", lambda tid: opened.append(tid))
    monkeypatch.setattr(desktop_goal, "POLL_SECONDS", 0)
    clock = [1000.0]
    monkeypatch.setattr(desktop_goal.time, "time", lambda: clock[0])
    return owner, server, ticket, opened, clock


async def journal_status():
    async with connect() as db:
        return await (await db.execute("SELECT status FROM continuation_attempts")).fetchone()


async def finish(owner):
    await asyncio.wait_for(asyncio.gather(*owner.goal_observer.tasks.values()), 10)


@pytest.mark.parametrize(
    "change", [None, "interrupted_metadata", "paused", "budget", "idle", "unknown", "absent", "ide"]
)
async def test_manual_goal_capture_requires_live_active_desktop(migrated_db, goal_setup, change):
    from codex_account_manager.core.errors import ConnectionNotReadyError

    owner, server, _ticket, opened, _clock = goal_setup
    from codex_account_manager.core.errors import DesktopContinuationRequired

    server.require_headless_compatible = AsyncMock(side_effect=DesktopContinuationRequired("local"))
    server.is_desktop_thread = AsyncMock(return_value=True)
    server.latest = {"id": "source", "status": "inProgress"}
    owner.native.thread_status.return_value = "active"
    owner.native.latest_turn = AsyncMock(side_effect=AssertionError("No history loading"))
    if change == "interrupted_metadata":
        server.latest["status"] = "interrupted"
    elif change == "paused":
        server.native = replace(server.native, status="paused")
    elif change == "budget":
        server.native = replace(server.native, tokens_used=2000)
    elif change == "idle":
        owner.native.thread_status.return_value = "idle"
    elif change == "unknown":
        owner.native.thread_status.side_effect = ConnectionNotReadyError("unavailable")
    elif change == "absent":
        server.native = GoalInfo("thread", None, None, False)
    elif change == "ide":
        server.is_desktop_thread = AsyncMock(return_value=False)
    try:
        ticket = await owner.prepare("thread", running_goal_only=True)
    except ConnectionNotReadyError:
        assert change == "unknown"
    else:
        if change in {None, "interrupted_metadata"}:
            assert ticket is not None and ticket.resume_active_goal
            assert ticket.turn_id == "source"
        else:
            assert ticket is None
    assert not opened


@pytest.mark.parametrize(
    "previous_attempt",
    [None, "goal_resumed", "goal_stopped", "goal_unconfirmed", "uncertain", "submitted"],
)
async def test_recovered_manual_goal_uses_opening_without_message(
    migrated_db, goal_setup, previous_attempt
):
    owner, server, ticket, opened, _clock = goal_setup
    ticket = replace(ticket, resume_active_goal=True)
    server.latest = {"id": ticket.turn_id, "status": "interrupted"}
    server.verified_stored_turn = AsyncMock(return_value=server.latest)
    owner.native.latest_turn = AsyncMock(side_effect=AssertionError("No native history loading"))

    async def activity(_tid):
        return "active" if opened else "notLoaded"

    owner.native.thread_status.side_effect = activity
    if previous_attempt:
        await owner._claim(ticket)
        await owner._record(ticket, previous_attempt)
    await owner.save_pending([ticket])
    await owner.recover_pending(ticket.account_id)
    await asyncio.gather(*tuple(owner.tasks))
    await finish(owner)
    if previous_attempt in {"uncertain", "submitted"}:
        assert not opened
        assert await journal_status() == (previous_attempt,)
    else:
        assert opened == ["thread"]
        assert await journal_status() == ("goal_resumed",)
        row = (await owner.tracker.visible())[0]
        assert row.verified and row.turn_status == "inProgress"
    owner.native.send.assert_not_awaited()


async def test_manual_goal_does_not_reopen_after_possible_user_stop(migrated_db, goal_setup):
    owner, server, ticket, opened, clock = goal_setup
    ticket = replace(ticket, resume_active_goal=True)
    server.latest = {"id": ticket.turn_id, "status": "interrupted"}
    await owner._claim(ticket)
    await owner.goal_observer.opened(ticket)
    opened.append("thread")
    clock[0] += 20
    owner.native.thread_status.return_value = "idle"
    assert await owner.goal_observer._probe(ticket, reopen=True) == "waiting"
    assert opened == ["thread"]
    assert await journal_status() == ("goal_opened",)


@pytest.mark.parametrize("resume_at", [5, 12])
async def test_delayed_goal_is_observed_without_reopening(migrated_db, goal_setup, resume_at):
    owner, server, ticket, opened, clock = goal_setup

    async def activity(_tid):
        if opened:
            clock[0] += 3
        if clock[0] >= 1000 + resume_at:
            server.latest = {"id": "next", "status": "inProgress"}
            return "active"
        return "idle"

    owner.native.thread_status.side_effect = activity
    await owner.run(ticket)
    await finish(owner)
    assert opened == ["thread"]
    owner.native.send.assert_not_awaited()
    assert await journal_status() == ("goal_resumed",)


async def test_one_reopen_can_start_goal_and_is_not_replayed(migrated_db, goal_setup):
    owner, server, ticket, opened, clock = goal_setup

    async def activity(_tid):
        if opened:
            clock[0] += 5
        if len(opened) == 2:
            server.latest = {"id": "next", "status": "inProgress"}
            return "active"
        return "idle"

    owner.native.thread_status.side_effect = activity
    await owner.run(ticket)
    await finish(owner)
    assert opened == ["thread", "thread"]
    assert await journal_status() == ("goal_resumed",)
    await owner.run(ticket)
    assert opened == ["thread", "thread"]
    owner.native.send.assert_not_awaited()


async def test_unresponsive_goal_expires_after_only_one_reopen(migrated_db, goal_setup):
    owner, server, ticket, opened, clock = goal_setup

    async def activity(_tid):
        if opened:
            clock[0] += 5
        return "idle"

    owner.native.thread_status.side_effect = activity
    await owner.run(ticket)
    await finish(owner)
    assert opened == ["thread", "thread"]
    assert await journal_status() == ("goal_unconfirmed",)
    assert await owner.goal_observer.state(ticket) is None
    await owner.run(ticket)
    assert opened == ["thread", "thread"]
    owner.native.send.assert_not_awaited()


@pytest.mark.parametrize("change", ["account", "paused", "budget", "objective", "disabled", "turn"])
async def test_changed_preconditions_never_reopen(migrated_db, goal_setup, monkeypatch, change):
    owner, server, ticket, opened, clock = goal_setup
    await owner._claim(ticket)
    await owner.goal_observer.opened(ticket)
    clock[0] += 20
    if change == "account":
        server.account_id = "another-account"
    elif change == "paused":
        server.native = replace(server.native, status="paused")
    elif change == "budget":
        server.native = replace(server.native, tokens_used=2000)
    elif change == "objective":
        server.native = replace(server.native, objective="changed")
    elif change == "disabled":
        owner.desktop_enabled = AsyncMock(return_value=False)
    else:
        server.latest = {"id": "user-turn", "status": "interrupted"}
    owner.goal_observer.start(ticket)
    await finish(owner)
    assert not opened
    assert await journal_status() == ("goal_stopped",)
    owner.native.send.assert_not_awaited()


async def test_recovered_open_keeps_deadline_and_reopen_count(migrated_db, goal_setup):
    owner, server, ticket, opened, clock = goal_setup
    await owner._claim(ticket)
    await owner.goal_observer.opened(ticket)
    clock[0] += 20
    await owner.goal_observer._probe(ticket, reopen=True)
    assert opened == ["thread"]
    assert await journal_status() == ("goal_reopened",)

    from codex_account_manager.continuity.automation import ContinuationSupervisor

    restarted = ContinuationSupervisor(factory=lambda: server)
    restarted.native.thread_status = AsyncMock(return_value="idle")
    assert await restarted.goal_observer.state(ticket) == ("goal_reopened", 1000)
    await restarted.goal_observer._probe(ticket, reopen=True)
    assert opened == ["thread"]
    clock[0] = 1061
    restarted.goal_observer.start(ticket)
    await finish(restarted)
    assert await journal_status() == ("goal_unconfirmed",)


async def test_old_observer_cannot_finish_replaced_account_request(migrated_db, goal_setup):
    owner, server, ticket, opened, clock = goal_setup
    await owner._claim(ticket)
    await owner.goal_observer.opened(ticket)
    replacement = replace(ticket, account_id="new-account")
    await owner.save_pending([replacement])
    await owner.goal_observer._finish(ticket, "goal_unconfirmed", "needs_user")
    assert await journal_status() == ("goal_opened",)
    assert await owner.goal_observer.state(replacement) == ("goal_opened", 1000)
    assert await owner.goal_observer.state(ticket) is None


async def test_expired_open_is_not_displayed_as_active_wait_after_crash(migrated_db, goal_setup):
    owner, server, ticket, opened, clock = goal_setup
    await owner.tracker.observe(
        "acc-1", "thread", {"id": "last", "status": "inProgress"}, server.native
    )
    await owner._claim(ticket)
    await owner.goal_observer.opened(ticket)
    assert (await owner.tracker.visible())[0].continuation_pending
    clock[0] += 61
    assert not (await owner.tracker.visible())[0].continuation_pending


@pytest.mark.parametrize("scope", [None, "desktop", "ide"])
async def test_stop_before_observer_starts_is_scoped(migrated_db, goal_setup, scope):
    owner, server, ticket, opened, clock = goal_setup
    await owner._claim(ticket)
    await owner.goal_observer.opened(ticket)
    owner.goal_observer._probe = AsyncMock(return_value="running")
    owner.goal_observer.start(ticket)
    bus.publish("continuation.stop", **({"scope": scope} if scope else {}))
    await finish(owner)
    assert await journal_status() == ("goal_resumed" if scope == "ide" else "goal_stopped",)
    assert not opened


@pytest.mark.parametrize("activity", ["active", "notLoaded", "missing", "unknown_capacity"])
async def test_reopen_requires_idle_desktop_and_known_capacity(migrated_db, goal_setup, activity):
    owner, server, ticket, opened, clock = goal_setup
    await owner._claim(ticket)
    await owner.goal_observer.opened(ticket)
    clock[0] += 20
    if activity == "missing":
        owner.native.thread_status.side_effect = ConnectionNotReadyError("missing")
        with pytest.raises(ConnectionNotReadyError):
            await owner.goal_observer._probe(ticket, reopen=True)
    else:
        owner.native.thread_status.return_value = (
            activity if activity in {"active", "notLoaded"} else "idle"
        )
        if activity == "unknown_capacity":
            snapshot = await server.read_account()
            server.read_account = AsyncMock(
                return_value=replace(snapshot, ordinary_usage_allowed=None)
            )
        await owner.goal_observer._probe(ticket, reopen=True)
    assert not opened
    assert await journal_status() == ("goal_opened",)


async def test_goal_wait_does_not_block_another_desktop_ticket(migrated_db, goal_setup):
    owner, server, ticket, opened, clock = goal_setup
    waiting = asyncio.Event()
    owner.goal_observer._probe = AsyncMock(side_effect=lambda *a, **kw: waiting.set() or "waiting")
    next_job = replace(ticket, thread_id="other")
    original_run = owner.run
    progressed = []

    async def run(item):
        if item.thread_id == "other":
            progressed.append(True)
        else:
            await original_run(item)

    owner.run = run
    owner.launch_batch([ticket, next_job])
    await asyncio.wait_for(asyncio.gather(*owner.tasks), 2)
    assert progressed == [True]
    assert owner.goal_observer.tasks
    await owner.stop()
    owner.native.send.assert_not_awaited()


async def test_waiting_goal_does_not_prevent_normal_message_delivery(migrated_db, goal_setup):
    owner, server, ticket, opened, clock = goal_setup
    absent = GoalInfo("other", None, None, False)
    turns = {"thread": server.latest, "other": {**server.latest, "id": "other-source"}}
    goal = server.native
    server.get_goal = AsyncMock(side_effect=lambda tid: goal if tid == "thread" else absent)
    server.latest_turn = AsyncMock(side_effect=lambda tid: turns[tid])
    server.verified_stored_turn = AsyncMock(side_effect=server.latest_turn)
    owner.native.latest_turn = AsyncMock(side_effect=server.latest_turn)
    owner.goal_observer._probe = AsyncMock(return_value="waiting")

    async def send(tid, source):
        assert tid == "other" and source == "other-source"
        turns[tid] = {"id": "other-next", "status": "inProgress"}
        return {"threadId": tid}

    owner.native.send.side_effect = send
    other = replace(
        ticket, thread_id="other", turn_id="other-source", goal_signature=goal_signature(absent)
    )
    owner.launch_batch([ticket, other])
    await asyncio.wait_for(asyncio.gather(*owner.tasks), 5)
    owner.native.send.assert_awaited_once_with("other", "other-source")
    assert opened == ["thread"]
    assert owner.goal_observer.tasks
    await owner.stop()


@pytest.mark.parametrize("state", ["active", "unavailable"])
async def test_initial_open_requires_ready_idle_desktop(migrated_db, goal_setup, state):
    owner, server, ticket, opened, clock = goal_setup
    if state == "active":
        owner.native.thread_status.return_value = "active"
        await owner.run(ticket)
    else:
        owner.native.thread_status.side_effect = ConnectionNotReadyError("not ready")
        with pytest.raises(ConnectionNotReadyError):
            await owner.run(ticket)
    assert not opened
    assert await journal_status() is None
    owner.native.send.assert_not_awaited()


async def test_pause_during_readiness_check_prevents_reopen(migrated_db, goal_setup):
    owner, server, ticket, opened, clock = goal_setup
    await owner._claim(ticket)
    await owner.goal_observer.opened(ticket)
    clock[0] += 20

    async def activity(_tid):
        server.native = replace(server.native, status="paused")
        return "idle"

    owner.native.thread_status.side_effect = activity
    assert await owner.goal_observer._probe(ticket, reopen=True) == "stopped"
    assert not opened


async def test_stopping_reopen_waits_for_shell_completion(migrated_db, goal_setup, monkeypatch):
    import threading

    from codex_account_manager.core.errors import TransactionError
    from codex_account_manager.core.operation_lock import OperationLock
    from codex_account_manager.platform import windows

    owner, server, ticket, opened, clock = goal_setup
    await owner._claim(ticket)
    await owner.goal_observer.opened(ticket)
    clock[0] += 20
    entered = threading.Event()
    release = threading.Event()

    def open_goal(_tid):
        entered.set()
        assert release.wait(2)

    monkeypatch.setattr(windows, "open_conversation", open_goal)
    owner.goal_observer.start(ticket)
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        stopped = asyncio.create_task(owner.stop())
        bus.publish("continuation.stop")
        bus.publish("continuation.stop")
        await asyncio.sleep(0.02)
        assert not stopped.done()
        with pytest.raises(TransactionError):
            with OperationLock(migrated_db.data_dir / "account-operation.lock"):
                pass
    finally:
        release.set()
    await asyncio.wait_for(stopped, 2)
    with OperationLock(migrated_db.data_dir / "account-operation.lock"):
        pass
    assert await journal_status() == ("goal_stopped",)
    owner.native.send.assert_not_awaited()
