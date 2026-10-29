"""Monitor account health and apply the selected switching policy.

Runs in the desktop process and publishes health snapshots to the UI.
"""

from __future__ import annotations

import asyncio
import time

from codex_account_manager.accounts.service import AccountService
from codex_account_manager.continuity.policy import (
    SwitchPolicy,
    automatic_exclusion_reason,
    resolve_policy,
)
from codex_account_manager.continuity.service import ContinuityService
from codex_account_manager.core.errors import HandoffDeferredError, OperationBusyError
from codex_account_manager.core.events import bus
from codex_account_manager.core.logging import get_logger
from codex_account_manager.domain.models import ProfileHealth
from codex_account_manager.domain.states import HandoffReason, SwitchPolicyKind
from codex_account_manager.monitoring.settings import (
    DEFAULT_POLICY,
    poll_interval,
)

log = get_logger(__name__)


class Watcher:
    def __init__(
        self,
        *,
        accounts: AccountService | None = None,
        continuity: ContinuityService | None = None,
        policy: SwitchPolicy | None = None,
        poll_seconds: int | None = None,
    ):
        self.accounts = accounts or AccountService()
        self.continuity = continuity or ContinuityService(accounts=self.accounts)
        self.policy = policy or resolve_policy(DEFAULT_POLICY)
        self._load_policy = policy is None
        self.poll_seconds = poll_interval(poll_seconds)
        self._load_interval = poll_seconds is None
        self._wake = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop = asyncio.Event()
        self._failed_switches: dict[tuple[str | None, str], tuple[float, float]] = {}
        self._manual_refresh = False
        self._last_owner_key: str | None = None

    def stop(self) -> None:
        def signal() -> None:
            self._stop.set()
            self._wake.set()

        if self._loop:
            self._loop.call_soon_threadsafe(signal)
        else:
            signal()

    async def poll_once(
        self, *, allow_switch: bool = True, track_work: bool = True
    ) -> list[ProfileHealth]:
        """One structured poll. Returns health for all profiles."""
        if self._load_policy:
            from codex_account_manager.storage.repositories import SettingsRepository

            values = await SettingsRepository().all()
            value = values.get("switch_policy", DEFAULT_POLICY)
            if self._load_interval:
                self.poll_seconds = poll_interval(values.get("poll_seconds"))
            try:
                self.policy = resolve_policy(value or DEFAULT_POLICY)
            except ValueError:
                self.policy = resolve_policy(SwitchPolicyKind.MANUAL)
        health = await self.accounts.all_health()
        bus.publish("health.updated", count=len(health), health=health)
        owner_key = None
        observed = False
        if track_work:
            try:
                owner_key = await self.continuity.observe_work()
                observed = True
                bus.publish("work.observed")
            except Exception:
                log.warning("Conversation tracking could not be refreshed.")
        current = next((h for h in health if h.is_active), None)
        health_owner = current.identity.fingerprint if current and current.identity else None
        account_changed = bool(owner_key and health_owner and owner_key != health_owner) or (
            self._last_owner_key is not None
            and owner_key is not None
            and owner_key != self._last_owner_key
        )
        if owner_key is not None:
            self._last_owner_key = owner_key
        if allow_switch and not account_changed:
            if observed and current is not None and SwitchPolicy._is_available(current):
                try:
                    await self.continuity.automation.recover_pending(owner_key)
                except Exception:
                    log.warning("Prepared continuation could not be recovered.")
                    bus.publish("continuation.status", state="needs_user", stage="recovery")
            await self._maybe_failover(health)
        return health

    async def _maybe_failover(self, health: list[ProfileHealth]) -> None:
        if self._loop is not None:
            from codex_account_manager.storage.repositories import SettingsRepository

            if await SettingsRepository().get("monitor_enabled", "true") != "true":
                return
        automation = getattr(self.continuity, "automation", None)
        if automation is not None and automation.tasks:
            return
        current = next((h for h in health if h.is_active), None)
        source = current.profile_id if current else None
        targets = {h.profile_id for h in health}
        self._failed_switches = {
            key: retry
            for key, retry in self._failed_switches.items()
            if key[0] == source and key[1] in targets
        }
        if not SwitchPolicy._current_limited(current):
            self._failed_switches.clear()
        now = time.monotonic()
        candidates = [
            h
            for h in health
            if now >= self._failed_switches.get((source, h.profile_id), (0.0, 300.0))[0]
        ]
        decision = self.policy.decide(current, candidates)
        if not decision.should_switch or decision.target is None:
            return
        if decision.requires_confirmation:
            bus.publish(
                "switch.suggested",
                target=decision.target.alias,
                reason=decision.reason,
            )
            log.info("Switch suggested: %s", decision.reason)
            return
        health_reader = getattr(self.accounts, "health", None)
        while health_reader is not None:
            try:
                target_health = await health_reader(decision.target.alias)
            except Exception:
                target_health = None
            if (
                target_health is not None
                and target_health.profile_id == decision.target.profile_id
                and SwitchPolicy._is_available(target_health)
                and target_health.ordinary_usage_allowed is True
                and automatic_exclusion_reason(target_health) is None
            ):
                break
            candidates = [c for c in candidates if c.profile_id != decision.target.profile_id]
            decision = self.policy.decide(current, candidates)
            if not decision.should_switch or decision.target is None:
                bus.publish(
                    "switch.deferred",
                    detail="No target account passed the fresh availability check.",
                )
                return
        if decision.target is None:
            return
        key = (source, decision.target.profile_id)
        log.info("Policy failover: %s", decision.reason)
        try:
            if current is not None and current.reauth_required:
                await self.continuity.continue_on_limit(
                    decision.target.alias,
                    reason=HandoffReason.AUTH_EXPIRED,
                )
            else:
                await self.continuity.continue_on_limit(decision.target.alias)
            self._failed_switches.clear()
            bus.publish("switch.completed", target=decision.target.alias)
        except (HandoffDeferredError, OperationBusyError) as exc:
            log.info("Automatic handoff deferred: %s", exc)
            bus.publish("switch.deferred", detail=str(exc))
        except Exception as exc:
            delay = self._failed_switches.get(key, (0.0, 300.0))[1]
            self._failed_switches[key] = (time.monotonic() + delay, min(delay * 2, 1800.0))
            log.warning("Automatic failover failed: %s", exc)
            from codex_account_manager.core.redaction import redact_text

            bus.publish("switch.failed", detail=redact_text(str(exc)))

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()

        def settings_changed(_event) -> None:
            if self._loop and not self._loop.is_closed():
                self._loop.call_soon_threadsafe(self._wake.set)

        def refresh_requested(event) -> None:
            self._manual_refresh = True
            settings_changed(event)

        unsubscribe = bus.subscribe("monitor.settings_changed", settings_changed)
        unsubscribe_refresh = bus.subscribe("monitor.refresh_requested", refresh_requested)
        try:
            while not self._stop.is_set():
                self._wake.clear()
                started = time.monotonic()
                retry_delay = None
                enabled = False
                try:
                    from codex_account_manager.storage.repositories import SettingsRepository

                    enabled = await SettingsRepository().get("monitor_enabled", "true") == "true"
                    bus.publish("monitor.status", enabled=enabled)
                    requested = self._manual_refresh
                    self._manual_refresh = False
                    if not enabled:
                        self.continuity._turn_baselines.clear()
                        await self.continuity.automation.stop(clear_pending=True)
                    await self.poll_once(allow_switch=enabled, track_work=enabled or requested)
                except OperationBusyError:
                    retry_delay = 2.0
                    log.debug("Account refresh deferred until the current operation completes.")
                except Exception:
                    log.exception("Watcher poll failed.")
                try:
                    await asyncio.wait_for(
                        self._wake.wait(),
                        timeout=(
                            retry_delay
                            if retry_delay is not None
                            else max(1.0, self.poll_seconds - (time.monotonic() - started))
                        ),
                    )
                except TimeoutError:
                    pass
        finally:
            unsubscribe()
            unsubscribe_refresh()
            automation = getattr(self.continuity, "automation", None)
            if automation is not None:
                await automation.stop()
            self._loop = None
