import asyncio
import subprocess
import sys

import pytest

from codex_account_manager.accounts.service import AccountService
from tests.fakes import FakeAppServer


def _factory(account_id="acc-1", **kw):
    def make(_home):
        return FakeAppServer(account_id=account_id, **kw)

    return make


async def test_create_and_list_profile(migrated_db):
    svc = AccountService(app_server_factory=_factory())
    profile = await svc.create_profile("ana")
    assert profile.alias == "ana"
    profiles = await svc.list_profiles()
    assert [p.alias for p in profiles] == ["ana"]
    # New profile uses file auth config.
    config = (migrated_db.profiles_dir / profile.id / "config.toml").read_text(encoding="utf-8")
    assert 'cli_auth_credentials_store = "file"' in config


async def test_bind_and_health_match(migrated_db):
    svc = AccountService(app_server_factory=_factory("acc-1"))
    profile = await svc.create_profile("ana")
    # Write a fake auth so auth_present is true.
    (migrated_db.profiles_dir / profile.id / "auth.json").write_bytes(b"{}")
    account_id = await svc.bind_current_account("ana")
    assert account_id == "acc-1"
    health = await svc.health("ana")
    assert health.account_match is True
    assert health.auth_present is True
    assert health.plan_type == "plus"
    assert health.primary_window_minutes == 300
    assert health.secondary_window_minutes == 10080


async def test_all_health_reports_every_profile(migrated_db):
    svc = AccountService(app_server_factory=_factory("acc-1"))
    await svc.create_profile("ana")
    await svc.create_profile("hesap2")
    health = await svc.all_health()
    assert {h.alias for h in health} == {"ana", "hesap2"}


async def test_health_survives_app_server_error(migrated_db):
    svc = AccountService(app_server_factory=_factory("acc-1", fail_on={"read_account"}))
    await svc.create_profile("ana")
    health = await svc.health("ana")
    assert health.error is not None
    assert health.plan_type is None


@pytest.mark.parametrize("outcome", ["success", "failure", "timeout", "cancel"])
async def test_login_subprocess_lifecycle(migrated_db, monkeypatch, outcome):
    from codex_account_manager.codex import runtime

    svc = AccountService(app_server_factory=_factory())
    profile = await svc.create_profile("login-test")
    scripts = {
        "success": "import os,pathlib;pathlib.Path(os.environ['CODEX_HOME'],'auth.json').write_text('{}')",
        "failure": "raise SystemExit(7)",
        "timeout": "import time;time.sleep(30)",
        "cancel": "import time;time.sleep(30)",
    }

    def command(*args):
        assert args == ("login",)
        return [sys.executable, "-c", scripts[outcome]]

    monkeypatch.setattr(runtime, "codex_command", command)
    spawn = asyncio.create_subprocess_exec
    processes = []
    started = asyncio.Event()

    async def capture_process(*args, **kwargs):
        assert kwargs["env"]["CODEX_HOME"] == profile.codex_home
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        process = await spawn(*args, **kwargs)
        processes.append(process)
        started.set()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture_process)
    if outcome == "timeout":
        wait_for = asyncio.wait_for

        async def short_timeout(awaitable, timeout):
            assert timeout in {600, 3}
            return await wait_for(awaitable, timeout=0.05 if timeout == 600 else timeout)

        monkeypatch.setattr(asyncio, "wait_for", short_timeout)

    if outcome == "success":
        assert await svc.login_profile(profile.alias) == "acc-1"
        assert (await svc.list_profiles())[0].bound_account_id == "acc-1"
    elif outcome == "cancel":
        task = asyncio.create_task(svc.login_profile(profile.alias))
        await asyncio.wait_for(started.wait(), timeout=10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        message = "sign-in failed" if outcome == "failure" else "timed out"
        with pytest.raises(ValueError, match=message):
            await svc.login_profile(profile.alias)
    assert len(processes) == 1
    assert processes[0].returncode is not None
    from codex_account_manager.core.operation_lock import OperationLock

    with OperationLock(migrated_db.data_dir / "account-operation.lock"):
        pass
    if outcome != "success":
        assert (await svc.list_profiles())[0].bound_account_id is None


async def test_active_health_uses_shared_snapshot_not_stale_profile(migrated_db):
    calls = []

    def factory(home):
        calls.append(home)
        return FakeAppServer(account_id="active")

    service = AccountService(app_server_factory=factory)
    profile = await service.create_profile("personal")
    await service.profiles.bind_account(profile.id, "active")
    (migrated_db.profiles_dir / profile.id / "auth.json").write_bytes(b"old opaque credentials")
    health = (await service.all_health())[0]
    assert calls == [str(migrated_db.shared_codex_home)]
    assert health.is_active and health.account_match
    assert health.email == "user@example.test"
    assert health.email not in repr(health)


async def test_transient_health_failure_retains_display_but_never_allows_switch(migrated_db):
    from codex_account_manager.continuity.policy import SwitchPolicy
    from codex_account_manager.core.errors import AppServerError
    from codex_account_manager.domain.states import QuotaState

    server = FakeAppServer()
    service = AccountService(app_server_factory=lambda _: server)
    profile = await service.create_profile("work")
    profile.bound_account_id = "acc-1"
    (migrated_db.profiles_dir / profile.id / "auth.json").write_bytes(b"opaque")
    previous = await service._health_for(profile, active_account_id="acc-1")
    server._fail_on.add("read_account")
    health = await service._health_for(profile, active_account_id="acc-1")
    assert health.stale and health.email == previous.email
    assert health.primary_used_percent == previous.primary_used_percent
    assert health.last_checked_at == previous.last_checked_at
    assert health.quota_state == QuotaState.UNKNOWN and health.ordinary_usage_allowed is None
    assert not health.reauth_required and not SwitchPolicy._is_available(health)
    assert not SwitchPolicy._current_limited(health)
    server._fail_on.clear()
    recovered = await service._health_for(profile, active_account_id="acc-1")
    assert not recovered.stale and recovered.error is None
    assert SwitchPolicy._is_available(recovered)
    profile.bound_account_id = "someone-else"
    unrelated = service._failed_health(profile, AppServerError("offline"), active_account_id=None)
    assert unrelated.email is None and not unrelated.stale


async def test_explicit_revocation_clears_cached_health_but_keeps_profile(migrated_db):
    from codex_account_manager.core.errors import SignInRequiredError

    service = AccountService(app_server_factory=_factory())
    profile = await service.create_profile("work")
    profile.bound_account_id = "acc-1"
    auth = migrated_db.profiles_dir / profile.id / "auth.json"
    auth.write_bytes(b"opaque")
    await service._health_for(profile, active_account_id=None)
    failed = service._failed_health(profile, SignInRequiredError("revoked"), active_account_id=None)
    assert failed.reauth_required and not failed.stale
    assert failed.email is None and failed.primary_used_percent is None
    assert auth.read_bytes() == b"opaque"
    assert len(await service.list_profiles()) == 1


async def test_unknown_active_identity_never_refreshes_profile_copies(migrated_db):
    from unittest.mock import AsyncMock

    from codex_account_manager.adapters.app_server import CodexAppServer
    from codex_account_manager.core.errors import SignInRequiredError

    calls = []
    saved = CodexAppServer("synthetic", refresh_on_unauthorized=True)
    saved.start = AsyncMock()
    saved.read_account = AsyncMock(return_value=await FakeAppServer().read_account())

    def unavailable(home):
        calls.append(home)
        if home == str(migrated_db.shared_codex_home):
            raise SignInRequiredError("sign-in rejected")
        return saved

    service = AccountService(app_server_factory=unavailable)
    profile = await service.create_profile("work")
    await service.profiles.bind_account(profile.id, "acc-1")
    service.credentials.write_active_atomic(b"opaque active credentials")
    result = await service.all_health()
    assert calls == [str(migrated_db.shared_codex_home), profile.codex_home]
    assert not saved.refresh_on_unauthorized
    assert result[0].error is None and result[0].account_match and not result[0].is_active
    assert service.credentials.read_active() == b"opaque active credentials"


@pytest.mark.parametrize("auth_file_exists", [False, True])
async def test_confirmed_signed_out_shared_home_does_not_block_saved_accounts(
    migrated_db, auth_file_exists
):
    from unittest.mock import AsyncMock

    from codex_account_manager.adapters.app_server import CodexAppServer

    calls = []
    signed_out = CodexAppServer(str(migrated_db.shared_codex_home))
    signed_out.start = AsyncMock()
    signed_out._request = AsyncMock(return_value={"account": None})

    def factory(home):
        calls.append(home)
        return signed_out if home == str(migrated_db.shared_codex_home) else FakeAppServer()

    service = AccountService(app_server_factory=factory)
    profile = await service.create_profile("saved")
    await service.profiles.bind_account(profile.id, "acc-1")
    service.credentials.profile_auth_path(profile.codex_home).write_bytes(b"saved credentials")
    if auth_file_exists:
        service.credentials.write_active_atomic(b"signed-out placeholder")
    original = service.credentials.read_active()
    result = (await service.all_health())[0]
    assert result.error is None and result.account_match and not result.is_active
    assert calls == [str(migrated_db.shared_codex_home), profile.codex_home]
    signed_out._request.assert_awaited_once_with("account/read", {"refreshToken": False})
    assert service.credentials.read_active() == original


async def test_health_cannot_race_with_switch_or_login(migrated_db):
    from codex_account_manager.core.errors import OperationBusyError
    from codex_account_manager.core.operation_lock import OperationLock

    def unexpected(home):
        pytest.fail("Health reads must wait for account mutations")

    service = AccountService(app_server_factory=unexpected)
    await service.create_profile("work")
    with OperationLock(migrated_db.data_dir / "account-operation.lock"):
        with pytest.raises(OperationBusyError):
            await service.all_health()


async def test_same_home_reads_cannot_rotate_tokens_concurrently(migrated_db):
    from codex_account_manager.core.errors import TransactionError

    started = asyncio.Event()
    release = asyncio.Event()

    class SlowServer(FakeAppServer):
        async def read_account(self):
            started.set()
            await release.wait()
            return await super().read_account()

    server = SlowServer()
    service = AccountService(app_server_factory=lambda _: server)
    task = asyncio.create_task(service.read_snapshot(migrated_db.shared_codex_home))
    await started.wait()
    try:
        with pytest.raises(TransactionError):
            await service.read_snapshot(migrated_db.shared_codex_home)
    finally:
        release.set()
        await task
    assert server.closed


async def test_concurrent_health_refreshes_share_one_query_and_survive_cancel(migrated_db):
    service = AccountService()
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def read():
        calls.append("health")
        started.set()
        await release.wait()
        return []

    service._all_health = read
    first = asyncio.create_task(service.all_health())
    await started.wait()
    second = asyncio.create_task(service.all_health())
    await asyncio.sleep(0)
    first.cancel()
    release.set()
    await asyncio.gather(first, return_exceptions=True)
    assert await second == [] and calls == ["health"]
    assert await service.all_health() == [] and calls == ["health", "health"]


async def test_stalled_profile_times_out_without_hiding_healthy_profile(migrated_db, monkeypatch):
    import codex_account_manager.accounts.service as module
    from codex_account_manager.core.operation_lock import OperationLock

    class StalledServer(FakeAppServer):
        async def read_account(self):
            await asyncio.Event().wait()

    stalled = StalledServer()
    service = AccountService()
    slow = await service.create_profile("slow")
    healthy = await service.create_profile("healthy")
    await service.profiles.bind_account(healthy.id, "acc-1")
    service.credentials.profile_auth_path(healthy.codex_home).write_bytes(b"opaque")
    service._factory = lambda home: (
        stalled if home == slow.codex_home else FakeAppServer(account_id="acc-1")
    )
    monkeypatch.setattr(module, "ACCOUNT_READ_TIMEOUT", 0.05)
    results = {row.alias: row for row in await asyncio.wait_for(service.all_health(), 2)}
    assert results["slow"].error == "Account check timed out. Try refreshing again."
    assert results["healthy"].account_match and not results["healthy"].error
    assert stalled.closed
    with OperationLock(migrated_db.data_dir / "account-operation.lock"):
        pass
    service._factory = lambda _: FakeAppServer(account_id="acc-1")
    assert all(row.error is None for row in await service.all_health())


async def test_missing_account_identity_is_unverified_not_a_mismatch(migrated_db):
    from dataclasses import replace

    server = FakeAppServer()
    service = AccountService(app_server_factory=lambda _: server)
    profile = await service.create_profile("work")
    profile.bound_account_id = "acc-1"
    snapshot = replace(await server.read_account(), account_id=None)

    async def incomplete():
        return snapshot

    server.read_account = incomplete
    health = await service._health_for(profile, active_account_id=None)
    assert health.account_match is None and health.error
    assert not health.reauth_required and health.email is None


def test_only_managed_profile_homes_enable_forced_auth_recovery(tmp_paths):
    from codex_account_manager.accounts.service import _default_factory

    assert _default_factory(str(tmp_paths.profiles_dir / "account")).refresh_on_unauthorized
    assert not _default_factory(str(tmp_paths.shared_codex_home)).refresh_on_unauthorized
    assert not _default_factory(str(tmp_paths.data_dir / "external")).refresh_on_unauthorized
