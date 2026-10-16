import sqlite3
import subprocess
import sys

import pytest

from codex_account_manager.core.files import restrict_access

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows file permissions")


def assert_current_user_only(path):
    import win32api
    import win32con
    import win32security

    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        user = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    finally:
        token.Close()
    descriptor = win32security.GetNamedSecurityInfo(
        str(path), win32security.SE_FILE_OBJECT, win32security.DACL_SECURITY_INFORMATION
    )
    acl = descriptor.GetSecurityDescriptorDacl()
    assert acl is not None and acl.GetAceCount() > 0
    for index in range(acl.GetAceCount()):
        ace = acl.GetAce(index)
        assert ace[0][0] == win32security.ACCESS_ALLOWED_ACE_TYPE
        assert ace[2] == user


def test_data_directory_permissions_reach_new_nested_files(tmp_paths):
    nested = tmp_paths.profiles_dir / "synthetic" / "nested"
    nested.mkdir(parents=True)
    file = nested / "metadata.json"
    file.write_text("synthetic")
    for path in (tmp_paths.data_dir, tmp_paths.logs_dir, nested, file):
        assert_current_user_only(path)


def test_existing_profile_permissions_reach_new_files_after_upgrade(tmp_paths):
    import win32con
    import win32security

    profile = tmp_paths.profiles_dir / "existing"
    profile.mkdir()
    original = profile / "config.toml"
    original.write_text("synthetic settings")
    descriptor = win32security.GetNamedSecurityInfo(
        str(profile), win32security.SE_FILE_OBJECT, win32security.OWNER_SECURITY_INFORMATION
    )
    old_acl = win32security.ACL()
    old_acl.AddAccessAllowedAce(
        win32security.ACL_REVISION, win32con.GENERIC_ALL, descriptor.GetSecurityDescriptorOwner()
    )
    win32security.SetNamedSecurityInfo(
        str(profile),
        win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
        None,
        None,
        old_acl,
        None,
    )

    tmp_paths.ensure()
    nested = profile / "new"
    nested.mkdir()
    metadata = nested / "metadata.json"
    metadata.write_text("synthetic metadata")
    assert_current_user_only(nested)
    assert_current_user_only(metadata)
    assert original.read_text() == "synthetic settings"


def test_permission_failure_is_reported_as_filesystem_error(tmp_path, monkeypatch):
    import pywintypes
    import win32security

    file = tmp_path / "synthetic.json"
    file.write_text("unchanged")

    def denied(*args):
        raise pywintypes.error(5, "SetNamedSecurityInfo", "Access denied")

    monkeypatch.setattr(win32security, "SetNamedSecurityInfo", denied)
    with pytest.raises(OSError, match="Could not protect"):
        restrict_access(file)
    assert file.read_text() == "unchanged"


def test_existing_database_permissions_and_live_sqlite_sidecars(tmp_paths):
    import win32security

    tmp_paths.db_path.write_bytes(b"")
    acl = win32security.ACL()
    acl.AddAccessAllowedAce(
        win32security.ACL_REVISION,
        0x1F01FF,
        win32security.CreateWellKnownSid(win32security.WinWorldSid),
    )
    win32security.SetNamedSecurityInfo(
        str(tmp_paths.db_path),
        win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
        None,
        None,
        acl,
        None,
    )
    tmp_paths.ensure()
    assert_current_user_only(tmp_paths.db_path)
    with sqlite3.connect(tmp_paths.db_path) as database:
        database.execute("PRAGMA journal_mode=WAL")
        database.execute("CREATE TABLE synthetic (value TEXT)")
        database.execute("INSERT INTO synthetic VALUES ('test')")
        database.commit()
        for suffix in ("-wal", "-shm"):
            path = tmp_paths.db_path.with_name(tmp_paths.db_path.name + suffix)
            assert path.exists()
            assert_current_user_only(path)


@pytest.mark.parametrize("location", ["direct", "profile", "data", "database"])
def test_junction_permissions_are_rejected_without_changing_target(
    tmp_path, tmp_paths, monkeypatch, location
):
    target = tmp_path / "external"
    target.mkdir()
    original = target / "keep.txt"
    original.write_text("unchanged")
    junction = (
        tmp_paths.db_path
        if location == "database"
        else (tmp_paths.profiles_dir if location == "profile" else tmp_path) / "redirected"
    )
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(target)],
        capture_output=True,
        check=False,
    )
    if result.returncode:
        pytest.skip("Test volume does not support directory junctions")
    import win32security

    def descriptor():
        dacl = win32security.GetNamedSecurityInfo(
            str(target), win32security.SE_FILE_OBJECT, win32security.DACL_SECURITY_INFORMATION
        ).GetSecurityDescriptorDacl()
        return [dacl.GetAce(index) for index in range(dacl.GetAceCount())]

    before = descriptor()
    if location in {"data", "database"}:
        import importlib

        paths_module = importlib.import_module("codex_account_manager.core.paths")
        data_dir = junction if location == "data" else tmp_paths.data_dir
        monkeypatch.setattr(paths_module, "user_data_dir", lambda *args, **kwargs: str(data_dir))
        tmp_paths = paths_module._resolve.__wrapped__()
    try:
        with pytest.raises(OSError, match="redirected"):
            if location != "direct":
                tmp_paths.ensure()
            else:
                restrict_access(junction)
        assert descriptor() == before
        assert original.read_text() == "unchanged"
    finally:
        junction.rmdir()
