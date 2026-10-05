"""Opt-in continuation in the existing local Codex owner (IDE or Desktop).

Private IPC versions are pinned; unknown schema, owner changes and ambiguous
receipts fail closed. Conversations, credentials and received content are never
logged or persisted. No separate conversation writer or remote port is opened.
"""

from __future__ import annotations

import asyncio
import ctypes
import sys
import threading
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from codex_account_manager.adapters.native_desktop import (
    CONTINUE_PROMPT,
    IDENTIFIER,
    _trusted_handle,
)
from codex_account_manager.adapters.windows_pipe import FramedPipe
from codex_account_manager.core.errors import (
    AppServerError,
    ConnectionNotReadyError,
    LocalResponseTooLargeError,
    OwnerNotFoundError,
)
from codex_account_manager.platform.editors import installed_editors

MAX_FRAME = 16 * 1024 * 1024
MAX_RECEIVED = 32 * 1024 * 1024
MAX_MESSAGES = 64


@dataclass(frozen=True)
class OwnerSnapshot:
    owner_id: str
    turn: dict
    blocked: bool
    goal: dict | None = field(repr=False)


def snapshot(message: dict, thread_id: str, owner_id: str) -> OwnerSnapshot:
    params = message.get("params", {})
    change = params.get("change", {}) if isinstance(params, dict) else {}
    state = change.get("conversationState", {}) if isinstance(change, dict) else {}
    if (
        message.get("type") != "broadcast"
        or message.get("method") != "thread-stream-state-changed"
        or message.get("version") != 11
        or message.get("sourceClientId") != owner_id
        or not isinstance(params, dict)
        or params.get("hostId") != "local"
        or params.get("conversationId") != thread_id
        or change.get("type") != "snapshot"
        or not isinstance(state, dict)
        or state.get("id") != thread_id
    ):
        raise AppServerError("The IDE conversation owner could not be verified.")
    turns = state.get("turns")
    history = state.get("turnHistory")
    if isinstance(history, dict) and history.get("kind") == "canonical":
        data = history.get("history", {})
        islands = data.get("islands", [])
        last = islands[-1] if islands else {}
        if last.get("newerBoundary", {}).get("status") != "exhausted":
            raise AppServerError("The IDE snapshot is missing its latest history.")
        entries = last.get("entries", [])
        entities = data.get("entitiesByKey", {})
        turns = [entities.get(entries[-1].get("value"))] if entries else []
    if not isinstance(turns, list) or not turns or not isinstance(turns[-1], dict):
        raise AppServerError("The IDE conversation has no verifiable turn.")
    latest = turns[-1]
    turn_id, status = latest.get("turnId"), latest.get("status")
    if (
        not isinstance(turn_id, str)
        or not IDENTIFIER.fullmatch(turn_id)
        or status not in {"inProgress", "completed", "failed", "interrupted"}
    ):
        raise AppServerError("The IDE turn format is unsupported.")
    runtime = state.get("threadRuntimeStatus")
    runtime_type = runtime.get("type") if isinstance(runtime, dict) else None
    # Missing request/runtime fields are unknown, not evidence of an idle owner.
    blocked = (
        not isinstance(state.get("requests"), list)
        or bool(state["requests"])
        or bool(state.get("unconfirmedTurnSubmissions"))
        or runtime_type not in {"idle", "systemError"}
        or (runtime_type == "systemError" and status != "failed")
    )
    goal = state.get("threadGoal")
    if goal is not None and not isinstance(goal, dict):
        raise AppServerError("The IDE goal format is unsupported.")
    return OwnerSnapshot(owner_id, {"id": turn_id, "status": status}, blocked, goal)


def _trusted_router(handle) -> bool:
    if _trusted_handle(handle):
        return True
    if sys.platform != "win32":
        return False
    import psutil

    query = ctypes.WinDLL("kernel32", use_last_error=True).GetNamedPipeServerProcessId
    query.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG)]
    query.restype = wintypes.BOOL
    pid = wintypes.ULONG()
    if not query(int(handle), ctypes.byref(pid)):
        return False
    try:
        process = psutil.Process(pid.value)
        allowed = {str(path.resolve()).casefold() for path in installed_editors().values()}
        return (
            str(Path(process.exe()).resolve()).casefold() in allowed
            and process.username() == psutil.Process().username()
        )
    except (OSError, psutil.Error):
        return False


class OwnerProtocol:
    def __init__(self, pipe):
        self.pipe = pipe
        self.client_id = "codex-account-manager"

    def request(self, method: str, params: dict, version: int, owner: str | None = None) -> dict:
        request_id = str(uuid4())
        message = {
            "type": "request",
            "method": method,
            "params": params,
            "version": version,
            "requestId": request_id,
            "sourceClientId": self.client_id,
            "timeoutMs": 5000,
        }
        if owner:
            message["targetClientId"] = owner
        self.pipe.send(message)
        for _ in range(MAX_MESSAGES):
            response = self.pipe.receive()
            if response.get("type") == "client-discovery-request":
                self.pipe.send(
                    {
                        "type": "client-discovery-response",
                        "requestId": response.get("requestId"),
                        "response": {"canHandle": False},
                    }
                )
            if response.get("type") != "response" or response.get("requestId") != request_id:
                continue
            if (
                method == "thread-owner-discovery"
                # Router-generated discovery misses omit the method; requestId
                # above still binds this response to our exact discovery request.
                and response.get("method") in {None, method}
                and response.get("resultType") == "error"
                and response.get("error") == "no-client-found"
            ):
                raise OwnerNotFoundError("No local client owns this conversation.")
            if response.get("resultType") != "success" or response.get("method") != method:
                raise AppServerError("The IDE rejected the operation or its protocol version.")
            if owner and response.get("handledByClientId") != owner:
                raise AppServerError("The IDE conversation owner changed.")
            return response
        raise AppServerError("Too many unrelated local messages.")

    def initialize(self) -> None:
        reply = self.request("initialize", {"clientType": "codex-account-manager"}, 0)
        client_id = reply.get("result", {}).get("clientId")
        if not isinstance(client_id, str) or not IDENTIFIER.fullmatch(client_id):
            raise AppServerError("The local router did not identify this client.")
        self.client_id = client_id

    def inspect(self, thread_id: str, expected_owner: str | None = None) -> OwnerSnapshot:
        reply = self.request(
            "thread-owner-discovery",
            {"hostId": "local", "conversationId": thread_id},
            1,
            expected_owner,
        )
        owner = reply.get("handledByClientId")
        if not isinstance(owner, str) or not IDENTIFIER.fullmatch(owner):
            raise AppServerError("The IDE owner identity is invalid.")
        self.pipe.send(
            {
                "type": "broadcast",
                "method": "thread-stream-following-changed",
                "version": 1,
                "sourceClientId": self.client_id,
                "targetClientIds": [owner],
                "params": {"hostId": "local", "conversationId": thread_id, "following": True},
            }
        )
        for _ in range(MAX_MESSAGES):
            response = self.pipe.receive()
            if (
                response.get("method") == "thread-stream-state-changed"
                and response.get("sourceClientId") == owner
            ):
                return snapshot(response, thread_id, owner)
        raise AppServerError("No current IDE snapshot was received.")

    def send(self, thread_id: str, expected: OwnerSnapshot, message_id: str) -> dict:
        current = self.inspect(thread_id, expected.owner_id)
        if current.blocked or current.turn != expected.turn or current.goal != expected.goal:
            raise AppServerError("The IDE conversation changed before continuation.")
        if current.turn.get("status") not in {"failed", "completed"}:
            raise AppServerError("The IDE conversation is not idle.")
        reply = self.request(
            "thread-follower-start-turn",
            {
                "conversationId": thread_id,
                "turnStart": {
                    "request": {
                        "threadId": thread_id,
                        "clientUserMessageId": message_id,
                        "input": [{"type": "text", "text": CONTINUE_PROMPT, "text_elements": []}],
                    },
                    "context": {"inheritThreadSettings": True},
                },
            },
            2,
            current.owner_id,
        )
        result = reply.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("result"), dict):
            raise AppServerError("IDE continuation delivery is unconfirmed.")
        turn = result["result"].get("turn")
        if (
            not isinstance(turn, dict)
            or not isinstance(turn.get("id"), str)
            or not IDENTIFIER.fullmatch(turn["id"])
        ):
            raise AppServerError("IDE continuation delivery is unconfirmed.")
        return {"id": turn["id"], "status": turn.get("status")}


def _exchange(
    thread_id: str,
    expected: OwnerSnapshot | None,
    message_id: str | None,
    cancelled: threading.Event,
):
    if sys.platform != "win32":
        raise AppServerError("IDE continuation requires Windows.")
    import pywintypes
    import win32con
    import win32file

    sending = expected is not None and bool(message_id)
    try:
        handle = win32file.CreateFile(
            r"\\.\pipe\codex-ipc",
            win32con.GENERIC_READ | win32con.GENERIC_WRITE,
            0,
            None,
            win32con.OPEN_EXISTING,
            win32con.FILE_FLAG_OVERLAPPED,
            None,
        )
        with FramedPipe(handle, cancelled, 12, MAX_FRAME, max_received=MAX_RECEIVED) as pipe:
            if not _trusted_router(handle):
                raise AppServerError("The local Codex router could not be verified.")
            client = OwnerProtocol(pipe)
            client.initialize()
            return (
                client.send(thread_id, expected, message_id)
                if expected is not None and message_id
                else client.inspect(thread_id)
            )
    except pywintypes.error as exc:
        if sending:
            raise AppServerError("IDE continuation delivery is unconfirmed.") from exc
        raise ConnectionNotReadyError("The local IDE channel is reconnecting.") from exc


class NativeIDE:
    async def _call(self, thread_id: str, expected=None, message_id=None):
        if not IDENTIFIER.fullmatch(thread_id):
            raise AppServerError("Invalid IDE conversation identity.")
        cancelled = threading.Event()
        try:
            async with asyncio.timeout(13):
                return await asyncio.to_thread(
                    _exchange, thread_id, expected, message_id, cancelled
                )
        finally:
            cancelled.set()

    async def inspect(self, thread_id: str) -> OwnerSnapshot:
        return await self._call(thread_id)

    async def wait_inspect(self, thread_id: str, timeout: float = 20.0) -> OwnerSnapshot:
        try:
            async with asyncio.timeout(timeout):
                while True:
                    try:
                        return await self.inspect(thread_id)
                    except LocalResponseTooLargeError:
                        raise
                    except (AppServerError, OSError, TimeoutError):
                        await asyncio.sleep(0.25)
        except TimeoutError as exc:
            raise ConnectionNotReadyError(
                "The local conversation owner did not become ready."
            ) from exc

    async def latest_turn(self, thread_id: str) -> dict:
        return (await self.inspect(thread_id)).turn

    async def send(self, thread_id: str, expected: OwnerSnapshot, message_id: str) -> dict:
        return await self._call(thread_id, expected, message_id)
