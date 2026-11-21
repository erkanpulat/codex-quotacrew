"""Verified GitHub release downloads and user-requested Windows updates."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from codex_account_manager import __version__
from codex_account_manager.core.files import restrict_access
from codex_account_manager.core.operation_lock import OperationLock
from codex_account_manager.core.paths import paths
from codex_account_manager.core.windows_shell import launch_from_explorer
from codex_account_manager.platform import package

REPOSITORY = "erkanpulat/quotacrew-codex-auto-resume"
LEGACY_REPOSITORY = "erkanpulat/codex-quotacrew"
RELEASES_URL = f"https://github.com/{REPOSITORY}/releases"
LEGACY_RELEASES_URL = f"https://github.com/{LEGACY_REPOSITORY}/releases"
API_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
LEGACY_API_URL = f"https://api.github.com/repos/{LEGACY_REPOSITORY}/releases/latest"
MAX_INSTALLER_BYTES = 512 * 1024 * 1024
VERSION = re.compile(r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)\Z")


class UpdateError(Exception):
    pass


def version_tuple(value: str) -> tuple[int, ...]:
    match = VERSION.fullmatch(value)
    if not match:
        raise UpdateError("The release version is invalid.")
    return tuple(map(int, match.groups()))


@dataclass(frozen=True)
class Release:
    version: str
    url: str
    download_url: str
    size: int
    sha256: str

    @property
    def filename(self) -> str:
        return f"QuotaCrew-Setup-{self.version}.exe"


def parse_release(data: dict, current: str = __version__) -> Release | None:
    if data.get("draft") is not False or data.get("prerelease") is not False:
        raise UpdateError("The release is not a stable published version.")
    tag = data.get("tag_name")
    if not isinstance(tag, str):
        raise UpdateError("The release version is invalid.")
    if version_tuple(tag) <= version_tuple(current):
        return None
    version = tag.removeprefix("v")
    page = data.get("html_url")
    if page == f"{RELEASES_URL}/tag/{tag}":
        release_root = RELEASES_URL
    elif page == f"{LEGACY_RELEASES_URL}/tag/{tag}":
        release_root = LEGACY_RELEASES_URL
    else:
        raise UpdateError("The release source could not be verified.")
    filename = f"QuotaCrew-Setup-{version}.exe"
    assets = data.get("assets")
    if not isinstance(assets, list):
        raise UpdateError("A verified Windows installer is not available for this release.")
    matches = [a for a in assets if isinstance(a, dict) and a.get("name") == filename]
    if len(matches) != 1:
        raise UpdateError("A verified Windows installer is not available for this release.")
    asset = matches[0]
    url = f"{release_root}/download/{tag}/{filename}"
    size, digest = asset.get("size"), asset.get("digest")
    if (
        asset.get("browser_download_url") != url
        or asset.get("state") != "uploaded"
        or type(size) is not int
        or not 0 < size <= MAX_INSTALLER_BYTES
        or not isinstance(digest, str)
        or not re.fullmatch(r"sha256:[a-fA-F0-9]{64}", digest)
    ):
        raise UpdateError("A verified Windows installer is not available for this release.")
    return Release(version, page, url, size, digest[7:].lower())


def _allowed_url(url: str) -> bool:
    parsed = urlsplit(url)
    return (
        parsed.scheme == "https"
        and parsed.hostname
        in {
            "api.github.com",
            "github.com",
            "release-assets.githubusercontent.com",
            "objects.githubusercontent.com",
        }
        and parsed.port in {None, 443}
        and parsed.username is None
        and parsed.password is None
    )


class _GitHubRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _allowed_url(newurl):
            raise UpdateError("The download destination could not be verified.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(url: str):
    if not _allowed_url(url):
        raise UpdateError("The download destination could not be verified.")
    request = urllib.request.Request(url, headers={"User-Agent": f"QuotaCrew/{__version__}"})
    return urllib.request.build_opener(_GitHubRedirects()).open(request, timeout=15)


def latest_release() -> Release | None:
    _require_direct_distribution()
    last_error: Exception | None = None
    for api_url in (API_URL, LEGACY_API_URL):
        try:
            with _open(api_url) as response:
                body = response.read(1024 * 1024 + 1)
            if len(body) > 1024 * 1024:
                raise UpdateError("The release response is too large.")
            data = json.loads(body)
            if not isinstance(data, dict):
                raise ValueError()
            return parse_release(data)
        except (OSError, ValueError, urllib.error.URLError) as exc:
            last_error = exc
    raise UpdateError("Updates could not be checked. Try again later.") from last_error


def installed_directory() -> Path | None:
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return None

    import winreg

    key = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\{7C2E5F3A-1D2B-4E6A-9C11-590DBC049B83}}_is1"
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY
        ) as handle:
            directory = Path(winreg.QueryValueEx(handle, "InstallLocation")[0]).resolve()
        return (
            directory
            if Path(sys.executable).resolve()
            in {directory / "QuotaCrew.exe", directory / "CodexAccountManager.exe"}
            else None
        )
    except (OSError, ValueError):
        return None


def cleanup_downloads(keep: Path | None = None) -> None:
    directory = paths.data_dir / "updates"
    if not directory.is_dir() or directory.is_symlink():
        return
    root = directory.resolve()
    for entry in directory.iterdir():
        if entry == keep or entry.is_symlink() or not entry.is_file():
            continue
        if entry.resolve().parent != root:
            continue
        if (
            re.fullmatch(r"(?:QuotaCrew|CodexAccountManager)-Setup-\d+\.\d+\.\d+\.exe", entry.name)
            or entry.suffix == ".part"
        ):
            try:
                entry.unlink()
            except OSError:
                pass


def _download(release: Release, cancelled: threading.Event) -> Path:
    version_tuple(release.version)
    if (
        not re.fullmatch(r"[a-f0-9]{64}", release.sha256)
        or not 0 < release.size <= MAX_INSTALLER_BYTES
    ):
        raise UpdateError("The installer metadata is invalid.")
    expected_urls = {
        f"{release_root}/download/{tag}/{release.filename}"
        for release_root in (RELEASES_URL, LEGACY_RELEASES_URL)
        for tag in (release.version, "v" + release.version)
    }
    if release.download_url not in expected_urls:
        raise UpdateError("The release source could not be verified.")
    directory = paths.data_dir / "updates"
    directory.mkdir(parents=True, exist_ok=True)
    restrict_access(directory)
    destination = directory / release.filename
    cleanup_downloads(keep=destination)
    temporary: Path | None = None
    try:
        deadline = time.monotonic() + 300
        digest = hashlib.sha256()
        received = 0
        with tempfile.NamedTemporaryFile(dir=directory, suffix=".part", delete=False) as output:
            temporary = Path(output.name)
            restrict_access(temporary)
            with _open(release.download_url) as response:
                while True:
                    if cancelled.is_set() or time.monotonic() >= deadline:
                        raise UpdateError("The update download was cancelled or timed out.")
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > release.size:
                        raise UpdateError("The downloaded installer could not be verified.")
                    digest.update(chunk)
                    output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if cancelled.is_set() or received != release.size or digest.hexdigest() != release.sha256:
            raise UpdateError("The downloaded installer could not be verified.")
        temporary.replace(destination)
        return destination
    except (OSError, urllib.error.URLError) as exc:
        raise UpdateError("The update could not be downloaded. Try again later.") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


async def download(release: Release) -> Path:
    _require_direct_distribution()
    cancelled = threading.Event()
    try:
        return await asyncio.to_thread(_download, release, cancelled)
    finally:
        cancelled.set()


def begin_install(release: Release, installer: Path) -> OperationLock:
    _require_direct_distribution()
    directory = installed_directory()
    if directory is None:
        raise UpdateError("Portable and source installations are updated from the release page.")
    if version_tuple(release.version) <= version_tuple(__version__):
        raise UpdateError("This version is already installed.")
    expected = paths.data_dir / "updates" / release.filename
    if installer.resolve() != expected.resolve() or installer.stat().st_size != release.size:
        raise UpdateError("The downloaded installer could not be verified.")
    with installer.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != release.sha256:
            raise UpdateError("The downloaded installer could not be verified.")
    lock = OperationLock(paths.data_dir / "account-operation.lock")
    lock.__enter__()
    try:
        with closing(sqlite3.connect(paths.db_path.as_uri() + "?mode=ro", uri=True)) as source:
            if source.execute("SELECT count(*) FROM pending_continuations").fetchone()[0]:
                raise UpdateError("Continuation is pending. Install the update after it finishes.")
            if source.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise UpdateError("The account database could not be verified.")
            backup_dir = paths.data_dir / "backups"
            backup_dir.mkdir(exist_ok=True)
            with tempfile.NamedTemporaryFile(
                prefix="before-update-", suffix=".db.bak", dir=backup_dir, delete=False
            ) as handle:
                backup = Path(handle.name)
                restrict_access(backup)
            with closing(sqlite3.connect(backup)) as target:
                source.backup(target)
            backups = sorted(
                backup_dir.glob("before-update-*.db.bak"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            for old in backups[2:]:
                if (
                    old.is_file()
                    and not old.is_symlink()
                    and old.resolve().parent == backup_dir.resolve()
                ):
                    old.unlink(missing_ok=True)
        launch_from_explorer(
            installer,
            [
                "/SILENT",
                "/NORESTART",
                "/NOCLOSEAPPLICATIONS",
                "/REOPENAPP=1",
                f"/WAITFORPID={os.getpid()}",
                f"/DIR={directory}",
            ],
        )
        return lock
    except Exception:
        lock.__exit__(None, None, None)
        raise


def _require_direct_distribution() -> None:
    if package.is_packaged():
        raise UpdateError("Updates for this installation are managed by Microsoft Store.")
