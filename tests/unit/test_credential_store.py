from codex_account_manager.adapters.credential_store import FileCredentialStore


def test_sync_cannot_write_another_accounts_credentials_into_profile(tmp_paths):
    import pytest

    from codex_account_manager.core.errors import AccountMismatchError

    store = FileCredentialStore(shared_home=tmp_paths.shared_codex_home)
    home = tmp_paths.profiles_dir / "p1"
    home.mkdir()
    (home / "auth.json").write_bytes(b"original-profile")
    store.write_active_atomic(b'{"tokens":{"account_id":"other"}}')
    with pytest.raises(AccountMismatchError):
        store.sync_active_to_profile(home, expected_account_id="expected")
    assert store.read_profile(home) == b"original-profile"
    store.write_active_atomic(b'{"tokens":{"account_id":"expected"}}')
    store.sync_active_to_profile(home, expected_account_id="expected")
    assert store.read_profile(home) == store.read_active()


def test_atomic_write_and_read(tmp_paths):
    store = FileCredentialStore(shared_home=tmp_paths.shared_codex_home)
    store.write_active_atomic(b'{"fake":"auth"}')
    assert store.read_active() == b'{"fake":"auth"}'
    # No temp file left behind.
    assert not (tmp_paths.shared_codex_home / "auth.json.cx-tmp").exists()


def test_copy_profile_returns_previous_for_rollback(tmp_paths):
    store = FileCredentialStore(shared_home=tmp_paths.shared_codex_home)
    store.write_active_atomic(b"OLD")

    profile_home = tmp_paths.profiles_dir / "p1"
    profile_home.mkdir(parents=True)
    (profile_home / "auth.json").write_bytes(b"NEW")

    previous = store.copy_profile_to_active(profile_home)
    assert previous == b"OLD"
    assert store.read_active() == b"NEW"

    store.restore_active(previous)
    assert store.read_active() == b"OLD"


def test_restore_none_removes_active(tmp_paths):
    store = FileCredentialStore(shared_home=tmp_paths.shared_codex_home)
    store.write_active_atomic(b"X")
    store.restore_active(None)
    assert store.read_active() is None


def test_ensure_file_auth_config_writes_setting(tmp_paths):
    store = FileCredentialStore(shared_home=tmp_paths.shared_codex_home)
    store.ensure_file_auth_config()
    text = (tmp_paths.shared_codex_home / "config.toml").read_text(encoding="utf-8")
    assert 'cli_auth_credentials_store = "file"' in text
