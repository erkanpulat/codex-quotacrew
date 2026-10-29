"""Fake adapters for testing without a real Codex install."""

from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import replace

from codex_account_manager.adapters.interfaces import CapabilitySet, GoalInfo, ServerInfo
from codex_account_manager.domain.identity import AccountIdentity, IdentityState
from codex_account_manager.domain.models import AccountSnapshot, ThreadInfo


def account_identity(workspace="acc-1", user="test-user"):
    return AccountIdentity(user_id=user, workspace_id=workspace, state=IdentityState.COMPLETE)


def account_auth(workspace="acc-1", user="test-user", generation=1):
    claims = {
        "https://api.openai.com/auth": {"chatgpt_user_id": user, "chatgpt_account_id": workspace}
    }
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    token = f"header.{payload}.signature"
    return json.dumps(
        {
            "auth_mode": "chatgpt",
            "tokens": {"account_id": workspace, "id_token": token, "access_token": token},
            "generation": generation,
        }
    ).encode()


_DEFAULT_IDENTITY = object()


class FakeAppServer:
    """A configurable in-memory App Server adapter.

    - ``account_id`` controls what ``read_account`` reports.
    - ``goals`` maps thread_id -> objective (None means no native goal).
    - ``fail_on`` can raise for a given method name to simulate errors.
    """

    def __init__(
        self,
        account_id: str = "acc-1",
        *,
        plan: str = "plus",
        ordinary_usage_allowed: bool = True,
        primary: float = 0.0,
        secondary: float = 10.0,
        threads: list[ThreadInfo] | None = None,
        turns: dict[str, dict] | None = None,
        goals: dict[str, str | None] | None = None,
        fail_on: set[str] | None = None,
        identity=_DEFAULT_IDENTITY,
    ):
        self.account_id = account_id
        self._identity = identity
        self.plan = plan
        self.ordinary_usage_allowed = ordinary_usage_allowed
        self.primary = primary
        self.secondary = secondary
        self._threads = threads or []
        self._turns = turns or {}
        self._goals = goals or {}
        self._fail_on = fail_on or set()
        self.started = False
        self.closed = False
        self.set_goal_calls: list[tuple[str, str]] = []
        self.resume_calls: list[str] = []

    @property
    def identity(self):
        if self._identity is _DEFAULT_IDENTITY:
            return account_identity(self.account_id)
        return self._identity

    @identity.setter
    def identity(self, value):
        self._identity = value

    async def start(self) -> ServerInfo:
        if "start" in self._fail_on:
            raise RuntimeError("start failed")
        self.started = True
        return ServerInfo(user_agent="fake/1", codex_home="fake", platform_os="windows")

    async def capabilities(self) -> CapabilitySet:
        return CapabilitySet(
            methods=frozenset({"account/read", "thread/list", "thread/goal/get", "thread/goal/set"})
        )

    async def read_account(self, *, include_identity=False) -> AccountSnapshot:
        if "read_account" in self._fail_on:
            raise RuntimeError("read_account failed")
        return AccountSnapshot(
            account_id=self.account_id,
            account_type="chatgpt",
            email="user@example.test",
            plan_type=self.plan,
            ordinary_usage_allowed=self.ordinary_usage_allowed,
            primary_used_percent=self.primary,
            primary_resets_at=1_800_000_000,
            primary_window_minutes=300,
            secondary_used_percent=self.secondary,
            secondary_resets_at=1_800_600_000,
            secondary_window_minutes=10080,
            identity=self.identity,
        )

    async def list_threads(
        self,
        cursor: str | None = None,
        *,
        max_items: int | None = None,
        include_subagents: bool = True,
    ) -> list[ThreadInfo]:
        threads = list(self._threads)
        if not include_subagents:
            threads = [t for t in threads if not (t.source or "").startswith("subAgent")]
        return threads[:max_items] if max_items is not None else threads

    async def latest_turn(self, thread_id: str) -> dict | None:
        return self._turns.get(thread_id)

    async def observed_turn(self, thread_id: str) -> dict | None:
        return await self.latest_turn(thread_id)

    async def verified_stored_turn(self, thread_id: str) -> dict | None:
        from codex_account_manager.core.errors import AppServerError

        raise AppServerError("No lifecycle fixture supplied.")

    async def require_bounded_desktop_history(self, thread_id: str) -> None:
        pass

    async def is_desktop_thread(self, thread_id: str) -> bool:
        return False

    async def thread_source(self, thread_id: str) -> str:
        return next((t.source or "cli" for t in self._threads if t.id == thread_id), "cli")

    async def require_headless_compatible(self, thread_id: str) -> None:
        pass

    async def resume_thread(self, thread_id: str) -> dict:
        self.resume_calls.append(thread_id)
        return {"threadId": thread_id}

    async def get_goal(self, thread_id: str) -> GoalInfo | None:
        objective = self._goals.get(thread_id)
        present = objective is not None
        return GoalInfo(thread_id=thread_id, objective=objective, status=None, present=present)

    async def set_goal(self, thread_id: str, objective: str) -> GoalInfo:
        self.set_goal_calls.append((thread_id, objective))
        self._goals[thread_id] = objective
        return GoalInfo(thread_id=thread_id, objective=objective, status="active", present=True)

    async def aclose(self) -> None:
        self.closed = True


class FakeDesktop:
    def __init__(self, *, ready: bool = True):
        self.ready = ready
        self.stopped = 0
        self.launched = 0

    def stop(self) -> None:
        self.stopped += 1

    def launch(self) -> None:
        self.launched += 1

    def is_running(self) -> bool:
        return True

    async def wait_ready(self, timeout: float = 30.0) -> bool:
        return self.ready


class ExecutionServer(FakeAppServer):
    def __init__(self):
        super().__init__()
        self.latest = {
            "id": "limited",
            "status": "failed",
            "error": {"codexErrorInfo": "usageLimitExceeded"},
        }
        self.native = GoalInfo("thread", None, None, False)
        self.after_turn = []
        self.turn_calls = []
        self.reactivations = []
        self.fail_send = False
        self.wait = False
        self.started_turn = asyncio.Event()

    async def latest_turn(self, thread_id):
        return self.latest

    async def get_goal(self, thread_id):
        return self.native

    async def resume_for_continuation(self, thread_id):
        self.resume_calls.append(thread_id)

    async def reactivate_usage_limited_goal(self, thread_id, previous):
        self.reactivations.append(previous)
        self.native = replace(previous, status="active")

    async def run_continuation_turn(self, thread_id, message_id, *, on_started=None):
        self.turn_calls.append((thread_id, message_id))
        if self.fail_send:
            raise RuntimeError("ambiguous delivery")
        if on_started:
            await on_started(str(len(self.turn_calls)))
        self.started_turn.set()
        if self.wait:
            await asyncio.Event().wait()
        self.latest = {"id": str(len(self.turn_calls)), "status": "completed"}
        if self.after_turn:
            self.native = self.after_turn.pop(0)
        return self.latest
