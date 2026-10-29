import asyncio
import json
import logging
from dataclasses import replace

import pytest

from codex_account_manager.accounts.service import AccountService
from codex_account_manager.adapters.credential_store import FileCredentialStore
from codex_account_manager.auth.transaction import AuthTransaction
from codex_account_manager.continuity.policy import AvailabilityFailoverPolicy
from codex_account_manager.core.errors import TransactionError
from codex_account_manager.core.files import atomic_write
from codex_account_manager.core.logging import _RedactingFilter, _RedactingFormatter
from codex_account_manager.core.operation_lock import OperationLock
from codex_account_manager.core.redaction import redact_text
from codex_account_manager.domain.identity import AccountIdentity, IdentityState
from codex_account_manager.domain.models import Profile
from codex_account_manager.domain.states import GoalState, QuotaState
from codex_account_manager.goals.service import GoalService
from tests.fakes import FakeAppServer, FakeDesktop, account_identity
from tests.unit.test_auth_transaction import _profile
from tests.unit.test_policy import _health


def test_cli_reports_startup_permission_failure_without_raw_error(monkeypatch):
    from typer.testing import CliRunner

    from codex_account_manager.cli import main as cli

    def denied():
        raise OSError("synthetic private filesystem detail")

    monkeypatch.setattr(cli, "configure_logging", denied)
    result = CliRunner().invoke(cli.app, ["status"])
    assert result.exit_code == 1
    assert "permissions" in result.output.lower()
    assert "synthetic private filesystem detail" not in result.output
    assert isinstance(result.exception, SystemExit)


@pytest.mark.parametrize("alias", ["", "  ", "a" * 65, "bad\nname", "bad\x00name"])
async def test_reject_invalid_profile_names(migrated_db, alias):
    with pytest.raises(ValueError):
        await AccountService().create_profile(alias)
    assert await AccountService().list_profiles() == []


async def test_rename_cannot_empty_name(migrated_db):
    service = AccountService()
    await service.create_profile("work")
    with pytest.raises(ValueError):
        await service.rename_profile("work", " ")
    assert (await service.list_profiles())[0].alias == "work"


async def test_delete_rejects_unmanaged_home(migrated_db, tmp_path):
    outside = tmp_path / "valuable"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    service = AccountService()
    await service.profiles.create(Profile(alias="outside", codex_home=str(outside)))
    with pytest.raises(ValueError, match="outside managed"):
        await service.remove_profile("outside")
    assert (outside / "keep.txt").read_text() == "keep"
    assert await service.profiles.get_by_alias("outside") is not None


@pytest.mark.parametrize(
    "setting",
    ["cli_auth_credentials_store", '"cli_auth_credentials_store"', "'cli_auth_credentials_store'"],
)
def test_root_setting_does_not_modify_nested_config(tmp_paths, setting):
    import tomllib

    config = tmp_paths.shared_codex_home / "config.toml"
    config.write_text(f'{setting} = "keyring"\n[other]\ncli_auth_credentials_store = "keyring"\n')
    FileCredentialStore(tmp_paths.shared_codex_home).ensure_file_auth_config()
    parsed = tomllib.loads(config.read_text())
    assert parsed["cli_auth_credentials_store"] == "file"
    assert parsed["other"]["cli_auth_credentials_store"] == "keyring"


def test_nested_setting_gets_separate_root_setting(tmp_paths):
    import tomllib

    config = tmp_paths.shared_codex_home / "config.toml"
    config.write_text('[other]\ncli_auth_credentials_store = "keyring"\n')
    FileCredentialStore(tmp_paths.shared_codex_home).ensure_file_auth_config()
    parsed = tomllib.loads(config.read_text())
    assert parsed["cli_auth_credentials_store"] == "file"
    assert parsed["other"]["cli_auth_credentials_store"] == "keyring"


def test_atomic_write_preserves_old_file_on_replace_failure(tmp_path, monkeypatch):
    import codex_account_manager.core.files as files

    target = tmp_path / "atomic-write" / "auth.json"
    target.parent.mkdir()
    atomic_write(target, b"old")

    def fail(*_args):
        raise OSError("disk failure")

    monkeypatch.setattr(files.os, "replace", fail)
    with pytest.raises(OSError):
        atomic_write(target, b"new")
    assert target.read_bytes() == b"old"
    assert list(target.parent.iterdir()) == [target]


def test_operation_lock_excludes_second_writer(tmp_path):
    path = tmp_path / "operation.lock"
    with OperationLock(path):
        with pytest.raises(TransactionError):
            with OperationLock(path):
                pytest.fail("second writer admitted")
    with OperationLock(path):
        pass


async def test_cancelled_switch_restores_auth_and_config(tmp_paths):
    store = FileCredentialStore(tmp_paths.shared_codex_home)
    store.write_active_atomic(b"original")
    config = tmp_paths.shared_codex_home / "config.toml"
    config.write_bytes(b'cli_auth_credentials_store = "keyring"\n')

    async def verify(_):
        raise asyncio.CancelledError()

    tx = AuthTransaction(credential_store=store, desktop=FakeDesktop(), verify_account=verify)
    with pytest.raises(asyncio.CancelledError):
        await tx.switch(_profile(tmp_paths))
    assert store.read_active() == b"original"
    assert config.read_bytes() == b'cli_auth_credentials_store = "keyring"\n'
    assert not tx.recovery_path.exists()


async def test_readiness_timeout_is_failure(tmp_paths):
    store = FileCredentialStore(tmp_paths.shared_codex_home)
    store.write_active_atomic(b"original")

    async def verify(_):
        return account_identity()

    tx = AuthTransaction(
        credential_store=store, desktop=FakeDesktop(ready=False), verify_account=verify
    )
    with pytest.raises(TransactionError) as error:
        await tx.switch(_profile(tmp_paths))
    assert error.value.rolled_back
    assert store.read_active() == b"original"


def test_recovery_restores_interrupted_snapshot(tmp_paths):
    store = FileCredentialStore(tmp_paths.shared_codex_home)
    store.write_active_atomic(b"original")
    tx = AuthTransaction(credential_store=store, desktop=FakeDesktop())
    tx._snapshot()
    tx._arm_recovery()
    store.write_active_atomic(b"partial switch")
    recovered = AuthTransaction(credential_store=store, desktop=FakeDesktop()).recover()
    assert recovered
    assert store.read_active() == b"original"
    assert not tx.recovery_path.exists()


def test_malformed_recovery_retained_without_modifying_auth(tmp_paths):
    store = FileCredentialStore(tmp_paths.shared_codex_home)
    store.write_active_atomic(b"original")
    tx = AuthTransaction(credential_store=store, desktop=FakeDesktop())
    tx.recovery_path.write_text(json.dumps({"home": "different"}))
    with pytest.raises(TransactionError):
        tx.recover()
    assert store.read_active() == b"original"
    assert tx.recovery_path.exists()


@pytest.mark.parametrize("state", [GoalState.COMPLETE, GoalState.FAILED, GoalState.PAUSED])
async def test_handoff_does_not_reactivate_terminal_or_paused_goals(migrated_db, state):
    service = GoalService()
    goal = await service.set_objective("thread", "An objective")
    goal.local_status = state
    await service.goals.upsert(goal)
    await service.mark_status("thread", GoalState.SWITCHING)
    await service.mark_status("thread", GoalState.RESUMING)
    adapter = FakeAppServer()
    await service.reconcile_after_resume("thread", adapter)
    assert not adapter.set_goal_calls
    assert (await service.get("thread")).local_status == state


@pytest.mark.parametrize(
    "changes",
    [{"account_match": None}, {"quota_state": QuotaState.UNKNOWN}, {"auth_present": False}],
)
def test_failover_rejects_unverified_candidates(changes):
    current = _health("current", active=True, allowed=False)
    candidate = replace(_health("other"), **changes)
    assert not AvailabilityFailoverPolicy().decide(current, [candidate]).should_switch


@pytest.mark.parametrize(
    "message",
    ["password=short", '{"access_token": "short"}', "Authorization: Bearer shortbutsecret123"],
)
def test_redaction_masks_short_named_secrets(message):
    assert "short" not in redact_text(message)


def test_logging_redacts_interpolation_and_tracebacks():
    try:
        raise ValueError("password=trace-secret")
    except ValueError:
        import sys

        record = logging.LogRecord(
            "test", logging.ERROR, "file", 1, "password=%s", ("arg-secret",), sys.exc_info()
        )
    _RedactingFilter().filter(record)
    result = _RedactingFormatter().format(record)
    assert "arg-secret" not in result
    assert "trace-secret" not in result


async def test_event_payload_masks_named_secrets(migrated_db):
    from codex_account_manager.storage.repositories import EventRepository

    repository = EventRepository()
    await repository.append(
        "failure",
        thread_id="t",
        payload={"password": "short-value", "detail": "api_key=another-short-value"},
    )
    timeline = await repository.timeline("t")
    assert "short-value" not in str(timeline)


def test_credential_file_permissions(tmp_path):
    import os
    import sys

    path = tmp_path / "credentials.json"
    atomic_write(path, b"synthetic credential")
    if sys.platform == "win32":
        import win32api
        import win32con
        import win32security

        descriptor = win32security.GetFileSecurity(
            str(path), win32security.DACL_SECURITY_INFORMATION
        )
        acl = descriptor.GetSecurityDescriptorDacl()
        token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
        try:
            user = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
        finally:
            token.Close()
        assert acl.GetAceCount() == 1
        assert acl.GetAce(0)[2] == user
    else:
        assert os.stat(path).st_mode & 0o077 == 0


async def test_goal_cannot_be_cleared_during_native_reconciliation(migrated_db):
    from codex_account_manager.adapters.interfaces import GoalInfo

    started = asyncio.Event()
    finish = asyncio.Event()
    service = GoalService()
    await service.set_objective("thread", "Objective")

    class SlowAdapter(FakeAppServer):
        async def get_goal(self, thread_id):
            started.set()
            await finish.wait()
            return GoalInfo(
                thread_id=thread_id, objective="Objective", status="active", present=True
            )

    task = asyncio.create_task(service.reconcile_after_resume("thread", SlowAdapter()))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        with pytest.raises(TransactionError):
            await service.user_clear("thread")
    finally:
        finish.set()
        await asyncio.wait_for(task, timeout=5)
    await service.user_clear("thread")
    assert (await service.get("thread")).user_cleared


@pytest.mark.parametrize("status", ["complete", "paused", "failed"])
async def test_native_goal_state_is_preserved(migrated_db, status):
    from codex_account_manager.adapters.interfaces import GoalInfo

    service = GoalService()
    await service.set_objective("thread", "Objective")

    class Adapter(FakeAppServer):
        async def get_goal(self, thread_id):
            return GoalInfo(thread_id=thread_id, objective="Objective", status=status, present=True)

    result = await service.reconcile_after_resume("thread", Adapter())
    assert result.action == "native_status"
    assert (await service.get("thread")).local_status.value == status


async def test_duplicate_profile_name_leaves_no_orphan(migrated_db):
    service = AccountService()
    await service.create_profile("work")
    before = set(migrated_db.profiles_dir.iterdir())
    with pytest.raises(ValueError, match="already exists"):
        await service.create_profile("work")
    assert set(migrated_db.profiles_dir.iterdir()) == before
    assert len(await service.list_profiles()) == 1


async def test_duplicate_rename_preserves_both_profiles(migrated_db):
    service = AccountService()
    await service.create_profile("work")
    await service.create_profile("personal")
    with pytest.raises(ValueError, match="already exists"):
        await service.rename_profile("personal", "work")
    assert {p.alias for p in await service.list_profiles()} == {"work", "personal"}


async def test_empty_health_does_not_start_codex(migrated_db):
    def unexpected_start(_home):
        pytest.fail("Empty accounts must not launch Codex")

    assert await AccountService(app_server_factory=unexpected_start).all_health() == []


async def test_bind_is_blocked_during_account_switch(migrated_db):
    service = AccountService()
    await service.create_profile("work")
    with OperationLock(migrated_db.data_dir / "account-operation.lock"):
        with pytest.raises(TransactionError, match="operation is in progress"):
            await service.bind_current_account("work")


async def test_health_error_preserves_known_active_identity(migrated_db):
    def unavailable(_home):
        raise OSError("Temporarily unavailable")

    service = AccountService(app_server_factory=unavailable)
    profile = Profile(
        alias="work",
        codex_home=str(migrated_db.profiles_dir / "work"),
        bound_account_id="account-work",
        bound_user_id="user-work",
    )
    health = await service._health_for(profile, active_identity=profile.identity)
    assert health.is_active
    assert health.quota_state == QuotaState.UNKNOWN
    assert health.error == "Temporarily unavailable"


async def test_duplicate_account_binding_preserves_original(migrated_db):
    identity = AccountIdentity(
        user_id="same-user", workspace_id="same-account", state=IdentityState.COMPLETE
    )
    service = AccountService(
        app_server_factory=lambda _: FakeAppServer(account_id="same-account", identity=identity)
    )
    await service.create_profile("work")
    await service.create_profile("personal")
    await service.bind_current_account("work")
    with pytest.raises(ValueError, match="already bound"):
        await service.bind_current_account("personal")
    profiles = {p.alias: p for p in await service.list_profiles()}
    assert profiles["work"].bound_account_id == "same-account"
    assert profiles["personal"].bound_account_id is None


def test_fresh_install_uses_product_name_for_data(tmp_path, monkeypatch):
    import importlib

    module = importlib.import_module("codex_account_manager.core.paths")
    monkeypatch.setattr(module, "user_data_dir", lambda name, **_: str(tmp_path / name))
    resolved = module._resolve.__wrapped__()
    assert resolved.data_dir == tmp_path / "CodexAccountManager"
    assert resolved.db_path.name == "accounts.db"


def test_packaged_and_protocol_versions_match_project_metadata():
    import tomllib
    from pathlib import Path

    from codex_account_manager import __version__

    metadata = tomllib.loads(Path("pyproject.toml").read_text("utf-8"))
    assert metadata["project"]["version"] == __version__
    assert f'#define AppVersion "{__version__}"' in Path("packaging/installer.iss").read_text(
        "utf-8"
    )


async def test_diagnostic_event_retention_preserves_recent_history(migrated_db, monkeypatch):
    from codex_account_manager.storage.repositories import EventRepository

    monkeypatch.setattr(EventRepository, "HISTORY_LIMIT", 3)
    repository = EventRepository()
    for index in range(7):
        await repository.append("test", thread_id="t", payload={"sequence": index})
    assert [item["payload"]["sequence"] for item in await repository.timeline("t")] == [4, 5, 6]
