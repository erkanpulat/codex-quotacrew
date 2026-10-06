"""DPAPI-protected Windows profiles and atomic shared Codex credential writes.

Only the official Codex process receives a temporary plaintext profile file.
Its latest rotated token is sealed after it exits. The shared Codex home remains
in the official client's file format; it is not an application-owned vault.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

from codex_account_manager.core import protection
from codex_account_manager.core.errors import AccountMismatchError
from codex_account_manager.core.files import atomic_write, restrict_access
from codex_account_manager.core.operation_lock import OperationLock
from codex_account_manager.core.paths import paths

_AUTH_FILE = "auth.json"
_FILE_AUTH_SETTING = 'cli_auth_credentials_store = "file"'
_VAULT_FILE = "auth.dpapi"
_PURPOSE = b"QuotaCrew.profile-auth.v1"


def managed_home(home: str | Path) -> bool:
    path = Path(home)
    return not path.is_symlink() and path.resolve().parent == paths.profiles_dir.resolve()


def _checked_file(path: Path) -> Path:
    if path.is_symlink() or path.resolve().parent != path.parent.resolve():
        raise protection.CredentialProtectionError("Refusing a redirected sign-in file.")
    return path


def _seal(home: Path) -> None:
    plain = _checked_file(home / _AUTH_FILE)
    vault = _checked_file(home / _VAULT_FILE)
    if plain.is_file():
        restrict_access(plain)
        encrypted = protection.protect(plain.read_bytes(), _PURPOSE)
        atomic_write(vault, encrypted)
        # Never delete the only usable copy before a verified durable write.
        protection.unprotect(vault.read_bytes(), _PURPOSE)
        plain.unlink()


class ProfileAuthSession:
    """Hold the per-profile lock until the owning CLI process has fully stopped."""

    def __init__(self, home: str | Path, *, signing_in: bool = False):
        self.home = Path(home)
        self.lock: OperationLock | None = None
        self.enabled = protection.available() and managed_home(home)
        self.signing_in = signing_in
        self.materialized = False

    def __enter__(self):
        if not self.enabled:
            return self
        lock = OperationLock(self.home / "auth-session.lock")
        lock.__enter__()
        self.lock = lock
        try:
            # A previous abrupt process exit may have left a newer CLI token.
            _seal(self.home)
            vault = _checked_file(self.home / _VAULT_FILE)
            if vault.exists() and not self.signing_in:
                atomic_write(
                    self.home / _AUTH_FILE, protection.unprotect(vault.read_bytes(), _PURPOSE)
                )
                self.materialized = True
            return self
        except BaseException:
            self.lock.__exit__(None, None, None)
            self.lock = None
            raise

    def __exit__(self, *_exc):
        if self.lock is None:
            return
        try:
            if (self.home / _AUTH_FILE).exists():
                _seal(self.home)
            elif self.materialized:
                # Codex deleted its credential (e.g. logout). Do not resurrect it.
                _checked_file(self.home / _VAULT_FILE).unlink(missing_ok=True)
        except OSError:
            raise protection.CredentialProtectionError(
                "Windows could not finish protecting the sign-in data. Close other account operations and retry."
            ) from None
        finally:
            self.lock.__exit__(None, None, None)
            self.lock = None


class FileCredentialStore:
    """Concrete :class:`CredentialStore` for the shared Codex home."""

    def __init__(self, shared_home: Path | None = None):
        self.shared_home = Path(shared_home) if shared_home else paths.shared_codex_home

    def profile_auth_path(self, codex_home: str | Path) -> Path:
        vault = Path(codex_home) / _VAULT_FILE
        return vault if vault.exists() else Path(codex_home) / _AUTH_FILE

    def profile_session(
        self, codex_home: str | Path, *, signing_in: bool = False
    ) -> ProfileAuthSession:
        if not managed_home(codex_home):
            raise protection.CredentialProtectionError(
                "Profile is outside managed account storage."
            )
        return ProfileAuthSession(codex_home, signing_in=signing_in)

    def read_profile(self, codex_home: str | Path) -> bytes:
        if not managed_home(codex_home):
            raise protection.CredentialProtectionError(
                "Profile is outside managed account storage."
            )
        home = Path(codex_home)
        with OperationLock(home / "auth-session.lock"):
            if protection.available():
                _seal(home)
                return protection.unprotect(
                    _checked_file(home / _VAULT_FILE).read_bytes(), _PURPOSE
                )
            return _checked_file(home / _AUTH_FILE).read_bytes()

    def protect_profiles(self) -> None:
        """Migrate/reseal idle managed profiles after crash recovery, without logging data."""
        if not protection.available() or not paths.profiles_dir.exists():
            return
        from codex_account_manager.core.errors import OperationBusyError

        for home in paths.profiles_dir.iterdir():
            if home.is_dir() and managed_home(home):
                try:
                    with OperationLock(home / "auth-session.lock"):
                        _seal(home)
                except OperationBusyError:
                    continue

    @property
    def active_auth_path(self) -> Path:
        return self.shared_home / _AUTH_FILE

    def read_active(self) -> bytes | None:
        path = self.active_auth_path
        return path.read_bytes() if path.exists() else None

    def write_active_atomic(self, data: bytes) -> None:
        atomic_write(self.active_auth_path, data)

    def copy_profile_to_active(self, codex_home: str | Path) -> bytes | None:
        """Copy a profile's auth into the shared home atomically.

        Returns the previous active bytes (for rollback) or ``None`` if there
        was none. Raises ``FileNotFoundError`` if the profile has no auth.
        """
        source = self.profile_auth_path(codex_home)
        if not source.exists():
            raise FileNotFoundError(f"Profile auth.json not found under {codex_home}")
        previous = self.read_active()
        self.write_active_atomic(self.read_profile(codex_home))
        return previous

    def restore_active(self, data: bytes | None) -> None:
        if data is None:
            self.active_auth_path.unlink(missing_ok=True)
            return
        self.write_active_atomic(data)

    def sync_active_to_profile(
        self, codex_home: str | Path, *, expected_account_id: str | None = None
    ) -> None:
        """Persist the current active auth back into a profile (atomic)."""
        active = self.read_active()
        if active is None:
            return
        if expected_account_id is not None:
            try:
                account_id = json.loads(active)["tokens"]["account_id"]
            except (ValueError, KeyError, TypeError):
                account_id = None
            if account_id != expected_account_id:
                raise AccountMismatchError(
                    "The active credentials changed before they could be saved."
                )
        if not managed_home(codex_home):
            raise protection.CredentialProtectionError(
                "Profile is outside managed account storage."
            )
        home = Path(codex_home)
        with OperationLock(home / "auth-session.lock"):
            if protection.available():
                atomic_write(
                    _checked_file(home / _VAULT_FILE), protection.protect(active, _PURPOSE)
                )
                _checked_file(home / _AUTH_FILE).unlink(missing_ok=True)
            else:
                atomic_write(_checked_file(home / _AUTH_FILE), active)

    def ensure_file_auth_config(self) -> None:
        """Make sure the shared home uses the file credential store."""
        self.shared_home.mkdir(parents=True, exist_ok=True)
        config = self.shared_home / "config.toml"
        text = config.read_text(encoding="utf-8-sig") if config.exists() else ""
        current = tomllib.loads(text)
        if current.get("cli_auth_credentials_store") == "file":
            return
        lines = text.splitlines(keepends=True)
        for index, line in enumerate(lines):
            if line.lstrip().startswith("["):
                break
            key = line.split("=", 1)[0].strip().strip("\"'")
            if key == "cli_auth_credentials_store":
                lines[index] = _FILE_AUTH_SETTING + "\n"
                break
        updated = "".join(lines)
        if tomllib.loads(updated).get("cli_auth_credentials_store") != "file":
            updated = _FILE_AUTH_SETTING + "\n" + updated
        tomllib.loads(updated)
        atomic_write(config, updated.encode("utf-8"))
