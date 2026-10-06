"""Conversation tracking, account handoffs, and post-switch continuation."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime

from codex_account_manager.accounts.service import AccountService
from codex_account_manager.adapters.app_server import CodexAppServer
from codex_account_manager.adapters.interfaces import GoalInfo
from codex_account_manager.adapters.native_desktop import verified_desktop_turn
from codex_account_manager.auth.transaction import AuthTransaction, SwitchResult
from codex_account_manager.continuity.automation import (
    ContinuationSupervisor,
    ContinuationTicket,
    has_verified_limit_turn,
)
from codex_account_manager.continuity.tracking import WorkTracker, goal_signature
from codex_account_manager.core.errors import (
    AppServerError,
    HandoffDeferredError,
    LocalResponseTooLargeError,
)
from codex_account_manager.core.events import bus
from codex_account_manager.core.logging import get_logger
from codex_account_manager.core.paths import paths
from codex_account_manager.core.redaction import redact_text
from codex_account_manager.domain.models import HandoffRecord, ThreadRecord
from codex_account_manager.domain.states import GoalState, HandoffReason
from codex_account_manager.goals.service import GoalService
from codex_account_manager.storage.repositories import (
    EventRepository,
    HandoffRepository,
    ProfileRepository,
    SettingsRepository,
    ThreadRepository,
)

log = get_logger(__name__)

OBSERVATION_TIMEOUT = 30.0
THREAD_CHECK_TIMEOUT = 2.0
THREAD_SCAN_TIMEOUT = 10.0
THREAD_CHECK_CONCURRENCY = 4


class ContinuityService:
    def __init__(
        self,
        *,
        accounts: AccountService | None = None,
        goals: GoalService | None = None,
        threads: ThreadRepository | None = None,
        handoffs: HandoffRepository | None = None,
        events: EventRepository | None = None,
        profiles: ProfileRepository | None = None,
    ):
        self.accounts = accounts or AccountService()
        self.goals = goals or GoalService()
        self.threads = threads or ThreadRepository()
        self.handoffs = handoffs or HandoffRepository()
        self.events = events or EventRepository()
        self.profiles = profiles or ProfileRepository()
        self.automation = ContinuationSupervisor()
        self.tracker = WorkTracker()
        self._work_running = False
        self._work_verified = False
        self._restart_independent: set[str] = set()
        self._unavailable_independent: set[str] = set()
        self._restart_blockers: set[str] = set()
        self._turn_baselines: dict[str, tuple[str, str, str, int]] = {}

    async def sync_threads(self) -> list[ThreadRecord]:
        """Read threads from the App Server and persist lightweight records."""
        adapter = CodexAppServer(paths.shared_codex_home)
        try:
            await adapter.start()
            infos = await adapter.list_threads()
        finally:
            await adapter.aclose()

        def timestamp(value: int | None) -> datetime:
            try:
                return datetime.fromtimestamp(value or 0, UTC)
            except (ValueError, OverflowError, OSError):
                return datetime.fromtimestamp(0, UTC)

        records: list[ThreadRecord] = []
        for info in infos:
            if not info.id:
                continue

            record = ThreadRecord(
                id=info.id,
                cwd=info.cwd,
                workspace=info.cwd,
                preview=info.preview,
                title=info.title,
                source=info.source,
                project_id=info.project_id,
                model_provider=info.model_provider,
                created_at=timestamp(info.created_at),
                updated_at=timestamp(info.recency_at or info.updated_at or info.created_at),
            )
            records.append(record)
        await self.threads.replace_listed(records)
        await self.tracker.reconcile({record.id for record in records})
        bus.publish("work.observed")
        return sorted(records, key=lambda record: record.updated_at, reverse=True)

    async def read_native_goal(self, thread_id: str) -> GoalInfo | None:
        adapter = CodexAppServer(paths.shared_codex_home)
        try:
            await adapter.start()
            return await adapter.get_goal(thread_id)
        finally:
            await adapter.aclose()

    async def observe_work(self) -> str | None:
        """Refresh running and interrupted work before evaluating account failover."""
        self._work_running = False
        self._work_verified = False
        self._restart_independent.clear()
        self._unavailable_independent.clear()
        self._restart_blockers.clear()
        bus.publish("work.observation_status", state="checking")
        try:
            async with asyncio.timeout(OBSERVATION_TIMEOUT):
                return await self._observe_work()
        except TimeoutError:
            await self.tracker.unverified()
            bus.publish("work.observation_status", state="timed_out")
            raise
        except Exception:
            await self.tracker.unverified()
            bus.publish("work.observation_status", state="failed")
            raise

    async def _observe_work(self) -> str | None:
        generation = self.tracker.generation
        adapter = CodexAppServer(paths.shared_codex_home, experimental=True)
        try:
            await adapter.start()
            account_id = (await adapter.read_account()).account_id
            if not account_id:
                await self.tracker.unverified()
                bus.publish("work.observation_status", state="signed_out")
                return None
            infos = await adapter.list_threads(include_subagents=False)
            listed = {info.id for info in infos}
            self._turn_baselines = {
                key: value for key, value in self._turn_baselines.items() if key in listed
            }
            await self.tracker.reconcile({info.id for info in infos})
            candidates = sorted(
                (i for i in infos if i.id and not (i.source or "").startswith("subAgent")),
                key=lambda i: i.recency_at or i.updated_at or 0,
                reverse=True,
            )
            # Recheck work tracked before the account switch too.
            tracked = await self.tracker.ids()
            selected = list(dict.fromkeys([*tracked, *(i.id for i in candidates[:20])]))
            sources = {i.id: i.source for i in candidates}
            for thread_id in tracked:
                saved_thread = await self.threads.get(thread_id)
                if saved_thread and thread_id not in sources:
                    sources[thread_id] = saved_thread.source
            checked = 0
            unavailable = 0
            unsupported = 0
            completed: set[str] = set()
            semaphore = asyncio.Semaphore(THREAD_CHECK_CONCURRENCY)
            await self.tracker.unverified(selected)

            async def inspect(thread_id: str) -> None:
                nonlocal checked, unavailable, unsupported
                async with semaphore:
                    try:
                        async with asyncio.timeout(THREAD_CHECK_TIMEOUT):
                            source = sources.get(thread_id)
                            if source is None:
                                source = await adapter.thread_source(thread_id)
                            if source not in {"cli", "exec", "appServer", "vscode"}:
                                unsupported += 1
                                completed.add(thread_id)
                                return
                            turn: dict | None
                            if source == "vscode":
                                if not await adapter.is_desktop_thread(thread_id):
                                    self._restart_independent.add(thread_id)
                                    if not await self.automation.ide_enabled():
                                        unsupported += 1
                                        completed.add(thread_id)
                                        return
                                    turn, _owner = await self.automation.owner_turn(
                                        adapter, thread_id
                                    )
                                else:
                                    turn = await verified_desktop_turn(
                                        self.automation.native, adapter, thread_id
                                    )
                            else:
                                self._restart_independent.add(thread_id)
                                turn = await adapter.observed_turn(thread_id)
                            if (
                                thread_id not in self._restart_independent
                                and turn
                                and turn.get("status") == "inProgress"
                            ):
                                self._work_running = True
                                self._restart_blockers.add(thread_id)
                            goal = await adapter.get_goal(thread_id) if turn else None
                            if turn and goal is None:
                                raise AppServerError("Observed goal could not be verified.")
                            new_turn = False
                            if turn and goal is not None:
                                previous = self._turn_baselines.get(thread_id)
                                current = (account_id, turn["id"], goal_signature(goal), generation)
                                new_turn = bool(
                                    previous
                                    and previous[0] == current[0]
                                    and previous[1] != current[1]
                                    and previous[2:] == current[2:]
                                )
                                self._turn_baselines[thread_id] = current
                            await self.tracker.observe(
                                account_id,
                                thread_id,
                                turn,
                                goal,
                                limited=has_verified_limit_turn(turn, goal),
                                new_turn=new_turn,
                                generation=generation,
                            )
                            checked += 1
                            completed.add(thread_id)
                            bus.publish("work.observed")
                    except Exception as exc:
                        unavailable += 1
                        log.warning(
                            "Could not verify an observed conversation (%s).", type(exc).__name__
                        )

            try:
                async with asyncio.timeout(THREAD_SCAN_TIMEOUT):
                    async with asyncio.TaskGroup() as group:
                        for thread_id in selected:
                            group.create_task(inspect(thread_id))
            except TimeoutError:
                unavailable = len(selected) - checked - unsupported
            incomplete = set(selected) - completed
            self._unavailable_independent = incomplete & self._restart_independent
            self._work_verified = not (incomplete - self._restart_independent)
            self._restart_blockers.update(incomplete - self._restart_independent)
            bus.publish(
                "work.observation_status",
                state="partial" if unavailable else "ready",
                checked=checked,
                unavailable=unavailable,
                unsupported=unsupported,
                checked_at=datetime.now().strftime("%H:%M:%S"),
            )
            return account_id
        finally:
            await adapter.aclose()

    async def continuation_support(self, thread_id: str) -> str:
        """Check source and native read availability without loading or sending input."""
        adapter = CodexAppServer(paths.shared_codex_home, experimental=True)
        try:
            async with asyncio.timeout(12):
                await adapter.start()
                source = await adapter.thread_source(thread_id)
                if source in {"cli", "exec", "appServer"}:
                    return "cli"
                if source != "vscode":
                    return "unsupported"
                if await self.automation.uses_ide(adapter, thread_id):
                    await self.automation.ide.inspect(thread_id)
                    return "ide"
                await self.automation.native.latest_turn(thread_id)
                return "desktop"
        except LocalResponseTooLargeError:
            return "response_too_large"
        except (AppServerError, TimeoutError, OSError):
            return "unverified"
        finally:
            await adapter.aclose()

    async def queue_verified_continuation(self, thread_id: str) -> None:
        from codex_account_manager.continuity.policy import SwitchPolicy
        from codex_account_manager.core.operation_lock import OperationLock
        from codex_account_manager.storage.database import connect

        if await SettingsRepository().get("monitor_enabled", "true") != "true":
            raise AppServerError("Enable monitoring before requesting continuation.")
        with OperationLock(paths.data_dir / "account-operation.lock"):
            snapshot = await self.accounts.read_snapshot(paths.shared_codex_home)
            profile = next(
                (
                    p
                    for p in await self.profiles.list()
                    if p.bound_account_id == snapshot.account_id
                ),
                None,
            )
            if profile is None or not profile.bound_account_id:
                raise AppServerError("The active account identity could not be verified.")
            active = await self.accounts._health_for(
                profile, active_account_id=snapshot.account_id, active_snapshot=snapshot
            )
            if not SwitchPolicy._is_available(active):
                raise AppServerError("The active account must have verified available capacity.")
            ticket = await self.automation.prepare(thread_id)
            if ticket is None:
                raise AppServerError(
                    "No eligible usage-limit interruption was verified. Nothing was sent."
                )
            async with connect() as db:
                attempt = await (
                    await db.execute(
                        "SELECT 1 FROM continuation_attempts WHERE thread_id=? AND source_turn_id=?",
                        (ticket.thread_id, ticket.turn_id),
                    )
                ).fetchone()
            if attempt:
                raise AppServerError(
                    "This continuation was already attempted. Inspect the conversation before retrying."
                )
            await self.automation.save_pending(
                [replace(ticket, account_id=profile.bound_account_id)]
            )
        bus.publish("monitor.refresh_requested")

    # Account switch commits before conversation loading and goal reconciliation.
    async def handoff(
        self,
        target_alias: str,
        *,
        reason: HandoffReason = HandoffReason.MANUAL,
        thread_id: str | None = None,
        transaction: AuthTransaction | None = None,
        before_desktop_stop: Callable[[], Awaitable[None]] | None = None,
    ) -> SwitchResult:
        target = await self.profiles.get_by_alias(target_alias)
        if not target:
            from codex_account_manager.core.errors import ProfileNotFoundError

            raise ProfileNotFoundError(f"Profile not found: {target_alias}")

        from_alias = await self.accounts.resolve_active_alias()
        from_profile = await self.profiles.get_by_alias(from_alias) if from_alias else None

        handoff = HandoffRecord(
            thread_id=thread_id,
            from_profile_id=from_profile.id if from_profile else None,
            to_profile_id=target.id,
            reason=reason,
        )
        await self.handoffs.save(handoff)
        await self.events.append(
            "handoff.started",
            thread_id=thread_id,
            profile_id=target.id,
            payload={"reason": reason.value, "from": from_alias, "to": target_alias},
        )

        if thread_id:
            await self.goals.mark_status(thread_id, GoalState.SWITCHING)

        # Account activation refreshes an installed Desktop independently of
        # whether its automatic continuation is enabled. Surface preferences
        # control subsequent work, not credential verification or recovery.
        tx = transaction or AuthTransaction()
        try:
            result = await tx.switch(target, before_desktop_stop=before_desktop_stop)
        except (Exception, asyncio.CancelledError) as exc:
            handoff.finished_at = datetime.now(UTC)
            handoff.success = False
            handoff.rolled_back = getattr(exc, "rolled_back", False)
            handoff.final_stage = getattr(exc, "stage", None)
            handoff.detail = redact_text(str(exc))
            await self.handoffs.save(handoff)
            await self.events.append(
                "handoff.failed", thread_id=thread_id, payload={"detail": redact_text(str(exc))}
            )
            if thread_id:
                await self.goals.mark_status(thread_id, GoalState.NEEDS_USER)
            raise

        handoff.finished_at = datetime.now(UTC)
        handoff.success = True
        handoff.final_stage = result.final_stage.value
        try:
            await self.handoffs.save(handoff)
            await self.events.append("handoff.completed", thread_id=thread_id, profile_id=target.id)
        except Exception:
            log.warning("Verified account switch committed but its history could not be saved.")
        if thread_id:
            try:
                await self.resume_conversation(thread_id)
            except Exception as exc:
                detail = redact_text(str(exc))
                result.detail = detail
                handoff.detail = detail
                try:
                    await self.handoffs.save(handoff)
                    await self.events.append(
                        "thread.resume_failed", thread_id=thread_id, payload={"detail": detail}
                    )
                    await self.goals.mark_status(thread_id, GoalState.NEEDS_USER)
                except Exception:
                    log.warning("Could not record the conversation load failure.")
                bus.publish("continuation.status", state="needs_user")
        return result

    async def _require_restart_safe(self, expected_account_id: str | None = None) -> str | None:
        try:
            account_id = await self.observe_work()
        except Exception as exc:
            raise HandoffDeferredError(
                "Account switch is waiting: conversation state could not be verified. Codex remains open."
            ) from exc
        for thread_id in self._restart_blockers:
            await self.automation.report(thread_id, "waiting_shared", "desktop_restart")
        if not self._work_verified:
            raise HandoffDeferredError(
                "Account switch is waiting: conversation state could not be verified. Codex remains open."
            )
        if self._work_running:
            raise HandoffDeferredError(
                "Account switch is waiting for running conversations to finish or report their limit. Codex remains open."
            )
        if expected_account_id is not None and account_id != expected_account_id:
            raise HandoffDeferredError(
                "Account switch is waiting: the active account changed. Codex remains open."
            )
        return account_id

    async def continue_on_limit(
        self, target_alias: str, *, transaction: AuthTransaction | None = None
    ) -> SwitchResult:
        """Commit the account handoff before starting any verified interrupted work."""
        if self.automation.tasks:
            raise AppServerError("An automatic continuation is still running.")
        generation = self.tracker.generation
        account_id = await self._require_restart_safe()

        async def before_desktop_stop() -> None:
            await self._require_restart_safe(account_id)
            if (
                await SettingsRepository().get("monitor_enabled", "true") != "true"
                or self.tracker.generation != generation
            ):
                raise HandoffDeferredError("Account switch stopped because monitoring changed.")

        saved = await self.tracker.limited(account_id) if account_id else []
        tickets = []
        preparation_failed = bool(self._unavailable_independent)
        restart_blocked = False
        for thread_id in self._unavailable_independent:
            await self.automation.report(thread_id, "needs_user", "verification")
        desktop_threads: list[str] = []
        desktop_tickets: list[ContinuationTicket] = []
        for thread_id, turn_id, signature in saved:
            try:
                async with asyncio.timeout(12):
                    ticket = await self.automation.prepare(thread_id)
                if ticket and ticket.turn_id == turn_id and ticket.goal_signature == signature:
                    if ticket.desktop:
                        desktop_threads.append(thread_id)
                        desktop_tickets.append(ticket)
                    else:
                        tickets.append(ticket)
                elif ticket:
                    await self.automation.report(thread_id, "needs_user", "verification")
                    preparation_failed = True
                    restart_blocked |= thread_id not in self._restart_independent
            except Exception:
                preparation_failed = True
                restart_blocked |= thread_id not in self._restart_independent
                await self.automation.report(thread_id, "needs_user", "preparation")
        if restart_blocked:
            raise HandoffDeferredError(
                "Account switch is waiting: continuation could not be prepared. Codex remains open."
            )
        target = await self.profiles.get_by_alias(target_alias)
        if not target or not target.bound_account_id:
            raise AppServerError("The target account identity could not be verified.")
        pending = [
            replace(t, account_id=target.bound_account_id) for t in desktop_tickets + tickets
        ]
        await self.automation.save_pending(pending)
        try:
            result = await self.handoff(
                target_alias,
                reason=HandoffReason.USAGE_LIMITED,
                transaction=transaction,
                before_desktop_stop=before_desktop_stop,
            )
        except BaseException:
            await self.automation.discard_pending(pending)
            raise
        if account_id:
            try:
                await self.tracker.clear(account_id, [ticket.thread_id for ticket in tickets])
            except Exception:
                log.warning("Account switch committed; observed work could not be cleared.")
        ready_desktop: list[ContinuationTicket] = []
        if desktop_threads and account_id:
            try:
                target = await self.profiles.get_by_alias(target_alias)
                if not target or not target.bound_account_id:
                    raise AppServerError("The target account identity could not be verified.")
                await self.tracker.await_desktop(
                    account_id, target.bound_account_id, desktop_threads
                )
                bus.publish("work.observed")
                ready_desktop = desktop_tickets
            except Exception:
                log.warning("Account switch committed; Desktop follow-up could not be saved.")
                bus.publish("continuation.status", state="needs_user", stage="journal")
        tickets = ready_desktop + tickets
        await self.automation.discard_pending(
            [t for t in pending if t.thread_id not in {item.thread_id for item in tickets}]
        )
        if tickets:
            try:
                target = await self.profiles.get_by_alias(target_alias)
                if not target or not target.bound_account_id:
                    raise AppServerError("The target account identity could not be verified.")
                self.automation.launch_batch(
                    [
                        ContinuationTicket(
                            ticket.thread_id,
                            ticket.turn_id,
                            target.bound_account_id,
                            ticket.goal_signature,
                            desktop=ticket.desktop,
                            owner_id=ticket.owner_id,
                        )
                        for ticket in tickets
                    ]
                )
            except Exception:
                log.warning("Account switch committed; automatic continuation could not start.")
                bus.publish("continuation.status", state="needs_user", stage="connection")
        elif not desktop_threads and await self.automation.enabled():
            bus.publish(
                "continuation.status", state="needs_user" if preparation_failed else "skipped"
            )
        return result

    async def resume_conversation(self, thread_id: str) -> None:
        await self.goals.mark_status(thread_id, GoalState.RESUMING)
        adapter = CodexAppServer(paths.shared_codex_home)
        try:
            await adapter.start()
            await adapter.resume_thread(thread_id)
            await self.events.append("thread.resumed", thread_id=thread_id)
            result = await self.goals.reconcile_after_resume(thread_id, adapter)
            if result.action == "blocked":
                raise AppServerError(result.detail)
            await self.events.append(
                "goal.reconciled", thread_id=thread_id, payload={"action": result.action}
            )
        finally:
            await adapter.aclose()

    async def recent_handoffs(self, limit: int = 20) -> list[HandoffRecord]:
        return await self.handoffs.recent(limit)
