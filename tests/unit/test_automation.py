import asyncio
from dataclasses import replace

import pytest

from codex_account_manager.adapters.interfaces import GoalInfo
from codex_account_manager.continuity.automation import (
    ContinuationSupervisor,
    goal_allows_continuation,
)
from codex_account_manager.core.events import bus
from codex_account_manager.domain.states import GoalState
from codex_account_manager.storage.database import connect
from codex_account_manager.storage.repositories import SettingsRepository
from tests.fakes import ExecutionServer


def goal(status="active", *, budget=1000, used=100):
    return GoalInfo("thread", "Finish the requested task", status, True, budget, used)


async def prepared(supervisor):
    ticket = await supervisor.prepare("thread")
    assert ticket is not None
    return replace(ticket, account_id="acc-1")


@pytest.mark.parametrize("surface", ["desktop", "ide"])
async def test_disabled_native_surface_never_falls_back_to_other_owner(migrated_db, surface):
    from unittest.mock import AsyncMock

    from codex_account_manager.core.errors import DesktopContinuationRequired

    server = ExecutionServer()

    async def native_only(_thread):
        raise DesktopContinuationRequired("native owner required")

    server.require_headless_compatible = native_only
    server.is_desktop_thread = AsyncMock(return_value=surface == "desktop")
    supervisor = ContinuationSupervisor(factory=lambda: server)
    supervisor.native.inspect = AsyncMock(side_effect=AssertionError("disabled surface"))
    supervisor.ide.inspect = AsyncMock(side_effect=AssertionError("disabled surface"))
    await SettingsRepository().set("desktop_continue", "false" if surface == "desktop" else "true")
    await SettingsRepository().set("ide_continue", "false" if surface == "ide" else "true")
    assert await supervisor.prepare("thread") is None
    supervisor.native.inspect.assert_not_awaited()
    supervisor.ide.inspect.assert_not_awaited()
    assert server.closed and not server.turn_calls


@pytest.mark.parametrize("status", ["paused", "blocked", "complete", "budgetLimited", "unknown"])
def test_native_goal_states_are_never_automatically_reactivated(status):
    assert not goal_allows_continuation(goal(status))


@pytest.mark.parametrize(
    "budget,used", [(100, 100), (100, 101), (-1, 0), (True, 0), (100, -1), (100, True)]
)
def test_goal_budget_is_not_reset_or_ignored(budget, used):
    assert not goal_allows_continuation(goal(budget=budget, used=used))


@pytest.mark.parametrize(
    "status,error",
    [
        ("interrupted", None),
        ("inProgress", None),
        ("completed", None),
        ("failed", "sessionBudgetExceeded"),
        ("failed", "other"),
        ("failed", "rateLimitExceeded"),
    ],
)
async def test_unverified_or_user_interrupted_work_is_not_selected(migrated_db, status, error):
    server = ExecutionServer()
    server.latest = {"id": "last", "status": status, "error": {"codexErrorInfo": error}}
    supervisor = ContinuationSupervisor(factory=lambda: server)
    assert await supervisor.prepare("thread") is None
    assert server.closed


async def test_usage_limit_without_goal_continues_exactly_one_turn(migrated_db):
    server = ExecutionServer()
    supervisor = ContinuationSupervisor(factory=lambda: server)
    ticket = await prepared(supervisor)
    await supervisor.run(ticket)
    assert len(server.turn_calls) == 1
    assert server.resume_calls == ["thread"]
    assert server.closed
    async with connect() as db:
        assert await (await db.execute("SELECT status FROM continuation_attempts")).fetchall() == [
            ("completed",)
        ]


async def test_prepared_continuation_survives_process_exit_and_recovers_once(migrated_db):
    import subprocess
    import sys
    from dataclasses import asdict

    server = ExecutionServer()
    supervisor = ContinuationSupervisor(factory=lambda: server)
    ticket = await prepared(supervisor)
    script = (
        "import asyncio, os\nfrom pathlib import Path\n"
        "from codex_account_manager.core.paths import paths\n"
        "from codex_account_manager.continuity.automation import ContinuationSupervisor, ContinuationTicket\n"
        f"object.__setattr__(paths, 'db_path', Path({str(migrated_db.db_path)!r}))\n"
        f"asyncio.run(ContinuationSupervisor().save_pending([ContinuationTicket(**{asdict(ticket)!r})]))\n"
        "os._exit(73)\n"
    )
    result = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-c", script],
        capture_output=True,
        timeout=15,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    assert result.returncode == 73, result.stderr
    restarted = ContinuationSupervisor(factory=lambda: server)
    await restarted.recover_pending("acc-1")
    await asyncio.gather(*tuple(restarted.tasks))
    await restarted.recover_pending("acc-1")
    assert len(server.turn_calls) == 1
    async with connect() as db:
        assert not await (await db.execute("SELECT * FROM pending_continuations")).fetchall()


@pytest.mark.parametrize("changed", ["account", "turn", "disabled", "expired", "claimed"])
async def test_pending_recovery_never_replays_unsafe_work(migrated_db, changed):
    server = ExecutionServer()
    supervisor = ContinuationSupervisor(factory=lambda: server)
    ticket = await prepared(supervisor)
    await supervisor.save_pending([ticket])
    if changed == "turn":
        server.latest = {"id": "new-user-turn", "status": "inProgress"}
    elif changed == "disabled":
        await SettingsRepository().set("auto_continue", "false")
    elif changed == "claimed":
        assert await supervisor._claim(ticket)
    elif changed == "expired":
        async with connect() as db:
            await db.execute("UPDATE pending_continuations SET created_at=0")
            await db.commit()
    restarted = ContinuationSupervisor(factory=lambda: server)
    await restarted.recover_pending("other" if changed == "account" else "acc-1")
    await asyncio.gather(*tuple(restarted.tasks))
    assert not server.turn_calls
    async with connect() as db:
        assert not await (await db.execute("SELECT * FROM pending_continuations")).fetchall()


async def test_desktop_channel_failure_keeps_ticket_for_recovery(migrated_db):
    from dataclasses import replace

    from codex_account_manager.core.errors import AppServerError

    server = ExecutionServer()
    supervisor = ContinuationSupervisor(factory=lambda: server)
    ticket = replace(await prepared(supervisor), desktop=True)
    await supervisor.save_pending([ticket])

    async def unavailable(_ticket):
        raise AppServerError("The verified Desktop native channel is unavailable.")

    supervisor.run = unavailable
    supervisor.launch_batch([ticket])
    await asyncio.gather(*tuple(supervisor.tasks))

    async with connect() as db:
        rows = await (await db.execute("SELECT thread_id FROM pending_continuations")).fetchall()
    assert rows == [(ticket.thread_id,)]


async def test_stopping_monitoring_can_discard_waiting_tickets_before_reenable(migrated_db):
    server = ExecutionServer()
    supervisor = ContinuationSupervisor(factory=lambda: server)
    await supervisor.save_pending([await prepared(supervisor)])
    await supervisor.stop(clear_pending=True)
    await supervisor.recover_pending("acc-1")
    assert not supervisor.tasks and not server.turn_calls
    async with connect() as db:
        assert not await (await db.execute("SELECT * FROM pending_continuations")).fetchall()


async def test_cancelled_desktop_lane_keeps_unstarted_tickets_for_recovery(migrated_db):
    import asyncio
    from dataclasses import replace

    supervisor = ContinuationSupervisor(factory=lambda: ExecutionServer())
    first = replace(await prepared(supervisor), desktop=True, thread_id="first")
    second = replace(first, thread_id="second")
    await supervisor.save_pending([first, second])
    started = asyncio.Event()

    async def blocked(ticket):
        started.set()
        await asyncio.Event().wait()

    supervisor.run = blocked
    supervisor.launch_batch([first, second])
    task = next(iter(supervisor.tasks))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    async with connect() as db:
        rows = await (
            await db.execute("SELECT thread_id FROM pending_continuations ORDER BY thread_id")
        ).fetchall()
    assert rows == [("first",), ("second",)]


async def test_failed_desktop_job_never_restarts_app_and_next_job_still_runs(
    migrated_db, monkeypatch
):
    from dataclasses import replace
    from unittest.mock import AsyncMock

    from codex_account_manager.adapters.native_desktop import NativeDesktop
    from codex_account_manager.core.errors import ConnectionNotReadyError

    supervisor = ContinuationSupervisor(factory=lambda: ExecutionServer())
    first = replace(await prepared(supervisor), desktop=True, thread_id="first")
    second = replace(first, thread_id="second")
    restart = AsyncMock(side_effect=AssertionError("Continuation must never restart Desktop"))
    monkeypatch.setattr(NativeDesktop, "restart", restart, raising=False)
    attempted = []

    async def execute(ticket):
        attempted.append(ticket.thread_id)
        if ticket.thread_id == "first":
            raise ConnectionNotReadyError("unavailable")

    supervisor.run = execute
    supervisor.launch_batch([first, second])
    await asyncio.gather(*tuple(supervisor.tasks))
    assert attempted == ["first", "second"]
    restart.assert_not_awaited()


async def test_limit_during_owned_continuation_remains_eligible_for_next_handoff(migrated_db):
    class LimitedAgain(ExecutionServer):
        async def run_continuation_turn(self, *args, **kwargs):
            turn = await super().run_continuation_turn(*args, **kwargs)
            self.latest = {
                **turn,
                "status": "failed",
                "error": {"codexErrorInfo": "usageLimitExceeded"},
            }
            return self.latest

    server = LimitedAgain()
    supervisor = ContinuationSupervisor(factory=lambda: server)
    await supervisor.run(await prepared(supervisor))
    pending = await supervisor.tracker.limited("acc-1")
    assert len(pending) == 1
    assert pending[0][1] == "1"
    next_ticket = await supervisor.prepare("thread")
    assert next_ticket is not None and next_ticket.turn_id == "1"
    assert not await supervisor.tracker.limited("acc-other")


async def test_active_goal_continues_until_complete_without_resetting_budget(migrated_db):
    server = ExecutionServer()
    server.native = goal("usageLimited", used=200)
    server.after_turn = [goal(used=300), goal("complete", used=400)]
    supervisor = ContinuationSupervisor(factory=lambda: server)
    await supervisor.run(await prepared(supervisor))
    assert len(server.turn_calls) == 2
    assert server.reactivations == [goal("usageLimited", used=200)]
    assert server.native.token_budget == 1000
    assert server.native.tokens_used == 400


async def test_goal_budget_exhausted_after_first_turn_stops_loop(migrated_db):
    server = ExecutionServer()
    server.native = goal()
    server.after_turn = [goal("budgetLimited", used=1000)]
    supervisor = ContinuationSupervisor(factory=lambda: server)
    await supervisor.run(await prepared(supervisor))
    assert len(server.turn_calls) == 1
    assert server.reactivations == []


async def test_changed_goal_after_handoff_does_not_receive_old_continuation(migrated_db):
    server = ExecutionServer()
    server.native = goal()
    supervisor = ContinuationSupervisor(factory=lambda: server)
    ticket = await prepared(supervisor)
    server.native = replace(server.native, objective="A different user request")
    await supervisor.run(ticket)
    assert not server.turn_calls


async def test_batch_preserves_order_for_work_sharing_desktop(migrated_db):
    server = ExecutionServer()
    supervisor = ContinuationSupervisor(factory=lambda: server)
    ticket = await prepared(supervisor)
    seen = []

    async def run(saved):
        seen.append(saved.thread_id)

    supervisor.run = run
    supervisor.launch_batch(
        [replace(ticket, desktop=True), replace(ticket, thread_id="second", desktop=True)]
    )
    await asyncio.gather(*tuple(supervisor.tasks))
    assert seen == ["thread", "second"]


@pytest.mark.parametrize("stop_before_start", [False, True])
async def test_stop_cancels_batch_before_a_conversation_run_starts(migrated_db, stop_before_start):
    from unittest.mock import AsyncMock

    supervisor = ContinuationSupervisor(factory=ExecutionServer)
    ticket = await prepared(supervisor)
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed_enabled():
        entered.set()
        await release.wait()
        return True

    supervisor.enabled = delayed_enabled
    supervisor.run = AsyncMock()
    supervisor.launch_batch([ticket])
    tasks = tuple(supervisor.tasks)
    if not stop_before_start:
        await asyncio.wait_for(entered.wait(), 2)
    bus.publish("continuation.stop")
    release.set()
    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 2)
    supervisor.run.assert_not_awaited()


@pytest.mark.parametrize("scope", ["desktop", "ide"])
@pytest.mark.parametrize("stop_before_start", [False, True])
async def test_scoped_stop_before_batch_run_preserves_other_surfaces(
    migrated_db, scope, stop_before_start
):
    from unittest.mock import AsyncMock

    await SettingsRepository().set("ide_continue", "true")
    supervisor = ContinuationSupervisor(factory=ExecutionServer)
    ticket = await prepared(supervisor)
    entered, release = asyncio.Event(), asyncio.Event()
    readers = 0

    async def delayed_enabled():
        nonlocal readers
        readers += 1
        if readers == 3:
            entered.set()
        await release.wait()
        return True

    supervisor.enabled = delayed_enabled
    supervisor.run = AsyncMock()
    supervisor.launch_batch(
        [
            replace(ticket, thread_id="ide", desktop=True, owner_id="owner"),
            replace(ticket, thread_id="desktop", desktop=True),
            replace(ticket, thread_id="cli"),
        ]
    )
    tasks = tuple(supervisor.tasks)
    if not stop_before_start:
        await asyncio.wait_for(entered.wait(), 2)
    bus.publish("continuation.stop", scope=scope)
    release.set()
    await asyncio.wait_for(asyncio.gather(*tasks), 2)
    assert {call.args[0].thread_id for call in supervisor.run.await_args_list} == {
        "ide",
        "desktop",
        "cli",
    } - {scope}


async def test_stalled_ide_does_not_delay_desktop_or_cli(migrated_db, monkeypatch):
    import codex_account_manager.continuity.automation as module

    await SettingsRepository().set("ide_continue", "true")
    supervisor = ContinuationSupervisor(factory=ExecutionServer)
    ticket = await prepared(supervisor)
    ide_started = asyncio.Event()
    progressed = set()
    cancelled = asyncio.Event()
    monkeypatch.setattr(module, "NATIVE_CONTINUATION_SECONDS", 0.2)

    async def run(saved):
        if saved.owner_id:
            ide_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        else:
            await ide_started.wait()
            assert not cancelled.is_set()
            progressed.add(saved.thread_id)

    supervisor.run = run
    tickets = [
        replace(ticket, thread_id="ide", desktop=True, owner_id="owner"),
        replace(ticket, thread_id="desktop", desktop=True),
        replace(ticket, thread_id="cli"),
    ]
    await supervisor.save_pending(tickets)
    supervisor.launch_batch(tickets)
    await asyncio.wait_for(asyncio.gather(*tuple(supervisor.tasks)), 3)
    assert progressed == {"desktop", "cli"} and cancelled.is_set()
    async with connect() as db:
        assert not await (await db.execute("SELECT * FROM pending_continuations")).fetchall()


async def test_parallel_continuations_hold_account_lock_until_last_reader_exits(migrated_db):
    from codex_account_manager.core.errors import TransactionError
    from codex_account_manager.core.operation_lock import OperationLock

    supervisor = ContinuationSupervisor()
    first_entered = asyncio.Event()
    second_entered = asyncio.Event()
    release_first = asyncio.Event()
    release_second = asyncio.Event()

    async def reader(entered, release):
        with supervisor._account_session():
            entered.set()
            await release.wait()

    first = asyncio.create_task(reader(first_entered, release_first))
    second = asyncio.create_task(reader(second_entered, release_second))
    try:
        await asyncio.wait_for(asyncio.gather(first_entered.wait(), second_entered.wait()), 2)
        release_first.set()
        await first
        with pytest.raises(TransactionError):
            with OperationLock(migrated_db.data_dir / "account-operation.lock"):
                pass
        second.cancel()
        await asyncio.gather(second, return_exceptions=True)
        with OperationLock(migrated_db.data_dir / "account-operation.lock"):
            pass
        assert supervisor._account_readers == 0
    finally:
        first.cancel()
        second.cancel()
        await asyncio.gather(first, second, return_exceptions=True)


async def test_failed_load_and_long_cli_do_not_block_another_cli(migrated_db):
    supervisor = ContinuationSupervisor(factory=ExecutionServer)
    ticket = await prepared(supervisor)
    failed, long, healthy = ExecutionServer(), ExecutionServer(), ExecutionServer()

    async def cannot_load(_thread_id):
        raise OSError("conversation unavailable")

    failed.resume_for_continuation = cannot_load
    long.wait = True
    servers = iter([failed, long, healthy])
    supervisor.factory = lambda: next(servers)
    supervisor.launch_batch(
        [
            replace(ticket, thread_id="failed"),
            replace(ticket, thread_id="long"),
            replace(ticket, thread_id="healthy"),
        ]
    )
    try:
        await asyncio.wait_for(
            asyncio.gather(long.started_turn.wait(), healthy.started_turn.wait()), 3
        )
        assert not failed.turn_calls
        assert len(healthy.turn_calls) == 1
        assert supervisor.tasks
    finally:
        await supervisor.stop()
    assert supervisor._account_readers == 0


async def test_unknown_goal_after_handoff_reports_goal_stage_without_sending(migrated_db):
    server = ExecutionServer()
    supervisor = ContinuationSupervisor(factory=lambda: server)
    ticket = await prepared(supervisor)
    server.native = None
    events = []
    unsubscribe = bus.subscribe("continuation.status", lambda event: events.append(event.payload))
    try:
        await supervisor.run(ticket)
    finally:
        unsubscribe()
    assert not server.turn_calls
    assert events[-1] == {"thread_id": "thread", "state": "needs_user", "stage": "goal"}


async def test_unknown_goal_during_preparation_is_not_reported_as_no_interruption(migrated_db):
    from codex_account_manager.core.errors import AppServerError

    server = ExecutionServer()
    server.native = None
    supervisor = ContinuationSupervisor(factory=lambda: server)
    with pytest.raises(AppServerError, match="verify the goal"):
        await supervisor.prepare("thread")
    assert not server.turn_calls
    assert server.closed


async def test_ambiguous_delivery_is_not_resent_even_by_new_supervisor(migrated_db):
    server = ExecutionServer()
    server.fail_send = True
    supervisor = ContinuationSupervisor(factory=lambda: server)
    ticket = await prepared(supervisor)
    await supervisor.run(ticket)
    await ContinuationSupervisor(factory=lambda: server).run(ticket)
    assert len(server.turn_calls) == 1
    async with connect() as db:
        assert await (await db.execute("SELECT status FROM continuation_attempts")).fetchall() == [
            ("uncertain",)
        ]


@pytest.mark.parametrize("change", ["account", "turn", "pause", "clear", "disabled"])
async def test_changes_during_handoff_prevent_sending(migrated_db, change):
    server = ExecutionServer()
    supervisor = ContinuationSupervisor(factory=lambda: server)
    ticket = await prepared(supervisor)
    if change == "account":
        server.account_id = "different"
    elif change == "turn":
        server.latest = {"id": "user-new-turn", "status": "completed"}
    elif change == "disabled":
        await SettingsRepository().set("auto_continue", "false")
    else:
        await supervisor.goals.set_objective("thread", "Task")
        if change == "pause":
            await supervisor.goals.mark_status("thread", GoalState.PAUSED)
        else:
            await supervisor.goals.user_clear("thread")
    await supervisor.run(ticket)
    assert not server.turn_calls


async def test_disable_signal_cancels_running_work_and_releases_locks(migrated_db):
    from codex_account_manager.core.operation_lock import OperationLock

    server = ExecutionServer()
    server.wait = True
    supervisor = ContinuationSupervisor(factory=lambda: server)
    ticket = await prepared(supervisor)
    task = asyncio.create_task(supervisor.run(ticket))
    await asyncio.wait_for(server.started_turn.wait(), 2)
    bus.publish("continuation.stop")
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    assert server.closed
    with OperationLock(migrated_db.data_dir / "account-operation.lock"):
        pass


@pytest.mark.parametrize("owner", ["cli", "desktop", "ide"])
@pytest.mark.parametrize("scope", ["ide", "desktop"])
async def test_disabling_surface_only_cancels_its_workers(migrated_db, owner, scope):
    from codex_account_manager.core.operation_lock import OperationLock

    server = ExecutionServer()
    supervisor = ContinuationSupervisor(factory=lambda: server)
    ticket = replace(
        await prepared(supervisor),
        desktop=owner != "cli",
        owner_id="ide-owner" if owner == "ide" else None,
    )
    connected = asyncio.Event()

    async def connecting():
        connected.set()
        await asyncio.Event().wait()

    server.start = connecting
    task = asyncio.create_task(supervisor.run(ticket))
    try:
        await asyncio.wait_for(connected.wait(), 2)
        bus.publish("continuation.stop", scope=scope)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        if owner != scope:
            assert not task.done() and not task.cancelling()
            bus.publish("continuation.stop")
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert server.closed and supervisor._account_readers == 0
    with OperationLock(migrated_db.data_dir / "account-operation.lock"):
        pass


@pytest.mark.parametrize("surface", ["ide", "desktop"])
async def test_disabled_surface_drops_only_its_pending_work_while_account_is_unavailable(
    migrated_db, surface
):
    server = ExecutionServer()
    supervisor = ContinuationSupervisor(factory=lambda: server)
    ticket = await prepared(supervisor)
    await supervisor.save_pending(
        [
            ticket,
            replace(
                ticket,
                thread_id=surface,
                desktop=True,
                owner_id="ide-owner" if surface == "ide" else None,
            ),
        ]
    )
    await SettingsRepository().set(surface + "_continue", "false")
    await supervisor.recover_pending(None)
    assert not supervisor.tasks and not server.turn_calls
    async with connect() as db:
        assert await (
            await db.execute("SELECT thread_id FROM pending_continuations")
        ).fetchall() == [("thread",)]


async def test_two_workers_cannot_send_the_same_turn(migrated_db):
    server = ExecutionServer()
    server.wait = True
    first = ContinuationSupervisor(factory=lambda: server)
    second = ContinuationSupervisor(factory=lambda: server)
    ticket = await prepared(first)
    task = asyncio.create_task(first.run(ticket))
    try:
        await asyncio.wait_for(server.started_turn.wait(), 2)
        await second.run(ticket)
        assert len(server.turn_calls) == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_account_lock_is_held_until_execution_connection_closes(migrated_db):
    from codex_account_manager.core.errors import TransactionError
    from codex_account_manager.core.operation_lock import OperationLock

    server = ExecutionServer()
    supervisor = ContinuationSupervisor(factory=lambda: server)
    ticket = await prepared(supervisor)
    closed_with_lock = []

    async def close():
        with pytest.raises(TransactionError):
            with OperationLock(migrated_db.data_dir / "account-operation.lock"):
                pass
        closed_with_lock.append(True)

    server.aclose = close
    await supervisor.run(ticket)
    assert closed_with_lock == [True]
    with OperationLock(migrated_db.data_dir / "account-operation.lock"):
        pass


async def test_desktop_preparation_never_claims_or_resumes_conversation(migrated_db):
    from codex_account_manager.core.errors import DesktopContinuationRequired

    server = ExecutionServer()

    async def desktop_only(_thread_id):
        raise DesktopContinuationRequired("Continue in Desktop")

    server.require_headless_compatible = desktop_only
    supervisor = ContinuationSupervisor(factory=lambda: server)
    from unittest.mock import AsyncMock

    server.is_desktop_thread = AsyncMock(return_value=True)
    supervisor.native.latest_turn = AsyncMock(return_value=server.latest)
    ticket = await supervisor.prepare("thread")
    assert ticket is not None and ticket.desktop
    from codex_account_manager.storage.database import connect

    async with connect() as db:
        row = await (await db.execute("SELECT COUNT(*) FROM continuation_attempts")).fetchone()
    assert row[0] == 0
    assert not server.resume_calls
    assert not server.turn_calls
    assert server.closed
