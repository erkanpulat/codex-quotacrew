import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from codex_account_manager.accounts.service import AccountService
from codex_account_manager.continuity.policy import AvailabilityFailoverPolicy
from codex_account_manager.core.errors import SignInRequiredError
from codex_account_manager.core.operation_lock import OperationLock
from tests.fakes import FakeAppServer, account_auth, account_identity
from tests.unit.test_watcher import _health


async def test_shared_routing_error_preserves_active_identity_and_failure(migrated_db):
    from codex_account_manager.core.errors import AppServerError

    service = AccountService()
    profile = await service.create_profile("active")
    await service.profiles.bind_account(profile.id, "active-id", user_id="test-user")
    service.credentials.write_active_atomic(account_auth("active-id"))
    service.credentials.profile_auth_path(profile.codex_home).write_bytes(b"{}")
    healthy = await FakeAppServer(account_id="active-id").read_account()

    async def read(home, **kwargs):
        if str(home) == str(migrated_db.shared_codex_home):
            raise AppServerError("workspace routing discovery unauthorized (401)")
        return healthy

    service.read_snapshot = read
    health = (await service.all_health())[0]
    assert health.is_active
    assert health.error and "401" in health.error
    assert health.ordinary_usage_allowed is None
    assert not health.reauth_required


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
    await service.profiles.bind_account(profile.id, "active-id", user_id="test-user")
    service.credentials.write_active_atomic(account_auth("active-id"))
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
    service.accounts.credentials.write_active_atomic(account_auth("source"))
    assert (
        await service._require_restart_safe(account_identity("source").fingerprint)
        == account_identity("source").fingerprint
    )
    assert service._work_verified


async def test_health_cancellation_waits_for_children_without_blocking_account_operations(
    migrated_db,
):
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
    try:
        assert not task.done()
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


async def test_rejected_source_can_escape_unreadable_history(migrated_db):
    from codex_account_manager.continuity.service import ContinuityService

    accounts = AccountService()
    accounts.credentials.write_active_atomic(account_auth("source"))
    accounts.read_snapshot = AsyncMock(side_effect=SignInRequiredError("session rejected"))
    service = ContinuityService(accounts=accounts)
    service.observe_work = AsyncMock(side_effect=SignInRequiredError("routing rejected"))
    assert (
        await service._require_restart_safe(account_identity("source").fingerprint)
        == account_identity("source").fingerprint
    )


async def test_routing_401_does_not_authorize_automatic_restart(migrated_db):
    from codex_account_manager.adapters.app_server import CodexAppServer
    from codex_account_manager.continuity.service import ContinuityService
    from codex_account_manager.core.errors import AppServerError, HandoffDeferredError

    server = CodexAppServer(migrated_db.shared_codex_home)
    server._request = AsyncMock(
        side_effect=AppServerError("workspace routing discovery unauthorized (401)")
    )
    accounts = AccountService()
    accounts.credentials.write_active_atomic(account_auth("source"))

    async def read(*args, **kwargs):
        return await server.read_account()

    accounts.read_snapshot = read
    service = ContinuityService(accounts=accounts)
    service.observe_work = AsyncMock(side_effect=AppServerError("history unavailable"))
    with pytest.raises(HandoffDeferredError):
        await service._require_restart_safe("source")


async def test_unreadable_tracking_does_not_veto_fresh_exhausted_account(migrated_db):
    from codex_account_manager.continuity.service import ContinuityService

    accounts = AccountService(
        app_server_factory=lambda _: FakeAppServer(
            account_id="source", ordinary_usage_allowed=False
        )
    )
    service = ContinuityService(accounts=accounts)
    service.observe_work = AsyncMock(side_effect=TimeoutError("history unavailable"))
    assert (
        await service._require_restart_safe(account_identity("source").fingerprint)
        == account_identity("source").fingerprint
    )


async def test_manual_switch_survives_source_routing_401(migrated_db):
    from codex_account_manager.auth.transaction import SwitchResult
    from codex_account_manager.continuity.service import ContinuityService
    from codex_account_manager.core.errors import AppServerError
    from codex_account_manager.domain.states import HandoffReason, TransactionStage

    accounts = AccountService()
    target = await accounts.create_profile("target")
    await accounts.profiles.bind_account(target.id, "target-id", user_id="test-user")
    accounts.credentials.write_active_atomic(account_auth("source-id"))
    accounts.active_owner_key = AsyncMock(
        side_effect=AppServerError("workspace routing discovery unauthorized (401)")
    )
    service = ContinuityService(accounts=accounts)
    service.observe_work = AsyncMock(
        side_effect=AppServerError("workspace routing discovery unauthorized (401)")
    )

    async def handoff(_alias, **kwargs):
        await kwargs["before_desktop_stop"]()
        return SwitchResult(True, TransactionStage.COMMIT, account_id="target-id")

    service.handoff = AsyncMock(side_effect=handoff)
    result = await service.continue_on_limit("target", reason=HandoffReason.MANUAL)
    assert result.success
    service.handoff.assert_awaited_once()


async def test_unreadable_tracking_never_authorizes_switch_when_quota_is_available(migrated_db):
    from codex_account_manager.continuity.service import ContinuityService
    from codex_account_manager.core.errors import HandoffDeferredError

    accounts = AccountService(
        app_server_factory=lambda _: FakeAppServer(account_id="source", ordinary_usage_allowed=True)
    )
    service = ContinuityService(accounts=accounts)
    service.observe_work = AsyncMock(side_effect=TimeoutError("history unavailable"))
    with pytest.raises(HandoffDeferredError):
        await service._require_restart_safe("source")


async def test_expired_source_does_not_skip_target_refresh_check(migrated_db, monkeypatch):
    from codex_account_manager.auth.transaction import AuthTransaction
    from tests.fakes import FakeDesktop
    from tests.unit.test_auth_transaction import _profile

    profile = _profile(migrated_db, "target")
    service = AccountService()
    service.credentials.write_active_atomic(account_auth("source"))
    monkeypatch.setattr(AccountService, "_active_owner_key_safe", AsyncMock(return_value=None))
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
    await service.profiles.bind_account(profile.id, "active-id", user_id="test-user")
    service.credentials.write_active_atomic(
        b'{"tokens":{"account_id":"active-id","refresh_token":"old"}}'
    )
    service.credentials.profile_auth_path(profile.codex_home).write_bytes(
        b'{"tokens":{"account_id":"active-id","refresh_token":"new"}}'
    )
    service.read_snapshot = AsyncMock(side_effect=SignInRequiredError("rejected"))
    assert (await service.all_health())[0].reauth_required
    assert await SettingsRepository().get(f"rejected_auth:{profile.id}") is None
