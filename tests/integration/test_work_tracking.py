from dataclasses import replace

from codex_account_manager.adapters.interfaces import GoalInfo
from codex_account_manager.continuity.service import ContinuityService
from codex_account_manager.continuity.tracking import WorkTracker
from tests.fakes import FakeAppServer
from tests.integration.test_limit_continuation import _limited_turn, _thread


async def test_two_desktop_threads_capture_fast_quota_turn_between_polls(migrated_db, monkeypatch):
    from unittest.mock import AsyncMock

    import codex_account_manager.continuity.service as module

    server = FakeAppServer(
        account_id="account-a",
        threads=[replace(_thread(tid, 100), source="vscode") for tid in ("one", "two")],
        turns={
            "one": {"id": "running", "status": "inProgress"},
            "two": {"id": "previous", "status": "completed"},
        },
    )
    server.is_desktop_thread = AsyncMock(return_value=True)
    monkeypatch.setattr(module, "CodexAppServer", lambda *_args, **_kwargs: server)
    service = ContinuityService()
    service.automation.native.latest_turn = AsyncMock(side_effect=server.latest_turn)
    await service.observe_work()
    server._turns.update(
        one={**_limited_turn(), "id": "running"},
        two={**_limited_turn(), "id": "fast-failure"},
    )
    await service.observe_work()
    assert {row[0] for row in await service.tracker.limited("account-a")} == {"one", "two"}
    await service.observe_work()
    assert {row[0] for row in await service.tracker.limited("account-a")} == {"one", "two"}


async def test_new_failed_turn_after_restart_preserves_observed_work(migrated_db):
    tracker = WorkTracker()
    goal = GoalInfo("thread", None, None, False)
    await tracker.observe("account", "thread", {"id": "running", "status": "inProgress"}, goal)
    restarted = WorkTracker()
    await restarted.observe("account", "thread", _limited_turn(), goal, limited=True)
    assert (await restarted.limited("account"))[0][1] == "limited-turn"


async def test_old_quota_failure_is_not_adopted_by_another_account(migrated_db):
    tracker = WorkTracker()
    goal = GoalInfo("thread", None, None, False)
    await tracker.observe("previous", "thread", {"id": "running", "status": "inProgress"}, goal)
    await tracker.observe("new", "thread", _limited_turn(), goal, limited=True)
    assert not await tracker.limited("new")


async def test_completed_work_is_retired_after_account_switch(migrated_db):
    tracker = WorkTracker()
    goal = GoalInfo("thread", None, None, False)
    await tracker.observe("previous", "thread", {"id": "turn", "status": "inProgress"}, goal)
    await tracker.observe("new", "thread", {"id": "turn", "status": "completed"}, goal)
    assert not await tracker.visible()


async def test_running_work_survives_restart_and_remains_tracked_outside_recent_twenty(
    migrated_db, monkeypatch
):
    import codex_account_manager.continuity.service as module

    server = FakeAppServer(
        account_id="account-a",
        threads=[_thread("working", 100)],
        turns={"working": {"id": "limited-turn", "status": "inProgress"}},
    )
    monkeypatch.setattr(module, "CodexAppServer", lambda _, **kwargs: server)
    await ContinuityService().observe_work()
    assert await WorkTracker().ids("account-a") == ["working"]
    server._threads.extend(_thread(str(i), 1000 + i) for i in range(25))
    server._turns["working"] = _limited_turn()
    restarted = ContinuityService()
    await restarted.observe_work()
    assert (await restarted.tracker.limited("account-a"))[0][1] == "limited-turn"


async def test_new_turn_invalidates_saved_interruption(migrated_db):
    tracker = WorkTracker()
    absent = GoalInfo("thread", None, None, False)
    await tracker.observe(
        "account-a", "thread", {"id": "limited-turn", "status": "inProgress"}, absent
    )
    await tracker.observe("account-a", "thread", _limited_turn(), absent, limited=True)
    assert await tracker.limited("account-a")
    await tracker.observe("account-a", "thread", {"id": "user", "status": "completed"}, absent)
    assert not await tracker.limited("account-a")


async def test_unverifiable_goal_keeps_checkpoint_for_later_revalidation(migrated_db, monkeypatch):
    import codex_account_manager.continuity.service as module

    tracker = WorkTracker()
    absent = GoalInfo("thread", None, None, False)
    await tracker.observe(
        "account-a", "thread", {"id": "limited-turn", "status": "inProgress"}, absent
    )
    await tracker.observe("account-a", "thread", _limited_turn(), absent, limited=True)
    server = FakeAppServer(
        account_id="account-a", threads=[_thread("thread", 100)], turns={"thread": _limited_turn()}
    )
    server.get_goal = lambda _thread_id: _unknown_goal()
    monkeypatch.setattr(module, "CodexAppServer", lambda _, **kwargs: server)
    await ContinuityService().observe_work()
    assert not await tracker.limited("account-a")
    saved = await tracker.visible()
    assert len(saved) == 1 and saved[0].thread_id == "thread" and not saved[0].verified


async def _unknown_goal():
    return None


async def test_deleted_conversations_are_removed_only_after_successful_listing(
    migrated_db, monkeypatch
):
    import pytest

    import codex_account_manager.continuity.service as module

    tracker = WorkTracker()
    await tracker.observe(
        "account-a",
        "deleted",
        {"id": "turn", "status": "inProgress"},
        GoalInfo("deleted", None, None, False),
    )
    server = FakeAppServer(account_id="account-a", threads=[])
    monkeypatch.setattr(module, "CodexAppServer", lambda _, **kwargs: server)

    async def broken_list(**_kwargs):
        raise RuntimeError("offline")

    service = ContinuityService()
    original = server.list_threads
    server.list_threads = broken_list
    with pytest.raises(RuntimeError, match="offline"):
        await service.observe_work()
    saved = await tracker.visible()
    assert len(saved) == 1 and not saved[0].verified
    server.list_threads = original
    await service.observe_work()
    assert await tracker.visible() == []


async def test_reset_pauses_monitoring_and_invalidates_in_flight_observation(migrated_db):
    from codex_account_manager.storage.repositories import SettingsRepository

    tracker = WorkTracker()
    old_generation = tracker.generation
    goal = GoalInfo("work", None, None, False)
    await tracker.observe("account", "work", {"id": "turn", "status": "inProgress"}, goal)
    await tracker.reset()
    await tracker.observe(
        "account", "work", {"id": "turn", "status": "inProgress"}, goal, generation=old_generation
    )
    assert await tracker.visible() == []
    assert await SettingsRepository().get("monitor_enabled") == "false"


async def test_saved_observation_expires_without_losing_continuation_checkpoint(migrated_db):
    import time

    from codex_account_manager.storage.database import connect

    tracker = WorkTracker()
    goal = GoalInfo("work", None, None, False)
    await tracker.observe("account", "work", {"id": "limited-turn", "status": "inProgress"}, goal)
    await tracker.observe("account", "work", _limited_turn(), goal, limited=True)
    async with connect() as db:
        await db.execute("UPDATE observed_work SET observed_at=?", (time.time() - 240,))
        await db.commit()
    saved = await tracker.visible()
    assert saved[0].limited and not saved[0].verified
    assert await tracker.limited("account")


async def test_multiple_interrupted_conversations_survive_restart_without_content(migrated_db):
    from codex_account_manager.storage.database import connect

    tracker = WorkTracker()
    for tid in ("one", "two"):
        goal = GoalInfo(tid, "private objective", "usageLimited", True, 500, 10)
        await tracker.observe(
            "account-a", tid, {"id": "limited-turn", "status": "inProgress"}, goal
        )
        await tracker.observe("account-a", tid, _limited_turn(), goal, limited=True)
    assert {row[0] for row in await WorkTracker().limited("account-a")} == {"one", "two"}
    async with connect() as db:
        rows = await (await db.execute("SELECT goal_fingerprint FROM observed_work")).fetchall()
    assert all("private objective" not in row[0] for row in rows)
    display = await tracker.visible()
    assert len(display) == 2
    assert all(work.goal_status == "usageLimited" and work.goal_present for work in display)


async def test_historical_limit_without_observed_running_turn_is_not_selected(migrated_db):
    tracker = WorkTracker()
    absent = GoalInfo("old", None, None, False)
    await tracker.observe("account-a", "old", _limited_turn(), absent, limited=True)
    assert not await tracker.limited("account-a")


async def test_ide_new_failed_turn_between_polls_is_tracked_but_history_is_not(
    migrated_db, monkeypatch
):
    from unittest.mock import AsyncMock

    import codex_account_manager.continuity.service as module
    from codex_account_manager.adapters.native_ide import OwnerSnapshot
    from codex_account_manager.storage.repositories import SettingsRepository

    await SettingsRepository().set("ide_continue", "true")
    info = replace(_thread("ide", 100), source="vscode")
    server = FakeAppServer(account_id="account-a", threads=[info], turns={"ide": _limited_turn()})
    server.is_desktop_thread = AsyncMock(return_value=False)
    monkeypatch.setattr(module, "CodexAppServer", lambda *_a, **_kw: server)
    service = ContinuityService()
    owner = OwnerSnapshot("owner", {"id": "limited-turn", "status": "failed"}, False, None)
    service.automation.ide.inspect = AsyncMock(return_value=owner)
    await service.observe_work()
    assert not await service.tracker.limited("account-a")
    server._turns["ide"] = {**_limited_turn(), "id": "new-short-turn"}
    service.automation.ide.inspect.return_value = replace(
        owner, turn={"id": "new-short-turn", "status": "failed"}
    )
    await service.observe_work()
    assert (await service.tracker.limited("account-a"))[0][1] == "new-short-turn"
    await service.tracker.reset()
    server._turns["ide"] = {**_limited_turn(), "id": "after-reset"}
    service.automation.ide.inspect.return_value = replace(
        owner, turn={"id": "after-reset", "status": "failed"}
    )
    await service.observe_work()
    assert not await service.tracker.limited("account-a")


async def test_saved_work_cannot_cross_account_or_survive_new_goal(migrated_db):
    tracker = WorkTracker()
    old_goal = GoalInfo("thread", "Original", "active", True, 500, 10)
    new_goal = GoalInfo("thread", "Replacement", "active", True, 500, 10)
    await tracker.observe(
        "account-a", "thread", {"id": "limited-turn", "status": "inProgress"}, old_goal
    )
    await tracker.observe("account-b", "thread", _limited_turn(), old_goal, limited=True)
    assert not await tracker.limited("account-b")
    await tracker.observe("account-a", "thread", _limited_turn(), new_goal, limited=True)
    assert not await tracker.limited("account-a")


async def test_consumed_interruption_is_not_reused(migrated_db):
    tracker = WorkTracker()
    absent = GoalInfo("thread", None, None, False)
    await tracker.observe(
        "account-a", "thread", {"id": "limited-turn", "status": "inProgress"}, absent
    )
    await tracker.observe("account-a", "thread", _limited_turn(), absent, limited=True)
    assert await tracker.limited("account-a")
    await tracker.clear("account-a", ["thread"])
    assert not await tracker.limited("account-a")


async def test_desktop_handoff_keeps_tracking_under_new_account_without_reusing_old_limit(
    migrated_db,
):
    tracker = WorkTracker()
    absent = GoalInfo("thread", None, None, False)
    await tracker.observe(
        "old-account", "thread", {"id": "limited-turn", "status": "inProgress"}, absent
    )
    await tracker.observe("old-account", "thread", _limited_turn(), absent, limited=True)
    await tracker.await_desktop("old-account", "new-account", ["thread"])
    pending = await tracker.visible()
    assert pending[0].turn_status == "awaitingDesktop"
    assert await tracker.ids("new-account") == ["thread"]
    assert not await tracker.limited("new-account")
    assert not await tracker.limited("old-account")
    await tracker.unverified(["thread"])
    await tracker.observe("new-account", "thread", _limited_turn(), absent, limited=True)
    refreshed = (await tracker.visible())[0]
    assert refreshed.turn_status == "awaitingDesktop" and refreshed.verified
    assert not await tracker.limited("new-account")
    await tracker.observe("new-account", "thread", {"id": "next", "status": "inProgress"}, absent)
    await tracker.observe(
        "new-account", "thread", {**_limited_turn(), "id": "next"}, absent, limited=True
    )
    assert (await tracker.limited("new-account"))[0][1] == "next"


async def test_desktop_followup_cannot_transfer_another_accounts_work(migrated_db):
    tracker = WorkTracker()
    absent = GoalInfo("thread", None, None, False)
    await tracker.observe("owner", "thread", {"id": "limited-turn", "status": "inProgress"}, absent)
    await tracker.observe("owner", "thread", _limited_turn(), absent, limited=True)
    await tracker.await_desktop("unrelated", "target", ["thread"])
    assert await tracker.limited("owner")
    assert not await tracker.ids("target")


async def test_stalled_conversation_does_not_hide_running_work(migrated_db, monkeypatch):
    import asyncio

    import codex_account_manager.continuity.service as module

    server = FakeAppServer(
        account_id="account-a", threads=[_thread("slow", 200), _thread("live", 100)]
    )
    cancelled = asyncio.Event()

    async def observed(thread_id):
        if thread_id == "slow":
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        return {"id": "live-turn", "status": "inProgress"}

    server.observed_turn = observed
    monkeypatch.setattr(module, "CodexAppServer", lambda _, **kwargs: server)
    monkeypatch.setattr(module, "THREAD_CHECK_TIMEOUT", 1)
    statuses = []
    unsubscribe = module.bus.subscribe(
        "work.observation_status", lambda event: statuses.append(event.payload)
    )
    try:
        assert await ContinuityService().observe_work() == "account-a"
    finally:
        unsubscribe()
    assert cancelled.is_set()
    assert await WorkTracker().ids("account-a") == ["live"]
    assert statuses[-1]["state"] == "partial"
    assert statuses[-1]["checked"] == 1
    assert statuses[-1]["unavailable"] == 1


async def test_scan_deadline_preserves_results_and_cancels_pending_reads(migrated_db, monkeypatch):
    import asyncio

    import codex_account_manager.continuity.service as module

    server = FakeAppServer(
        account_id="account-a", threads=[_thread("live", 200), _thread("slow", 100)]
    )
    pending = set()

    async def observed(thread_id):
        if thread_id == "slow":
            pending.add(thread_id)
            try:
                await asyncio.Event().wait()
            finally:
                pending.remove(thread_id)
        return {"id": "live-turn", "status": "inProgress"}

    server.observed_turn = observed
    monkeypatch.setattr(module, "CodexAppServer", lambda _, **kwargs: server)
    monkeypatch.setattr(module, "THREAD_SCAN_TIMEOUT", 1)
    monkeypatch.setattr(module, "THREAD_CHECK_TIMEOUT", 10)
    assert await ContinuityService().observe_work() == "account-a"
    assert not pending
    assert await WorkTracker().ids("account-a") == ["live"]


async def test_desktop_limit_keeps_running_checkpoint_when_summary_omits_error_code(
    migrated_db, monkeypatch
):
    from unittest.mock import AsyncMock

    import codex_account_manager.continuity.service as module

    thread = replace(_thread("desktop", 100), source="vscode")
    server = FakeAppServer(account_id="owner", threads=[thread])
    monkeypatch.setattr(module, "CodexAppServer", lambda _, **kwargs: server)
    service = ContinuityService()
    running = {"id": "same-turn", "status": "inProgress"}
    server.is_desktop_thread = AsyncMock(return_value=True)
    service.automation.native.latest_turn = AsyncMock(return_value=running)
    await service.observe_work()
    assert await service.tracker.ids("owner") == ["desktop"]
    server._turns["desktop"] = {**_limited_turn(), "id": "same-turn"}
    service.automation.native.latest_turn.return_value = {
        "id": "same-turn",
        "status": "failed",
        "error": {"message": "Localized quota error"},
    }
    await service.observe_work()
    assert (await service.tracker.limited("owner"))[0][1] == "same-turn"


async def test_unsupported_sources_are_counted_without_native_or_turn_queries(
    migrated_db, monkeypatch
):
    from unittest.mock import AsyncMock

    import codex_account_manager.continuity.service as module
    from codex_account_manager.core.events import bus

    server = FakeAppServer(threads=[replace(_thread("unknown", 100), source="future-client")])
    server.observed_turn = AsyncMock()
    monkeypatch.setattr(module, "CodexAppServer", lambda *args, **kwargs: server)
    service = ContinuityService()
    service.automation.native.latest_turn = AsyncMock()
    events = []
    unsubscribe = bus.subscribe(
        "work.observation_status", lambda event: events.append(event.payload)
    )
    try:
        await service.observe_work()
    finally:
        unsubscribe()
    assert events[-1]["unsupported"] == 1
    assert events[-1]["unavailable"] == 0
    server.observed_turn.assert_not_awaited()
    service.automation.native.latest_turn.assert_not_awaited()


async def test_desktop_source_requires_native_proof_before_preparing_ticket(migrated_db):
    from unittest.mock import AsyncMock

    import pytest

    from codex_account_manager.continuity.automation import ContinuationSupervisor
    from codex_account_manager.core.errors import AppServerError, DesktopContinuationRequired

    server = FakeAppServer(turns={"t": _limited_turn()})
    server.is_desktop_thread = AsyncMock(return_value=True)
    server.require_headless_compatible = AsyncMock(
        side_effect=DesktopContinuationRequired("external")
    )
    supervisor = ContinuationSupervisor(factory=lambda: server)
    supervisor.native.latest_turn = AsyncMock(side_effect=AppServerError("not in Desktop"))
    with pytest.raises(AppServerError, match="not in Desktop"):
        await supervisor.prepare("t")
    assert server.closed
    assert not server.resume_calls
    supervisor.native.latest_turn = AsyncMock(return_value=_limited_turn())
    ticket = await supervisor.prepare("t")
    assert ticket and ticket.desktop and ticket.turn_id == "limited-turn"


async def test_support_probe_is_read_only_and_does_not_treat_ide_as_desktop(
    migrated_db, monkeypatch
):
    from unittest.mock import AsyncMock

    import codex_account_manager.continuity.service as module
    from codex_account_manager.core.errors import AppServerError

    server = FakeAppServer(threads=[replace(_thread("external", 100), source="vscode")])
    monkeypatch.setattr(module, "CodexAppServer", lambda *args, **kwargs: server)
    service = ContinuityService()
    service.automation.native.latest_turn = AsyncMock(
        side_effect=AppServerError("no native connection")
    )
    assert await service.continuation_support("external") == "unverified"
    from codex_account_manager.core.errors import LocalResponseTooLargeError

    service.automation.native.latest_turn = AsyncMock(
        side_effect=LocalResponseTooLargeError("size")
    )
    assert await service.continuation_support("external") == "response_too_large"
    service.automation.native.latest_turn = AsyncMock(
        return_value={"id": "last", "status": "completed"}
    )
    assert await service.continuation_support("external") == "desktop"
    server._threads = [replace(_thread("external", 100), source="unknown")]
    service.automation.native.latest_turn.reset_mock()
    assert await service.continuation_support("external") == "unsupported"
    service.automation.native.latest_turn.assert_not_awaited()
    assert not server.resume_calls and not server.set_goal_calls and server.closed


async def test_failed_recheck_excludes_old_limited_checkpoint_until_verified(migrated_db):
    tracker = WorkTracker()
    goal = GoalInfo("thread", None, None, False)
    await tracker.observe("account", "thread", {"id": "limited-turn", "status": "inProgress"}, goal)
    await tracker.observe("account", "thread", _limited_turn(), goal, limited=True)
    assert len(await tracker.limited("account")) == 1
    await tracker.unverified(["thread"])
    assert not await tracker.limited("account")
    assert len(await tracker.visible()) == 1
    await tracker.observe("account", "thread", _limited_turn(), goal, limited=True)
    assert len(await tracker.limited("account")) == 1
