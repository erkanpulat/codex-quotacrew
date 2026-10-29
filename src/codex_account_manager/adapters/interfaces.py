"""Adapter protocols (structural interfaces).

These are the seams that make the app resilient to Codex changes and testable
without a real Codex install. Concrete implementations live alongside; fakes
live under ``tests``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from codex_account_manager.domain.models import AccountSnapshot, ThreadInfo


@dataclass(frozen=True)
class ServerInfo:
    user_agent: str | None
    codex_home: str | None
    platform_os: str | None


@dataclass(frozen=True)
class CapabilitySet:
    """Which App Server methods are available on this Codex build."""

    methods: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class GoalInfo:
    thread_id: str
    objective: str | None
    status: str | None
    present: bool
    token_budget: int | None = None
    tokens_used: int = 0


@runtime_checkable
class AppServerAdapter(Protocol):
    async def start(self) -> ServerInfo: ...
    async def capabilities(self) -> CapabilitySet: ...
    async def read_account(self, *, include_identity: bool = False) -> AccountSnapshot: ...
    async def list_threads(
        self,
        cursor: str | None = None,
        *,
        max_items: int | None = None,
        include_subagents: bool = True,
    ) -> list[ThreadInfo]: ...
    async def resume_thread(self, thread_id: str) -> dict: ...
    async def get_goal(self, thread_id: str) -> GoalInfo | None: ...
    async def set_goal(self, thread_id: str, objective: str) -> GoalInfo: ...
    async def aclose(self) -> None: ...


@runtime_checkable
class DesktopLauncher(Protocol):
    def stop(self) -> None: ...
    def launch(self) -> None: ...
    def is_running(self) -> bool: ...
    async def wait_ready(self, timeout: float = 30.0) -> bool: ...


@runtime_checkable
class CredentialStore(Protocol):
    def profile_auth_path(self, codex_home: str | Path) -> Path: ...
    def read_active(self) -> bytes | None: ...
    def write_active_atomic(self, data: bytes) -> None: ...
    def restore_active(self, data: bytes | None) -> None: ...
