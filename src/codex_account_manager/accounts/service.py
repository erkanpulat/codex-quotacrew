"""AccountService — the structured API the CLI, GUI and watcher all use.

Returns typed snapshots (``AccountSnapshot``, ``ProfileHealth``) instead of
rendered tables. The monitoring watcher uses the same snapshots as the UI.

The service is constructed with an App Server *factory* so tests can inject a
fake adapter without a real Codex install.
"""

from __future__ import annotations

import asyncio
import shutil
import sqlite3
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from codex_account_manager.adapters.app_server import CodexAppServer
from codex_account_manager.adapters.credential_store import FileCredentialStore
from codex_account_manager.adapters.interfaces import AppServerAdapter
from codex_account_manager.codex.quota import evaluate_quota
from codex_account_manager.core.child_process import (
    ChildProcessLifetime,
    finish_cleanup,
    stop_owned_process,
)
from codex_account_manager.core.errors import (
    AccountMismatchError,
    AppServerError,
    OperationBusyError,
    ProfileNotFoundError,
    SignedOutError,
    SignInRequiredError,
)
from codex_account_manager.core.files import restrict_access
from codex_account_manager.core.logging import get_logger
from codex_account_manager.core.operation_lock import OperationLock
from codex_account_manager.core.paths import paths
from codex_account_manager.core.protection import CredentialProtectionError
from codex_account_manager.core.redaction import redact_text
from codex_account_manager.domain.models import AccountSnapshot, Profile, ProfileHealth
from codex_account_manager.domain.states import QuotaState
from codex_account_manager.storage.repositories import ProfileRepository

log = get_logger(__name__)

AppServerFactory = Callable[[str], AppServerAdapter]

_PROFILE_CONFIG = 'cli_auth_credentials_store = "file"\n'
#: Upper bound on concurrent App Server reads to keep the machine responsive.
_MAX_CONCURRENT_READS = 4
ACCOUNT_READ_TIMEOUT = 45.0


def _default_factory(codex_home: str) -> AppServerAdapter:
    return CodexAppServer(
        codex_home,
        refresh_on_unauthorized=Path(codex_home).resolve().parent == paths.profiles_dir.resolve(),
    )


async def _safe_aclose(adapter: AppServerAdapter) -> None:
    try:
        await adapter.aclose()
    except CredentialProtectionError:
        raise
    except Exception:
        pass


class AccountService:
    def __init__(
        self,
        profiles: ProfileRepository | None = None,
        *,
        app_server_factory: AppServerFactory | None = None,
        credential_store: FileCredentialStore | None = None,
    ):
        self.profiles = profiles or ProfileRepository()
        self._factory = app_server_factory or _default_factory
        self.credentials = credential_store or FileCredentialStore()
        self._last_health: dict[str, tuple[str | None, ProfileHealth]] = {}
        self._health_task: asyncio.Task[list[ProfileHealth]] | None = None

    async def list_profiles(self) -> list[Profile]:
        return await self.profiles.list()

    async def create_profile(self, alias: str) -> Profile:
        alias = _validate_alias(alias)
        profile_id = str(uuid4())
        codex_home = paths.profiles_dir / profile_id
        paths.profiles_dir.mkdir(parents=True, exist_ok=True)
        codex_home.mkdir(parents=True, exist_ok=False)
        try:
            restrict_access(codex_home)
            (codex_home / "config.toml").write_text(_PROFILE_CONFIG, encoding="utf-8")
            profile = Profile(id=profile_id, alias=alias, codex_home=str(codex_home))
            await self.profiles.create(profile)
        except sqlite3.IntegrityError as exc:
            shutil.rmtree(codex_home, ignore_errors=True)
            raise ValueError("A profile with this name already exists.") from exc
        except Exception:
            shutil.rmtree(codex_home, ignore_errors=True)
            raise
        return profile

    async def rename_profile(self, alias: str, new_alias: str) -> None:
        profile = await self._require(alias)
        try:
            await self.profiles.rename(profile.id, _validate_alias(new_alias))
        except sqlite3.IntegrityError as exc:
            raise ValueError("A profile with this name already exists.") from exc

    async def remove_profile(self, alias: str, *, delete_files: bool = True) -> None:
        profile = await self._require(alias)
        with OperationLock(paths.data_dir / "account-operation.lock"):
            home = Path(profile.codex_home)
            managed = paths.profiles_dir.resolve()
            resolved = home.resolve()
            if delete_files and (
                home.is_symlink()
                or resolved.parent != managed
                or resolved == paths.shared_codex_home.resolve()
            ):
                raise ValueError("Refusing to delete a directory outside managed profiles.")
            if delete_files and home.exists():
                shutil.rmtree(home)
            await self.profiles.delete(profile.id)

    async def login_profile(self, alias: str) -> str:
        import subprocess
        import sys

        from codex_account_manager.codex.runtime import codex_command, profile_environment

        profile = await self._require(alias)
        creationflags = 0
        if sys.platform == "win32":
            creationflags = subprocess.CREATE_NEW_CONSOLE
        with OperationLock(paths.data_dir / "account-operation.lock"):
            with self.credentials.profile_session(profile.codex_home, signing_in=True):
                process = await asyncio.create_subprocess_exec(
                    *codex_command("login"),
                    env=profile_environment(profile.codex_home),
                    creationflags=creationflags,
                )
                lifetime = ChildProcessLifetime()
                try:
                    lifetime.attach(process.pid)
                    code = await asyncio.wait_for(process.wait(), timeout=600)
                except TimeoutError:
                    raise ValueError(
                        "Codex sign-in timed out after 10 minutes. Try signing in again."
                    ) from None
                finally:
                    await finish_cleanup(stop_owned_process(process, lifetime))
                if (Path(profile.codex_home) / "auth.json").exists():
                    restrict_access(Path(profile.codex_home) / "auth.json")
            if code != 0:
                log.warning("Codex sign-in exited with code %s", code)
                raise ValueError(
                    "Codex sign-in failed. Check Codex in Settings > System check, then try again."
                )
            return await self._bind_current_account(alias)

    async def bind_current_account(self, alias: str) -> str:
        with OperationLock(paths.data_dir / "account-operation.lock"):
            return await self._bind_current_account(alias)

    async def _bind_current_account(self, alias: str) -> str:
        profile = await self._require(alias)
        snapshot = await self.read_snapshot(profile.codex_home)
        if not snapshot.account_id:
            raise AccountMismatchError("Could not read an account id to bind.")
        try:
            await self.profiles.bind_account(profile.id, snapshot.account_id)
        except sqlite3.IntegrityError as exc:
            raise ValueError("This account is already bound to another profile.") from exc
        return snapshot.account_id

    async def read_snapshot(
        self, codex_home: str | Path, *, allow_refresh: bool = True
    ) -> AccountSnapshot:
        home_key = str(Path(codex_home).resolve()).casefold()
        digest = sha256(home_key.encode()).hexdigest()[:24]
        with OperationLock(paths.data_dir / f"account-read-{digest}.lock"):
            adapter = self._factory(str(codex_home))
            if not allow_refresh and isinstance(adapter, CodexAppServer):
                adapter.refresh_on_unauthorized = False
            try:
                try:
                    async with asyncio.timeout(ACCOUNT_READ_TIMEOUT):
                        await adapter.start()
                        return await adapter.read_account()
                except TimeoutError as exc:
                    raise AppServerError("Account check timed out. Try refreshing again.") from exc
            finally:
                await asyncio.shield(_safe_aclose(adapter))

    async def health(self, alias: str) -> ProfileHealth:
        profile = await self._require(alias)
        return (await self._health_batch([profile]))[0]

    async def all_health(self) -> list[ProfileHealth]:
        if self._health_task is None or self._health_task.done():
            self._health_task = asyncio.create_task(self._all_health())
        return list(await asyncio.shield(self._health_task))

    async def _all_health(self) -> list[ProfileHealth]:
        profiles = await self.profiles.list()
        profile_ids = {p.id for p in profiles}
        self._last_health = {
            key: value for key, value in self._last_health.items() if key in profile_ids
        }
        return await self._health_batch(profiles)

    async def _health_batch(self, profiles: list[Profile]) -> list[ProfileHealth]:
        if not profiles:
            return []
        try:
            with OperationLock(paths.data_dir / "account-operation.lock"):
                allow_refresh = True
                try:
                    active = await self.read_snapshot(paths.shared_codex_home)
                    if not active.account_id and self.credentials.active_auth_path.exists():
                        raise AppServerError("The active account identity is unavailable.")
                except SignedOutError:
                    active = None
                except Exception:
                    allow_refresh = not self.credentials.active_auth_path.exists()
                    active = None
                active_id = active.account_id if active else None
                semaphore = asyncio.Semaphore(_MAX_CONCURRENT_READS)

                async def bounded(profile: Profile) -> ProfileHealth:
                    async with semaphore:
                        return await self._health_for(
                            profile,
                            active_account_id=active_id,
                            active_snapshot=active,
                            allow_refresh=allow_refresh,
                        )

                return list(await asyncio.gather(*(bounded(p) for p in profiles)))
        except OperationBusyError:
            raise
        except Exception as exc:
            return [self._failed_health(p, exc, active_account_id=None) for p in profiles]

    async def _health_for(
        self,
        profile: Profile,
        *,
        active_account_id: str | None,
        active_snapshot: AccountSnapshot | None = None,
        allow_refresh: bool = True,
    ) -> ProfileHealth:
        auth_present = self.credentials.profile_auth_path(profile.codex_home).exists()
        try:
            is_active = bool(
                profile.bound_account_id and profile.bound_account_id == active_account_id
            )
            snapshot = (
                active_snapshot
                if is_active and active_snapshot
                else (
                    await self.read_snapshot(profile.codex_home)
                    if allow_refresh
                    else await self.read_snapshot(profile.codex_home, allow_refresh=False)
                )
            )
            if profile.bound_account_id and not snapshot.account_id:
                raise AppServerError(
                    "Codex did not return an account identity. Try checking again."
                )
            decision = evaluate_quota(snapshot)
            match = (
                None
                if not profile.bound_account_id
                else profile.bound_account_id == snapshot.account_id
            )
            health = ProfileHealth(
                alias=profile.alias,
                profile_id=profile.id,
                plan_type=snapshot.plan_type,
                subscription_until=snapshot.subscription_until if match is True else None,
                subscription_checked_at=snapshot.subscription_checked_at if match is True else None,
                email=snapshot.email if match is True else None,
                reset_credits=snapshot.reset_credits if match is True else None,
                primary_used_percent=snapshot.primary_used_percent,
                secondary_used_percent=snapshot.secondary_used_percent,
                primary_window_minutes=snapshot.primary_window_minutes,
                secondary_window_minutes=snapshot.secondary_window_minutes,
                primary_resets_at=snapshot.primary_resets_at,
                secondary_resets_at=snapshot.secondary_resets_at,
                ordinary_usage_allowed=snapshot.ordinary_usage_allowed,
                auth_present=auth_present,
                account_match=match,
                is_active=is_active,
                quota_state=decision.state,
                last_checked_at=datetime.now(UTC),
            )
            if match is True:
                self._last_health[profile.id] = (profile.bound_account_id, health)
            else:
                self._last_health.pop(profile.id, None)
            return health
        except Exception as exc:
            return self._failed_health(profile, exc, active_account_id=active_account_id)

    def _failed_health(
        self, profile: Profile, error: Exception, *, active_account_id: str | None
    ) -> ProfileHealth:
        auth_present = self.credentials.profile_auth_path(profile.codex_home).exists()
        reauth = isinstance(error, SignInRequiredError)
        cached = self._last_health.get(profile.id)
        previous = cached[1] if cached and cached[0] == profile.bound_account_id else None
        if previous and auth_present and not reauth:
            return replace(
                previous,
                alias=profile.alias,
                stale=True,
                is_active=bool(
                    profile.bound_account_id and profile.bound_account_id == active_account_id
                ),
                quota_state=QuotaState.UNKNOWN,
                ordinary_usage_allowed=None,
                error=redact_text(str(error)),
            )
        self._last_health.pop(profile.id, None)
        return ProfileHealth(
            alias=profile.alias,
            profile_id=profile.id,
            plan_type=None,
            primary_used_percent=None,
            secondary_used_percent=None,
            primary_resets_at=None,
            secondary_resets_at=None,
            ordinary_usage_allowed=None,
            auth_present=auth_present,
            account_match=None,
            is_active=bool(
                profile.bound_account_id and profile.bound_account_id == active_account_id
            ),
            quota_state=QuotaState.UNKNOWN,
            last_checked_at=None,
            error=redact_text(str(error)),
            reauth_required=reauth,
        )

    async def active_account_id(self) -> str | None:
        """Account id currently active in the shared home."""
        snapshot = await self.read_snapshot(paths.shared_codex_home)
        return snapshot.account_id

    async def _active_account_id_safe(self) -> str | None:
        try:
            return await self.active_account_id()
        except Exception:
            return None

    async def resolve_active_alias(self) -> str | None:
        active_id = await self._active_account_id_safe()
        if not active_id:
            return None
        for profile in await self.profiles.list():
            if profile.bound_account_id == active_id:
                return profile.alias
        return None

    async def _require(self, alias: str) -> Profile:
        profile = await self.profiles.get_by_alias(alias)
        if not profile:
            raise ProfileNotFoundError(f"Profile not found: {alias}")
        return profile


def _validate_alias(alias: str) -> str:
    alias = alias.strip()
    if not alias or len(alias) > 64 or any(ord(char) < 32 or ord(char) == 127 for char in alias):
        raise ValueError("Use a profile name of 1–64 characters without control characters.")
    return alias
