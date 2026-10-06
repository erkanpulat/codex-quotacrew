"""Account switching with a durable recovery snapshot and interprocess exclusion."""

from __future__ import annotations

import asyncio
import base64
import json
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from codex_account_manager.adapters.credential_store import FileCredentialStore
from codex_account_manager.adapters.desktop import WindowsDesktopLauncher
from codex_account_manager.adapters.interfaces import DesktopLauncher
from codex_account_manager.core import protection
from codex_account_manager.core.errors import AccountMismatchError, TransactionError
from codex_account_manager.core.events import bus
from codex_account_manager.core.files import atomic_write
from codex_account_manager.core.logging import get_logger
from codex_account_manager.core.operation_lock import OperationLock
from codex_account_manager.core.paths import paths
from codex_account_manager.core.redaction import redact_text
from codex_account_manager.domain.models import Profile
from codex_account_manager.domain.states import TransactionStage

log = get_logger(__name__)
_RECOVERY_PURPOSE = b"QuotaCrew.switch-recovery.v1"


@dataclass
class SwitchResult:
    success: bool
    final_stage: TransactionStage
    rolled_back: bool = False
    account_id: str | None = None
    detail: str | None = None


@dataclass
class _Journal:
    profile_id: str
    profile_alias: str
    stages: list[dict] = field(default_factory=list)
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def record(self, stage: TransactionStage, ok: bool = True, detail: str | None = None) -> None:
        self.stages.append(
            {
                "stage": stage.value,
                "ok": ok,
                "detail": redact_text(detail or ""),
                "at": datetime.now(UTC).isoformat(),
            }
        )
        atomic_write(
            paths.data_dir / "switch.journal.json",
            json.dumps(
                {
                    "profile_id": self.profile_id,
                    "profile_alias": self.profile_alias,
                    "started_at": self.started_at,
                    "stages": self.stages,
                }
            ).encode(),
        )

    def clear(self) -> None:
        try:
            (paths.data_dir / "switch.journal.json").unlink(missing_ok=True)
        except OSError:
            log.warning("Could not remove the completed switch journal.")


class AuthTransaction:
    def __init__(
        self,
        *,
        credential_store: FileCredentialStore | None = None,
        desktop: DesktopLauncher | None = None,
        verify_account=None,
        wait_ready_timeout: float = 40.0,
        launch_desktop: bool | None = None,
    ):
        self.credentials = credential_store or FileCredentialStore()
        self.desktop = desktop if desktop is not None else _default_desktop()
        self._verify_account = verify_account
        self.wait_ready_timeout = wait_ready_timeout
        self._launch_requested = launch_desktop
        self.launch_desktop = launch_desktop is not False

    @property
    def recovery_path(self) -> Path:
        return paths.data_dir / "switch.recovery.json"

    def _snapshot(self) -> None:
        payload: dict = {
            "home": str(self.credentials.shared_home.resolve()),
            "desktop_running": self.desktop.is_running(),
            "credentials_may_have_changed": False,
        }
        for name in ("auth.json", "config.toml"):
            source = self.credentials.shared_home / name
            payload[name] = (
                base64.b64encode(source.read_bytes()).decode() if source.exists() else None
            )
        self._write_recovery(payload)

    def _write_recovery(self, payload: dict) -> None:
        atomic_write(
            self.recovery_path, protection.protect(json.dumps(payload).encode(), _RECOVERY_PURPOSE)
        )

    def _read_recovery(self) -> dict:
        raw = self.recovery_path.read_bytes()
        if raw.startswith(protection.MAGIC):
            return json.loads(protection.unprotect(raw, _RECOVERY_PURPOSE))
        # Retain compatibility with pre-encryption snapshots. Encrypt before any
        # fallible restore operation so another interrupted recovery stays safe.
        payload = json.loads(raw)
        if protection.available():
            self._write_recovery(payload)
        return payload

    def _arm_recovery(self) -> None:
        payload = self._read_recovery()
        # Desktop may rotate credentials while shutting down. Roll back to its final state.
        for name in ("auth.json", "config.toml"):
            source = self.credentials.shared_home / name
            payload[name] = (
                base64.b64encode(source.read_bytes()).decode() if source.exists() else None
            )
        payload["credentials_may_have_changed"] = True
        self._write_recovery(payload)

    def _restore(self) -> None:
        payload = self._read_recovery()
        if payload.get("home") != str(self.credentials.shared_home.resolve()):
            raise TransactionError(
                "Recovery snapshot belongs to a different Codex home.", stage="rollback"
            )
        # Validate the entire snapshot before making any changes.
        decoded = {
            name: base64.b64decode(payload[name], validate=True)
            if payload[name] is not None
            else None
            for name in ("auth.json", "config.toml")
        }
        if payload.get("credentials_may_have_changed") is False:
            if (
                payload.get("desktop_running")
                and self.launch_desktop
                and not self.desktop.is_running()
            ):
                self.desktop.launch()
            self.recovery_path.unlink()
            return
        self.desktop.stop()
        self.credentials.restore_active(decoded["auth.json"])
        config = self.credentials.shared_home / "config.toml"
        if decoded["config.toml"] is None:
            config.unlink(missing_ok=True)
        else:
            atomic_write(config, decoded["config.toml"])
        if payload.get("desktop_running") and self.launch_desktop:
            self.desktop.launch()
        self.recovery_path.unlink()

    def recover(self) -> bool:
        """Restore an interrupted switch before starting the GUI or another switch."""
        with OperationLock(paths.data_dir / "account-operation.lock"):
            if not self.recovery_path.exists():
                self.credentials.protect_profiles()
                return False
            self._restore()
            (paths.data_dir / "switch.journal.json").unlink(missing_ok=True)
            self.credentials.protect_profiles()
            return True

    async def switch(
        self,
        target: Profile,
        *,
        after_switch: Callable[[], Awaitable[None]] | None = None,
        before_desktop_stop: Callable[[], Awaitable[None]] | None = None,
    ) -> SwitchResult:
        with OperationLock(paths.data_dir / "account-operation.lock"):
            if self.recovery_path.exists():
                self._restore()
            return await self._switch_locked(
                target, after_switch=after_switch, before_desktop_stop=before_desktop_stop
            )

    async def _switch_locked(
        self,
        target: Profile,
        *,
        after_switch: Callable[[], Awaitable[None]] | None,
        before_desktop_stop: Callable[[], Awaitable[None]] | None = None,
    ) -> SwitchResult:
        if not target.bound_account_id:
            raise TransactionError(
                f"Profile '{target.alias}' is not bound to an account.", stage="prepare"
            )
        source = self.credentials.profile_auth_path(target.codex_home)
        if not source.is_file():
            raise TransactionError(
                f"Profile '{target.alias}' has no auth.json to activate.", stage="prepare"
            )
        if isinstance(self.desktop, WindowsDesktopLauncher):
            if self._launch_requested is None:
                from codex_account_manager.platform.windows import is_desktop_installed

                # Desktop is an optional surface. CLI and IDE account activation
                # must work without installing or attempting to launch it.
                self.launch_desktop = await asyncio.to_thread(is_desktop_installed)
        if self.launch_desktop and isinstance(self.desktop, WindowsDesktopLauncher):
            # Resolve the dependency before checking/rotating credentials, writing
            # a recovery snapshot, or stopping any application.
            await asyncio.to_thread(self.desktop.require_available)
        source_profile: Profile | None = None
        if self._verify_account is None:
            from codex_account_manager.accounts.service import AccountService

            service = AccountService()
            active_id = await service._active_account_id_safe()
            for profile in await service.list_profiles():
                if active_id and profile.bound_account_id == active_id:
                    source_profile = profile
                    self.credentials.sync_active_to_profile(
                        profile.codex_home, expected_account_id=profile.bound_account_id
                    )
                    break
            # A valid access token can conceal a revoked refresh session.
            account_id = await self._verify(
                self.credentials.shared_home
                if active_id == target.bound_account_id
                else Path(target.codex_home),
                force_refresh=bool(active_id and active_id != target.bound_account_id)
                or not self.credentials.active_auth_path.exists(),
            )
            if account_id != target.bound_account_id:
                raise AccountMismatchError("Profile credentials no longer match its bound account.")
        new_auth = self.credentials.read_profile(target.codex_home)
        if not new_auth:
            raise TransactionError("Profile credentials are empty.", stage="prepare")
        if before_desktop_stop is not None:
            await before_desktop_stop()
        journal = _Journal(profile_id=target.id, profile_alias=target.alias)
        current_stage = TransactionStage.PREPARE
        snapshot_written = False

        def stage(value: TransactionStage) -> None:
            nonlocal current_stage
            current_stage = value
            journal.record(value)
            log.info("Account switch stage: %s (pid=%d).", value.value, os.getpid())
            bus.publish("switch.stage", stage=value.value, ok=True, alias=target.alias)

        try:
            stage(TransactionStage.PREPARE)
            stage(TransactionStage.BACKUP_ACTIVE_AUTH)
            self._snapshot()
            snapshot_written = True
            if self.launch_desktop or self.desktop.is_running():
                stage(TransactionStage.STOP_DESKTOP)
                self.desktop.stop()
            if source_profile is not None:
                # Preserve any credential rotation during Desktop shutdown.
                source_id = await self._verify(self.credentials.shared_home)
                if source_id != source_profile.bound_account_id:
                    raise AccountMismatchError("The source account changed during handoff.")
                self.credentials.sync_active_to_profile(
                    source_profile.codex_home, expected_account_id=source_profile.bound_account_id
                )
                if source_profile.id == target.id:
                    new_auth = self.credentials.read_profile(target.codex_home)
            self._arm_recovery()
            self.credentials.ensure_file_auth_config()
            stage(TransactionStage.ATOMIC_REPLACE)
            self.credentials.write_active_atomic(new_auth)
            stage(TransactionStage.VERIFY_ACCOUNT)
            account_id = await self._verify(self.credentials.shared_home)
            if account_id != target.bound_account_id:
                raise AccountMismatchError(
                    "Activated auth does not match the profile's bound account."
                )
            self.credentials.sync_active_to_profile(target.codex_home)
            if self.launch_desktop:
                stage(TransactionStage.START_DESKTOP)
                self.desktop.launch()
                stage(TransactionStage.WAIT_READY)
                if not await self.desktop.wait_ready(self.wait_ready_timeout):
                    raise TransactionError("Desktop readiness timed out.", stage="wait_ready")
            if after_switch is not None:
                stage(TransactionStage.RESUME_THREAD)
                await after_switch()
            stage(TransactionStage.COMMIT)
            # Deleting the snapshot is the commit point; no fallible work follows.
            self.recovery_path.unlink()
            snapshot_written = False
            journal.clear()
            return SwitchResult(True, TransactionStage.COMMIT, account_id=account_id)
        except (Exception, asyncio.CancelledError) as exc:
            detail = redact_text(str(exc)) or "Account switch cancelled."
            rolled_back = False
            if snapshot_written:
                try:
                    self._restore()
                    rolled_back = True
                except Exception:
                    log.exception("Rollback incomplete; recovery snapshot retained.")
            try:
                journal.record(TransactionStage.ROLLBACK, False, detail)
            except OSError:
                log.warning("Could not write rollback journal.")
            bus.publish(
                "switch.failed",
                alias=target.alias,
                stage=current_stage.value,
                rolled_back=rolled_back,
                detail=detail,
            )
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise TransactionError(
                detail, stage=current_stage.value, rolled_back=rolled_back
            ) from exc

    async def _verify(self, codex_home: Path, *, force_refresh: bool = False) -> str | None:
        if self._verify_account is not None:
            return await self._verify_account(str(codex_home))
        from codex_account_manager.accounts.service import AccountService

        snapshot = await AccountService().read_snapshot(codex_home, force_refresh=force_refresh)
        return snapshot.account_id


def _default_desktop() -> DesktopLauncher:
    import sys

    return WindowsDesktopLauncher() if sys.platform == "win32" else _NullDesktop()


class _NullDesktop:
    def stop(self) -> None: ...
    def launch(self) -> None: ...
    def is_running(self) -> bool:
        return False

    async def wait_ready(self, timeout: float = 30.0) -> bool:
        return True
