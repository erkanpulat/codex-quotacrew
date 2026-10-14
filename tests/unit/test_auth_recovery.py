import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from codex_account_manager.accounts.service import AccountService
from codex_account_manager.continuity.policy import AvailabilityFailoverPolicy
from codex_account_manager.core.errors import SignInRequiredError
from codex_account_manager.core.operation_lock import OperationLock
from tests.fakes import FakeAppServer
from tests.unit.test_watcher import _health


async def test_rejected_profile_stays_rejected_until_credentials_change(migrated_db):
    service = AccountService(app_server_factory=lambda _: FakeAppServer(account_id="target"))
    profile = await service.create_profile("target")
    await service.profiles.bind_account(profile.id, "target")
    auth = service.credentials.profile_auth_path(profile.codex_home)
    auth.write_bytes(b'{"tokens":{"refresh_token":"old"}}')
    await service.reject_profile(profile)
    restarted = AccountService(app_server_factory=lambda _: FakeAppServer(account_id="target"))
    restarted.read_snapshot = AsyncMock(
        return_value=await FakeAppServer(account_id="target").read_account()
    )
    result = await restarted.health("target")
    assert result.reauth_required
    assert result.error
    assert all(
        str(call.args[0]) != profile.codex_home for call in restarted.read_snapshot.await_args_list
    )
    auth.write_bytes(b'{"tokens":{"refresh_token":"new"}}')
    assert not (await restarted.health("target")).reauth_required


async def test_rejected_active_identity_is_preserved_without_trusting_saved_profile(migrated_db):
    service = AccountService()
    profile = await service.create_profile("active")
    await service.profiles.bind_account(profile.id, "active-id")
    service.credentials.write_active_atomic(b'{"tokens":{"account_id":"active-id"}}')
    service.read_snapshot = AsyncMock(side_effect=SignInRequiredError("rejected"))
    result = (await service.all_health())[0]
    assert result.is_active
    assert result.reauth_required
    assert result.account_match is None
    service.read_snapshot.assert_awaited_once()


def test_auth_failure_selects_another_verified_account():
    current = replace(_health("broken", active=True), reauth_required=True, error="rejected")
    target = _health("healthy")
    decision = AvailabilityFailoverPolicy().decide(current, [current, target])
    assert decision.should_switch
    assert decision.target == target


async def test_waiting_lock_releases_after_cancellation(tmp_path):
    path = tmp_path / "operation.lock"
    entered = asyncio.Event()

    async def hold():
        async with OperationLock(path).wait(timeout=1):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(hold())
    await asyncio.wait_for(entered.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with OperationLock(path):
        pass


async def test_waiting_lock_yields_until_other_operation_finishes(tmp_path):
    path = tmp_path / "operation.lock"
    first = OperationLock(path)
    first.__enter__()

    async def release():
        await asyncio.sleep(0.02)
        first.__exit__(None, None, None)

    task = asyncio.create_task(release())
    try:
        async with OperationLock(path).wait(timeout=1):
            assert task.done()
    finally:
        first.__exit__(None, None, None)
        await task


async def test_unattributed_signin_failure_does_not_quarantine_target(migrated_db):
    from codex_account_manager.continuity.service import ContinuityService

    accounts = AccountService(app_server_factory=lambda _: FakeAppServer(account_id="source"))
    profile = await accounts.create_profile("bad-target")
    await accounts.profiles.bind_account(profile.id, "target")
    accounts.credentials.profile_auth_path(profile.codex_home).write_bytes(b"{}")
    service = ContinuityService(accounts=accounts)
    tx = AsyncMock()
    tx.switch.side_effect = SignInRequiredError("rejected")
    with pytest.raises(SignInRequiredError):
        await service.handoff("bad-target", transaction=tx)
    from codex_account_manager.storage.repositories import SettingsRepository

    assert await SettingsRepository().get(f"rejected_auth:{profile.id}") is None
    records = await service.recent_handoffs()
    assert records[0].success is False


async def test_expired_auth_can_check_idle_work_without_claiming_a_valid_login(
    migrated_db, monkeypatch
):
    import codex_account_manager.continuity.service as module
    from codex_account_manager.continuity.service import ContinuityService

    server = FakeAppServer()
    server.read_account = AsyncMock(side_effect=SignInRequiredError("rejected"))
    monkeypatch.setattr(module, "CodexAppServer", lambda *a, **kw: server)
    service = ContinuityService()
    service.accounts.credentials.write_active_atomic(b'{"tokens":{"account_id":"source"}}')
    assert await service._require_restart_safe("source") == "source"
    assert service._work_verified


async def test_health_cancellation_finishes_children_before_unlocking(migrated_db):
    service = AccountService()
    profile = await service.create_profile("waiting")
    started = asyncio.Event()
    cleanup = asyncio.Event()
    proceed = asyncio.Event()
    service.read_snapshot = AsyncMock(return_value=await FakeAppServer().read_account())

    async def check(*args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup.set()
            await proceed.wait()

    service._health_for = check
    task = asyncio.create_task(service._health_batch([profile]))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    await asyncio.wait_for(cleanup.wait(), 2)
    from codex_account_manager.core.errors import OperationBusyError

    try:
        with pytest.raises(OperationBusyError):
            with OperationLock(migrated_db.data_dir / "account-operation.lock"):
                pass
    finally:
        proceed.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    with OperationLock(migrated_db.data_dir / "account-operation.lock"):
        pass


async def test_rejected_auth_does_not_bypass_running_work_guard(migrated_db):
    from codex_account_manager.continuity.service import ContinuityService
    from codex_account_manager.core.errors import HandoffDeferredError

    service = ContinuityService()
    service.observe_work = AsyncMock(return_value="source")
    service._work_verified = True
    service._work_running = True
    with pytest.raises(HandoffDeferredError, match="running conversations"):
        await service._require_restart_safe("source")


async def test_expired_source_does_not_skip_target_refresh_check(migrated_db, monkeypatch):
    from codex_account_manager.auth.transaction import AuthTransaction
    from tests.fakes import FakeDesktop
    from tests.unit.test_auth_transaction import _profile

    profile = _profile(migrated_db, "target")
    service = AccountService()
    service.credentials.write_active_atomic(b'{"tokens":{"account_id":"source"}}')
    monkeypatch.setattr(AccountService, "_active_account_id_safe", AsyncMock(return_value=None))
    monkeypatch.setattr(AccountService, "list_profiles", AsyncMock(return_value=[]))
    read = AsyncMock(side_effect=SignInRequiredError("rejected target"))
    monkeypatch.setattr(AccountService, "read_snapshot", read)
    desktop = FakeDesktop()
    with pytest.raises(SignInRequiredError):
        await AuthTransaction(desktop=desktop).switch(profile)
    assert read.await_args.kwargs == {"force_refresh": True}
    assert desktop.stopped == 0
    from codex_account_manager.storage.repositories import SettingsRepository

    assert await SettingsRepository().get(
        f"rejected_auth:{profile.id}"
    ) == service._credential_fingerprint(profile)


async def test_rejected_shared_session_does_not_quarantine_new_profile_login(migrated_db):
    from codex_account_manager.storage.repositories import SettingsRepository

    service = AccountService()
    profile = await service.create_profile("active")
    await service.profiles.bind_account(profile.id, "active-id")
    service.credentials.write_active_atomic(
        b'{"tokens":{"account_id":"active-id","refresh_token":"old"}}'
    )
    service.credentials.profile_auth_path(profile.codex_home).write_bytes(
        b'{"tokens":{"account_id":"active-id","refresh_token":"new"}}'
    )
    service.read_snapshot = AsyncMock(side_effect=SignInRequiredError("rejected"))
    assert (await service.all_health())[0].reauth_required
    assert await SettingsRepository().get(f"rejected_auth:{profile.id}") is None
