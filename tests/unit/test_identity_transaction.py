from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from codex_account_manager.accounts.service import AccountService
from codex_account_manager.adapters.auth_identity import auth_evidence
from codex_account_manager.adapters.credential_store import FileCredentialStore
from codex_account_manager.auth.transaction import AuthTransaction
from codex_account_manager.core.errors import AccountMismatchError, AppServerError, TransactionError
from codex_account_manager.domain.identity import AccountIdentity, IdentityState
from codex_account_manager.domain.models import Profile
from tests.fakes import FakeDesktop, account_auth, account_identity


def profile(tmp_paths, user):
    home = tmp_paths.profiles_dir / user
    home.mkdir()
    (home / "auth.json").write_bytes(account_auth("W1", user))
    return Profile(
        id=user, alias=user, codex_home=str(home), bound_account_id="W1", bound_user_id=user
    )


@pytest.mark.parametrize("fail_target", [False, True])
async def test_same_workspace_source_rotation_never_overwrites_first_user(
    tmp_paths, monkeypatch, fail_target
):
    profiles = [profile(tmp_paths, f"U{i}") for i in range(1, 5)]
    first, source, target, fourth = profiles
    store = FileCredentialStore(tmp_paths.shared_codex_home)
    store.write_active_atomic(account_auth("W1", "U2"))
    unchanged = {p.id: store.read_profile(p.codex_home) for p in (first, fourth)}
    rotated = account_auth("W1", "U2", generation=2)

    class Desktop(FakeDesktop):
        def stop(self):
            super().stop()
            if self.stopped == 1:
                store.write_active_atomic(rotated)

    desktop = Desktop()

    async def snapshot(_self, home, **_kwargs):
        raw = store.read_active() if Path(home) == store.shared_home else store.read_profile(home)
        identity = auth_evidence(raw).identity
        if fail_target and desktop.stopped and identity.matches(target.identity):
            raise RuntimeError("Synthetic activation failure")
        return SimpleNamespace(account_id="W1", identity=identity)

    monkeypatch.setattr(AccountService, "list_profiles", AsyncMock(return_value=profiles))
    monkeypatch.setattr(AccountService, "read_snapshot", snapshot)
    tx = AuthTransaction(credential_store=store, desktop=desktop)
    if fail_target:
        with pytest.raises(TransactionError):
            await tx.switch(target)
        assert store.read_active() == rotated
    else:
        result = await tx.switch(target)
        assert result.identity.matches(target.identity)
        assert store.read_active() == account_auth("W1", "U3")
        assert desktop.stopped == desktop.launched == 1
    assert store.read_profile(source.codex_home) == rotated
    for p in (first, fourth):
        assert store.read_profile(p.codex_home) == unchanged[p.id]


async def test_target_user_changed_after_preflight_rejects_before_desktop_stop(
    tmp_paths, monkeypatch
):
    target = profile(tmp_paths, "U1")
    store = FileCredentialStore(tmp_paths.shared_codex_home)
    store.write_active_atomic(account_auth("W2", "U2"))
    original = store.read_active()
    desktop = FakeDesktop()

    async def snapshot(_self, home, **_kwargs):
        if Path(home) != store.shared_home:
            # Same workspace, different user between RPC verification and activation.
            (Path(home) / "auth.json").write_bytes(account_auth("W1", "U3"))
            identity = target.identity
        else:
            identity = account_identity("W2", "U2")
        return SimpleNamespace(account_id=identity.workspace_id, identity=identity)

    monkeypatch.setattr(AccountService, "read_snapshot", snapshot)
    monkeypatch.setattr(AccountService, "list_profiles", AsyncMock(return_value=[target]))
    with pytest.raises(AccountMismatchError):
        await AuthTransaction(credential_store=store, desktop=desktop).switch(target)
    assert store.read_active() == original
    assert desktop.stopped == desktop.launched == 0


async def test_unknown_source_does_not_block_verified_manual_target(tmp_paths, monkeypatch):
    target = profile(tmp_paths, "U1")
    store = FileCredentialStore(tmp_paths.shared_codex_home)
    store.write_active_atomic(b"unreadable old credentials")

    async def snapshot(_self, home, **_kwargs):
        raw = store.read_active() if Path(home) == store.shared_home else store.read_profile(home)
        identity = auth_evidence(raw).identity
        return SimpleNamespace(account_id=identity.workspace_id, identity=identity)

    monkeypatch.setattr(AccountService, "read_snapshot", snapshot)
    monkeypatch.setattr(AccountService, "list_profiles", AsyncMock(return_value=[target]))
    desktop = FakeDesktop()
    assert (await AuthTransaction(credential_store=store, desktop=desktop).switch(target)).success
    assert store.read_active() == account_auth("W1", "U1")
    assert desktop.stopped == desktop.launched == 1


@pytest.mark.parametrize("state", [IdentityState.UNKNOWN, IdentityState.CONFLICT])
async def test_unverified_target_is_not_reported_as_a_different_account(
    tmp_paths, monkeypatch, state
):
    target = profile(tmp_paths, "U1")
    store = FileCredentialStore(tmp_paths.shared_codex_home)
    store.write_active_atomic(account_auth("W2", "U2"))
    original = store.read_active()
    desktop = FakeDesktop()

    async def snapshot(_self, home, **_kwargs):
        identity = (
            account_identity("W2", "U2")
            if Path(home) == store.shared_home
            else AccountIdentity(state=state)
        )
        return SimpleNamespace(account_id="W1", identity=identity)

    monkeypatch.setattr(AccountService, "read_snapshot", snapshot)
    monkeypatch.setattr(AccountService, "list_profiles", AsyncMock(return_value=[target]))
    with pytest.raises(AppServerError, match="could not be verified"):
        await AuthTransaction(credential_store=store, desktop=desktop).switch(target)
    assert store.read_active() == original
    assert desktop.stopped == desktop.launched == 0
