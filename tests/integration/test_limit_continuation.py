"""Limit-driven conversation continuation, end-to-end with fakes."""

from __future__ import annotations

import pytest

from codex_account_manager.accounts.service import AccountService
from codex_account_manager.adapters.credential_store import FileCredentialStore
from codex_account_manager.adapters.interfaces import GoalInfo
from codex_account_manager.auth.transaction import AuthTransaction
from codex_account_manager.continuity.service import ContinuityService
from codex_account_manager.continuity.tracking import WorkTracker
from codex_account_manager.domain.models import ThreadInfo
from codex_account_manager.goals.service import GoalService
from tests.fakes import FakeAppServer, FakeDesktop


def _factory(account_id: str):
    def make(_home: str) -> FakeAppServer:
        return FakeAppServer(account_id=account_id)

    return make


def _thread(tid: str, recency: int, preview: str = "hello") -> ThreadInfo:
    return ThreadInfo(
        id=tid,
        preview=preview,
        cwd="C:/repo",
        path=None,
        model="gpt",
        reasoning_effort=None,
        created_at=1,
        updated_at=recency,
        recency_at=recency,
        status="notLoaded",
    )


def _limited_turn() -> dict:
    return {
        "id": "limited-turn",
        "status": "failed",
        "error": {"codexErrorInfo": "usageLimitExceeded"},
    }


async def _observed_running(thread_id: str, account_id: str = "acc-1") -> None:
    await WorkTracker().observe(
        account_id,
        thread_id,
        {"id": "limited-turn", "status": "inProgress"},
        GoalInfo(thread_id, None, None, False),
    )


async def _seed_two_profiles(migrated_db, accounts: AccountService):
    p1 = await accounts.create_profile("ana")
    p2 = await accounts.create_profile("hesap2")
    for p, acc in ((p1, "acc-1"), (p2, "acc-2")):
        (migrated_db.profiles_dir / p.id / "auth.json").write_bytes(b"{}")
        await accounts.profiles.bind_account(p.id, acc)
    return p1, p2


@pytest.mark.parametrize("block", [None, "monitor", "capacity", "claimed", "turn"])
async def test_explicit_limit_recovery_checks_capacity_and_durable_claim(migrated_db, block):
    from codex_account_manager.core.errors import AppServerError
    from codex_account_manager.storage.database import connect
    from codex_account_manager.storage.repositories import SettingsRepository
    from tests.fakes import ExecutionServer

    accounts = AccountService(
        app_server_factory=lambda _: FakeAppServer(
            account_id="acc-2", ordinary_usage_allowed=block != "capacity"
        )
    )
    await _seed_two_profiles(migrated_db, accounts)
    service = ContinuityService(accounts=accounts)
    server = ExecutionServer()
    service.automation.factory = lambda: server
    if block == "monitor":
        await SettingsRepository().set("monitor_enabled", "false")
    elif block == "claimed":
        ticket = await service.automation.prepare("thread")
        assert await service.automation._claim(ticket)
    elif block == "turn":
        server.latest = {"id": "user-stopped", "status": "interrupted"}
    if block:
        with pytest.raises(AppServerError):
            await service.queue_verified_continuation("thread")
    else:
        await service.queue_verified_continuation("thread")
    async with connect() as db:
        rows = await (await db.execute("SELECT account_id FROM pending_continuations")).fetchall()
        assert rows == ([] if block else [("acc-2",)])
    assert not server.turn_calls


@pytest.mark.parametrize("state", ["inProgress", "unavailable"])
async def test_quota_failover_never_stops_desktop_before_work_is_safe(
    migrated_db, monkeypatch, state
):
    from dataclasses import replace
    from unittest.mock import AsyncMock

    import codex_account_manager.continuity.service as cs

    accounts = AccountService(app_server_factory=_factory("acc-2"))
    await _seed_two_profiles(migrated_db, accounts)
    fake = FakeAppServer(
        threads=[replace(_thread("still-working", 999), source="vscode")],
        turns={"still-working": {"id": "limited-turn", "status": "inProgress"}},
    )
    if state == "unavailable":

        async def unavailable(_thread):
            raise OSError("channel unavailable")

    monkeypatch.setattr(cs, "CodexAppServer", lambda *_args, **_kwargs: fake)
    service = ContinuityService(accounts=accounts)
    fake.is_desktop_thread = AsyncMock(return_value=True)
    service.automation.native.latest_turn = (
        unavailable
        if state == "unavailable"
        else AsyncMock(return_value=fake._turns["still-working"])
    )
    await _observed_running("still-working")
    desktop = FakeDesktop()
    store = FileCredentialStore(shared_home=migrated_db.shared_codex_home)
    store.write_active_atomic(b"original")

    async def verify(_home):
        return "acc-2"

    tx = AuthTransaction(credential_store=store, desktop=desktop, verify_account=verify)
    from codex_account_manager.core.errors import HandoffDeferredError

    with pytest.raises(HandoffDeferredError):
        await service.continue_on_limit("hesap2", transaction=tx)
    assert not desktop.stopped
    assert store.read_active() == b"original"
    assert not await service.recent_handoffs()


@pytest.mark.parametrize("change", ["preparation", "target_verification", "unavailable", "paused"])
async def test_last_restart_check_preserves_work_started_during_handoff(
    migrated_db, monkeypatch, change
):
    from dataclasses import replace
    from pathlib import Path
    from unittest.mock import AsyncMock

    import codex_account_manager.continuity.service as cs
    from codex_account_manager.core.errors import DesktopContinuationRequired, HandoffDeferredError
    from codex_account_manager.storage.database import connect
    from codex_account_manager.storage.repositories import SettingsRepository

    accounts = AccountService(app_server_factory=_factory("acc-2"))
    await _seed_two_profiles(migrated_db, accounts)
    fake = FakeAppServer(
        threads=[replace(_thread("desktop", 999), source="vscode")],
        turns={"desktop": _limited_turn()},
    )
    monkeypatch.setattr(cs, "CodexAppServer", lambda *_args, **_kwargs: fake)
    fake.is_desktop_thread = AsyncMock(return_value=True)
    fake.require_headless_compatible = AsyncMock(side_effect=DesktopContinuationRequired("local"))
    service = ContinuityService(accounts=accounts)
    service.automation.factory = lambda: fake
    turn = _limited_turn()

    async def latest(_thread):
        return turn

    service.automation.native.latest_turn = latest
    await _observed_running("desktop")
    prepare = service.automation.prepare

    async def changed_prepare(thread_id):
        nonlocal turn
        if change == "preparation":
            turn = {"id": "new-user-turn", "status": "inProgress"}
        ticket = await prepare(thread_id)
        if change == "unavailable":
            service.automation.native.latest_turn = AsyncMock(side_effect=OSError("disconnected"))
        if change == "paused":
            await SettingsRepository().set("monitor_enabled", "false")
        return ticket

    service.automation.prepare = changed_prepare
    desktop = FakeDesktop()
    store = FileCredentialStore(shared_home=migrated_db.shared_codex_home)
    source_auth = b'{"tokens":{"account_id":"acc-1"}}'
    store.write_active_atomic(source_auth)
    tx = AuthTransaction(credential_store=store, desktop=desktop)
    monkeypatch.setattr(AccountService, "_active_account_id_safe", AsyncMock(return_value="acc-1"))

    async def verify(home, *, force_refresh=False):
        nonlocal turn
        assert Path(home) != migrated_db.shared_codex_home
        if change == "target_verification":
            turn = {"id": "new-user-turn", "status": "inProgress"}
        return "acc-2"

    tx._verify = verify
    with pytest.raises(HandoffDeferredError):
        await service.continue_on_limit("hesap2", transaction=tx)
    assert desktop.stopped == desktop.launched == 0
    assert store.read_active() == source_auth
    assert not tx.recovery_path.exists()
    assert not service.automation.tasks
    async with connect() as db:
        assert not await (await db.execute("SELECT * FROM pending_continuations")).fetchall()
        assert not await (await db.execute("SELECT * FROM continuation_attempts")).fetchall()


async def test_tracking_picks_verified_limit_over_newer_unrelated_chat(migrated_db, monkeypatch):
    import codex_account_manager.continuity.service as cs

    fake = FakeAppServer(
        threads=[_thread("old", 100), _thread("new", 999), _thread("mid", 500)],
        turns={"old": _limited_turn(), "new": {"id": "ordinary", "status": "completed"}},
    )
    options = {}

    def factory(_home, **kwargs):
        options.update(kwargs)
        return fake

    monkeypatch.setattr(cs, "CodexAppServer", factory)
    await _observed_running("old")

    await ContinuityService().observe_work()
    assert (await WorkTracker().limited("acc-1"))[0][0] == "old"
    assert options == {"experimental": True}


async def test_tracking_skips_unverified_conversations(migrated_db, monkeypatch):
    import codex_account_manager.continuity.service as cs

    fake = FakeAppServer(
        threads=[_thread("recent", 999)],
        turns={"recent": {"id": "ordinary", "status": "completed"}},
    )
    monkeypatch.setattr(cs, "CodexAppServer", lambda home, **kwargs: fake)

    await ContinuityService().observe_work()
    assert not await WorkTracker().limited("acc-1")


@pytest.mark.parametrize("desktop_owned", [False, True])
async def test_continue_on_limit_carries_active_conversation(
    migrated_db, monkeypatch, desktop_owned
):
    accounts = AccountService(app_server_factory=_factory("acc-2"))
    await _seed_two_profiles(migrated_db, accounts)

    store = FileCredentialStore(shared_home=migrated_db.shared_codex_home)
    store.write_active_atomic(b"acc-1-auth")

    import codex_account_manager.continuity.service as cs

    # The shared-home adapter reports one active thread with no native goal.
    fake = FakeAppServer(
        account_id="acc-2",
        threads=[_thread("t-live", 999, preview="Refactor the parser")],
        turns={"t-live": _limited_turn()},
        goals={"t-live": None},
    )
    monkeypatch.setattr(cs, "CodexAppServer", lambda home, **kwargs: fake)

    async def verify(_home: str) -> str:
        return "acc-2"

    tx = AuthTransaction(credential_store=store, desktop=FakeDesktop(), verify_account=verify)
    service = ContinuityService(accounts=accounts, goals=GoalService())
    service.automation.factory = lambda: fake
    if desktop_owned:
        from codex_account_manager.core.errors import DesktopContinuationRequired

        async def desktop_only(_thread_id):
            raise DesktopContinuationRequired("Continue in Desktop")

        fake.require_headless_compatible = desktop_only
        from unittest.mock import AsyncMock

        fake.is_desktop_thread = AsyncMock(return_value=True)

        service.automation.native.latest_turn = AsyncMock(return_value=_limited_turn())
    launched = []
    service.automation.launch_batch = launched.extend
    await _observed_running("t-live", "acc-2")

    result = await service.continue_on_limit("hesap2", transaction=tx)

    assert result.success is True
    if desktop_owned:
        assert not fake.resume_calls
        assert len(launched) == 1 and launched[0].desktop
        assert (await service.tracker.visible())[0].turn_status == "awaitingDesktop"
        assert not await service.tracker.limited("acc-2")
    else:
        assert not fake.resume_calls
        assert len(launched) == 1 and launched[0].thread_id == "t-live"
    # A preview is not an instruction to create a goal.
    assert fake.set_goal_calls == []
    # New profile's auth is active.
    assert store.read_active() == b"{}"


async def test_continue_on_limit_without_any_thread(migrated_db, monkeypatch):
    accounts = AccountService(app_server_factory=_factory("acc-2"))
    await _seed_two_profiles(migrated_db, accounts)
    store = FileCredentialStore(shared_home=migrated_db.shared_codex_home)
    store.write_active_atomic(b"acc-1-auth")

    import codex_account_manager.continuity.service as cs

    fake = FakeAppServer(account_id="acc-2", threads=[])  # no threads at all
    monkeypatch.setattr(cs, "CodexAppServer", lambda home, **kwargs: fake)

    async def verify(_home: str) -> str:
        return "acc-2"

    tx = AuthTransaction(credential_store=store, desktop=FakeDesktop(), verify_account=verify)
    service = ContinuityService(accounts=accounts, goals=GoalService())

    # Should still switch cleanly even with nothing to carry forward.
    result = await service.continue_on_limit("hesap2", transaction=tx)
    assert result.success is True
    assert store.read_active() == b"{}"


async def test_automatic_work_starts_only_after_committed_handoff(migrated_db, monkeypatch):
    import asyncio

    import codex_account_manager.continuity.service as cs
    from codex_account_manager.continuity.automation import ContinuationSupervisor
    from tests.fakes import ExecutionServer

    accounts = AccountService(app_server_factory=_factory("acc-2"))
    await _seed_two_profiles(migrated_db, accounts)
    store = FileCredentialStore(shared_home=migrated_db.shared_codex_home)
    store.write_active_atomic(b"previous")
    fake = FakeAppServer(threads=[_thread("thread", 999)], turns={"thread": _limited_turn()})
    monkeypatch.setattr(cs, "CodexAppServer", lambda _home, **kwargs: fake)
    execution = ExecutionServer()
    execution.account_id = "acc-2"
    execution.latest = _limited_turn()
    service = ContinuityService(accounts=accounts)
    service.automation = ContinuationSupervisor(factory=lambda: execution)
    await _observed_running("thread")

    async def cannot_clear(_account_id, _thread_ids):
        raise OSError("checkpoint storage unavailable")

    service.tracker.clear = cannot_clear
    original = execution.run_continuation_turn

    async def after_commit(*args, **kwargs):
        assert (await service.recent_handoffs())[0].success is True
        assert store.read_active() == b"{}"
        return await original(*args, **kwargs)

    execution.run_continuation_turn = after_commit

    async def verify(_home):
        return "acc-2"

    tx = AuthTransaction(credential_store=store, desktop=FakeDesktop(), verify_account=verify)
    await service.continue_on_limit("hesap2", transaction=tx)
    await asyncio.gather(*tuple(service.automation.tasks))
    assert len(execution.turn_calls) == 1


async def test_failed_handoff_never_launches_model_work(migrated_db, monkeypatch):
    import pytest

    import codex_account_manager.continuity.service as cs
    from codex_account_manager.continuity.automation import ContinuationSupervisor
    from tests.fakes import ExecutionServer

    accounts = AccountService(app_server_factory=_factory("acc-2"))
    await _seed_two_profiles(migrated_db, accounts)
    monkeypatch.setattr(
        cs,
        "CodexAppServer",
        lambda _home, **kwargs: FakeAppServer(
            threads=[_thread("thread", 999)], turns={"thread": _limited_turn()}
        ),
    )
    execution = ExecutionServer()
    execution.latest = _limited_turn()
    service = ContinuityService(accounts=accounts)
    service.automation = ContinuationSupervisor(factory=lambda: execution)
    await _observed_running("thread")

    class FailedTransaction:
        async def switch(self, *args, **kwargs):
            raise RuntimeError("switch failed")

    with pytest.raises(RuntimeError, match="switch failed"):
        await service.continue_on_limit("hesap2", transaction=FailedTransaction())
    assert not service.automation.tasks
    assert not execution.turn_calls


async def test_conversation_load_failure_does_not_undo_verified_account_switch(
    migrated_db, monkeypatch
):
    import asyncio

    import codex_account_manager.continuity.service as cs
    from codex_account_manager.continuity.automation import ContinuationSupervisor
    from tests.fakes import ExecutionServer

    accounts = AccountService(app_server_factory=_factory("acc-2"))
    await _seed_two_profiles(migrated_db, accounts)
    store = FileCredentialStore(shared_home=migrated_db.shared_codex_home)
    store.write_active_atomic(b"previous")
    monkeypatch.setattr(
        cs,
        "CodexAppServer",
        lambda _home, **kwargs: FakeAppServer(
            threads=[_thread("thread", 999)], turns={"thread": _limited_turn()}
        ),
    )
    execution = ExecutionServer()
    execution.latest = _limited_turn()
    execution.account_id = "acc-2"
    service = ContinuityService(accounts=accounts)
    service.automation = ContinuationSupervisor(factory=lambda: execution)
    await _observed_running("thread")

    async def cannot_resume(_thread_id):
        raise RuntimeError("Conversation could not be loaded")

    execution.resume_for_continuation = cannot_resume

    async def verify(_home):
        return "acc-2"

    tx = AuthTransaction(credential_store=store, desktop=FakeDesktop(), verify_account=verify)
    result = await service.continue_on_limit("hesap2", transaction=tx)
    assert result.success
    assert result.detail is None
    assert store.read_active() == b"{}"
    await asyncio.gather(*tuple(service.automation.tasks))
    assert not execution.turn_calls
    handoff = (await service.recent_handoffs())[0]
    assert handoff.success
    assert handoff.detail == result.detail
    from codex_account_manager.storage.database import connect

    async with connect() as db:
        records = await (
            await db.execute(
                "SELECT thread_id, payload FROM events WHERE topic='continuation.status'"
            )
        ).fetchall()
    import json

    assert any(
        tid == "thread" and json.loads(payload) == {"state": "needs_user", "stage": "conversation"}
        for tid, payload in records
    )


async def test_history_write_failure_after_commit_does_not_report_switch_failure(migrated_db):
    accounts = AccountService(app_server_factory=_factory("acc-2"))
    await _seed_two_profiles(migrated_db, accounts)
    store = FileCredentialStore(shared_home=migrated_db.shared_codex_home)
    store.write_active_atomic(b"previous")
    service = ContinuityService(accounts=accounts)
    original_save = service.handoffs.save
    calls = 0

    async def fail_after_commit(record):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("history unavailable")
        await original_save(record)

    service.handoffs.save = fail_after_commit

    async def verify(_home):
        return "acc-2"

    tx = AuthTransaction(credential_store=store, desktop=FakeDesktop(), verify_account=verify)
    result = await service.handoff("hesap2", transaction=tx)
    assert result.success
    assert store.read_active() == b"{}"
    assert calls == 2


async def test_two_desktop_handoffs_keep_goal_and_follow_new_account(migrated_db, monkeypatch):
    import asyncio
    from dataclasses import replace
    from unittest.mock import AsyncMock

    import codex_account_manager.continuity.service as cs
    from codex_account_manager.core.errors import DesktopContinuationRequired

    accounts = AccountService(app_server_factory=_factory("acc-2"))
    await _seed_two_profiles(migrated_db, accounts)
    store = FileCredentialStore(shared_home=migrated_db.shared_codex_home)
    store.write_active_atomic(b"source-auth")
    desktop_thread = replace(_thread("t-live", 999), source="vscode")
    fake = FakeAppServer(account_id="acc-1", threads=[desktop_thread])
    fake.is_desktop_thread = AsyncMock(return_value=True)
    native_goal = GoalInfo("t-live", "Preserve this objective", "active", True, 5000, 100)
    turn = {"id": "first", "status": "inProgress"}
    sends = []

    async def goal(_tid):
        return native_goal

    async def desktop_only(_tid):
        raise DesktopContinuationRequired("Desktop owns this work")

    fake.get_goal = goal
    fake.require_headless_compatible = desktop_only
    monkeypatch.setattr(cs, "CodexAppServer", lambda *_args, **_kwargs: fake)
    service = ContinuityService(accounts=accounts)
    service.automation.factory = lambda: fake

    async def latest(_tid):
        return dict(turn)

    async def send(tid, source_id):
        nonlocal turn
        sends.append((tid, source_id, fake.account_id))
        turn = {"id": f"next-{len(sends)}", "status": "inProgress"}
        return {"threadId": tid}

    service.automation.native.latest_turn = latest
    service.automation.native.send = send
    await service.observe_work()
    for alias, target in [("hesap2", "acc-2"), ("ana", "acc-1")]:
        source_id = turn["id"]
        turn = {
            "id": source_id,
            "status": "failed",
            "error": {"codexErrorInfo": "usageLimitExceeded"},
        }

        async def verify(_home, target=target):
            fake.account_id = target
            return target

        tx = AuthTransaction(credential_store=store, desktop=FakeDesktop(), verify_account=verify)
        result = await service.continue_on_limit(alias, transaction=tx)
        assert result.success
        await asyncio.gather(*tuple(service.automation.tasks))
        assert sends[-1] == ("t-live", source_id, target)
        tracked = await service.tracker.visible()
        assert len(tracked) == 1 and tracked[0].turn_status == "inProgress"
        assert not await service.tracker.limited(target)
    assert len(sends) == 2
    assert not fake.resume_calls and not fake.set_goal_calls
    assert native_goal.token_budget == 5000 and native_goal.tokens_used == 100


@pytest.mark.parametrize("ide_failure", [None, "observation", "preparation", "running", "timeout"])
@pytest.mark.parametrize("desktop_disabled", [False, True])
async def test_two_desktop_and_one_ide_limit_continue_after_committed_switch(
    migrated_db, monkeypatch, ide_failure, desktop_disabled
):
    import asyncio
    from dataclasses import replace

    import codex_account_manager.continuity.service as cs
    from codex_account_manager.adapters.native_ide import snapshot
    from codex_account_manager.core.errors import (
        DesktopContinuationRequired,
        LocalResponseTooLargeError,
    )
    from codex_account_manager.storage.repositories import SettingsRepository
    from tests.unit.test_native_ide import state_message

    await SettingsRepository().set("ide_continue", "true")
    await SettingsRepository().set("desktop_continue", str(not desktop_disabled).lower())
    accounts = AccountService(app_server_factory=_factory("acc-2"))
    await _seed_two_profiles(migrated_db, accounts)
    store = FileCredentialStore(shared_home=migrated_db.shared_codex_home)
    store.write_active_atomic(b"source-auth")
    ids = ["desktop-one", "desktop-two", "vscode"]
    turns = {tid: {"id": "last", "status": "inProgress"} for tid in ids}
    fake = FakeAppServer(
        account_id="acc-1",
        threads=[replace(_thread(tid, 100), source="vscode") for tid in ids],
        turns=turns,
    )
    service = ContinuityService(accounts=accounts)
    service.automation.factory = lambda: fake
    monkeypatch.setattr(cs, "CodexAppServer", lambda *_args, **_kwargs: fake)

    async def native_only(_tid):
        raise DesktopContinuationRequired("local owner")

    fake.require_headless_compatible = native_only
    fail_ide = False

    async def inspect(tid):
        assert tid == "vscode", "Desktop must not use the IDE owner registry"
        if fail_ide and ide_failure == "observation":
            raise LocalResponseTooLargeError("test frame limit")
        if fail_ide and ide_failure == "timeout":
            await asyncio.Event().wait()
        owner = fake.account_id + "-" + tid
        turn = turns[tid]
        message = state_message(
            id=tid,
            turns=[{"turnId": turn["id"], "status": turn["status"]}],
            threadRuntimeStatus={"type": "systemError" if turn["status"] == "failed" else "active"},
        )
        message["sourceClientId"] = owner
        message["params"]["conversationId"] = tid
        return snapshot(message, tid, owner)

    sends = []

    async def send(tid, owner, _message_id):
        assert tid == "vscode"
        assert fake.account_id == "acc-2"
        assert owner.owner_id == "acc-2-" + tid
        assert tid not in sends
        sends.append(tid)
        turns[tid] = {"id": "next", "status": "inProgress"}
        return turns[tid]

    service.automation.ide.inspect = inspect
    service.automation.ide.send = send

    async def is_desktop(tid):
        return tid.startswith("desktop-")

    async def native_latest(tid):
        assert tid.startswith("desktop-")
        return turns[tid]

    async def native_send(tid, turn_id):
        assert tid.startswith("desktop-") and turn_id == "last"
        assert fake.account_id == "acc-2" and tid not in sends
        sends.append(tid)
        turns[tid] = {"id": "next", "status": "inProgress"}
        return {"threadId": tid}

    fake.is_desktop_thread = is_desktop
    service.automation.native.latest_turn = native_latest
    service.automation.native.send = native_send
    await service.observe_work()
    assert len(await service.tracker.visible()) == 3
    for tid in ids:
        turns[tid] = {
            "id": "last",
            "status": "failed",
            "error": {"codexErrorInfo": "usageLimitExceeded"},
        }
    fail_ide = True
    if ide_failure == "running":
        turns["vscode"] = {"id": "last", "status": "inProgress"}
    if ide_failure == "timeout":
        monkeypatch.setattr(cs, "THREAD_SCAN_TIMEOUT", 5)
        monkeypatch.setattr(cs, "THREAD_CHECK_TIMEOUT", 10)
    if ide_failure == "preparation":
        original_prepare = service.automation.prepare

        async def prepare(tid):
            if tid == "vscode":
                raise LocalResponseTooLargeError("test frame limit")
            return await original_prepare(tid)

        service.automation.prepare = prepare

    async def verify(_home):
        fake.account_id = "acc-2"
        return "acc-2"

    transaction = AuthTransaction(
        credential_store=store, desktop=FakeDesktop(), verify_account=verify
    )
    result = await service.continue_on_limit("hesap2", transaction=transaction)
    assert result.success
    await asyncio.gather(*tuple(service.automation.tasks))
    expected = ([] if desktop_disabled else ids[:2]) + (ids[2:] if ide_failure is None else [])
    assert set(sends) == set(expected)
    observed = {work.thread_id: work for work in await service.tracker.visible()}
    assert all(observed[tid].turn_status == "inProgress" for tid in sends)
    if ide_failure is not None:
        from codex_account_manager.continuity.tracking import account_hash

        assert observed["vscode"].account_hash == account_hash("acc-1")
        assert observed["vscode"].verified == (ide_failure in {"running", "preparation"})
    assert not await service.tracker.limited("acc-2")
    assert not fake.resume_calls and not fake.set_goal_calls
