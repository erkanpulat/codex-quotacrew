import hashlib
import io
import json
import threading
from dataclasses import replace
from unittest.mock import Mock

import pytest

from codex_account_manager import updates
from codex_account_manager.core.errors import OperationBusyError
from codex_account_manager.core.operation_lock import OperationLock
from codex_account_manager.storage.database import connect
from codex_account_manager.storage.repositories import SettingsRepository
from tests.fakes import account_identity

PACKAGE = b"synthetic installer; never executed"


def test_download_cleanup_is_scoped_and_preserves_unrelated_files(tmp_paths):
    directory = tmp_paths.data_dir / "updates"
    directory.mkdir()
    old = directory / "CodexAccountManager-Setup-0.1.0.exe"
    keep = directory / "QuotaCrew-Setup-2.0.0.exe"
    partial = directory / "interrupted.part"
    obsolete = directory / "QuotaCrew-Setup-1.0.0.exe"
    unrelated = directory / "my-file.txt"
    for path in (old, keep, partial, obsolete, unrelated):
        path.write_bytes(b"data")
    updates.cleanup_downloads(keep)
    assert not old.exists() and not partial.exists()
    assert not obsolete.exists()
    assert keep.exists() and unrelated.exists()
    updates.cleanup_downloads()
    assert not keep.exists() and unrelated.exists()


def metadata(version="2.0.0"):
    filename = f"QuotaCrew-Setup-{version}.exe"
    return {
        "tag_name": "v" + version,
        "draft": False,
        "prerelease": False,
        "html_url": f"{updates.RELEASES_URL}/tag/v{version}",
        "assets": [
            {
                "name": filename,
                "state": "uploaded",
                "size": len(PACKAGE),
                "digest": "sha256:" + hashlib.sha256(PACKAGE).hexdigest(),
                "browser_download_url": f"{updates.RELEASES_URL}/download/v{version}/{filename}",
            }
        ],
    }


@pytest.mark.parametrize(
    "remote,current,newer",
    [
        ("2.0.0", "1.9.9", True),
        ("0.10.0", "0.9.0", True),
        ("1.0.0", "1.0.0", False),
        ("1.0.0", "2.0.0", False),
    ],
)
def test_release_versions_are_compared_numerically(remote, current, newer):
    assert bool(updates.parse_release(metadata(remote), current)) == newer


@pytest.mark.parametrize(
    "fault",
    [
        "draft",
        "prerelease",
        "tag",
        "source",
        "url",
        "digest",
        "missing",
        "duplicate",
        "size",
        "boolean-size",
        "huge",
    ],
)
def test_untrusted_or_incomplete_releases_cannot_supply_an_installer(fault):
    data = metadata()
    if fault in {"draft", "prerelease"}:
        data[fault] = True
    elif fault == "tag":
        data["tag_name"] = "../../2.0.0"
    elif fault == "source":
        data["html_url"] = "https://other.example/release"
    elif fault == "url":
        data["assets"][0]["browser_download_url"] = "https://other.example/setup.exe"
    elif fault == "digest":
        data["assets"][0]["digest"] = "sha256:bad"
    elif fault == "missing":
        data["assets"] = []
    elif fault == "duplicate":
        data["assets"] *= 2
    else:
        data["assets"][0]["size"] = {
            "size": -1,
            "boolean-size": True,
            "huge": updates.MAX_INSTALLER_BYTES + 1,
        }[fault]
    with pytest.raises(updates.UpdateError):
        updates.parse_release(data)


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/file",
        "https://evil.example/file",
        "file:///C:/setup.exe",
        "https://github.com.evil.example/file",
        "https://user:password@github.com/file",
        "https://github.com:444/file",
    ],
)
def test_redirects_cannot_escape_https_github_hosts(url):
    with pytest.raises(updates.UpdateError):
        updates._GitHubRedirects().redirect_request(None, None, 302, "", {}, url)


def test_latest_check_is_bounded_and_network_errors_are_safe(monkeypatch):
    opened = Mock(return_value=io.BytesIO(json.dumps(metadata()).encode()))
    monkeypatch.setattr(updates, "_open", opened)
    assert updates.latest_release().version == "2.0.0"
    assert opened.call_args.args == (updates.API_URL,)
    opened.return_value = io.BytesIO(b"x" * (1024 * 1024 + 1))
    with pytest.raises(updates.UpdateError, match="too large"):
        updates.latest_release()
    opened.side_effect = OSError("private network details")
    with pytest.raises(updates.UpdateError) as caught:
        updates.latest_release()
    assert "private" not in str(caught.value)


@pytest.mark.parametrize("fault", [None, "hash", "truncated", "oversized", "cancelled", "network"])
def test_download_verifies_size_hash_cancellation_and_cleans_partial_files(
    tmp_paths, monkeypatch, fault
):
    release = updates.parse_release(metadata())
    content = PACKAGE
    if fault == "hash":
        content = b"x" * len(PACKAGE)
    if fault == "truncated":
        content = PACKAGE[:-1]
    if fault == "oversized":
        content = PACKAGE + b"x"
    opened = Mock(return_value=io.BytesIO(content))
    if fault == "network":
        opened.side_effect = OSError("private URL")
    monkeypatch.setattr(updates, "_open", opened)
    cancelled = threading.Event()
    if fault == "cancelled":
        cancelled.set()
    if fault:
        with pytest.raises(updates.UpdateError):
            updates._download(release, cancelled)
        assert not (tmp_paths.data_dir / "updates" / release.filename).exists()
    else:
        assert updates._download(release, cancelled).read_bytes() == PACKAGE
    assert not list((tmp_paths.data_dir / "updates").glob("*.part"))


async def test_verified_install_preserves_data_and_holds_account_lock_until_exit(
    migrated_db, monkeypatch, tmp_path
):
    release = updates.parse_release(metadata())
    monkeypatch.setattr(updates, "_open", lambda _: io.BytesIO(PACKAGE))
    installer = await updates.download(release)
    destination = tmp_path / "Application with spaces"
    monkeypatch.setattr(updates, "installed_directory", lambda: destination)
    launch = Mock()
    monkeypatch.setattr(updates, "launch_from_explorer", launch)
    await SettingsRepository().set("keep_in_tray", "true")
    lock = updates.begin_install(release, installer)
    try:
        with pytest.raises(OperationBusyError):
            with OperationLock(migrated_db.data_dir / "account-operation.lock"):
                pass
        assert await SettingsRepository().get("keep_in_tray") == "true"
        assert len(list((migrated_db.data_dir / "backups").glob("before-update-*.db.bak"))) == 1
        executable, arguments = launch.call_args.args
        assert executable == installer
        assert "/NOCLOSEAPPLICATIONS" in arguments and "/NORESTART" in arguments
        assert "/REOPENAPP=1" in arguments
        assert any(a.startswith("/WAITFORPID=") for a in arguments)
        assert f"/DIR={destination}" in arguments
    finally:
        lock.__exit__(None, None, None)


@pytest.mark.parametrize(
    "fault", ["pending", "lock", "tampered", "location", "source", "downgrade"]
)
async def test_install_rejects_active_work_tampering_and_unsupported_installations(
    migrated_db, monkeypatch, tmp_path, fault
):
    from codex_account_manager.continuity.automation import (
        ContinuationSupervisor,
        ContinuationTicket,
    )

    release = updates.parse_release(metadata())
    monkeypatch.setattr(updates, "_open", lambda _: io.BytesIO(PACKAGE))
    installer = await updates.download(release)
    monkeypatch.setattr(
        updates, "installed_directory", lambda: None if fault == "source" else tmp_path
    )
    launch = Mock()
    monkeypatch.setattr(updates, "launch_from_explorer", launch)
    held = None
    if fault == "pending":
        await ContinuationSupervisor().save_pending(
            [
                ContinuationTicket(
                    "thread",
                    "turn",
                    "account",
                    "goal",
                    owner_key=account_identity("account").fingerprint,
                )
            ]
        )
    elif fault == "lock":
        held = OperationLock(migrated_db.data_dir / "account-operation.lock")
        held.__enter__()
    elif fault == "tampered":
        installer.write_bytes(b"x" * len(PACKAGE))
    elif fault == "location":
        installer = tmp_path / release.filename
        installer.write_bytes(PACKAGE)
    elif fault == "downgrade":
        release = replace(release, version="0.0.1")
    try:
        with pytest.raises((updates.UpdateError, OperationBusyError)):
            updates.begin_install(release, installer)
        launch.assert_not_called()
    finally:
        if held:
            held.__exit__(None, None, None)
    async with connect() as db:
        assert (await (await db.execute("PRAGMA integrity_check")).fetchone()) == ("ok",)
