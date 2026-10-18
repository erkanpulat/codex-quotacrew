"""Bounded, local-only access to the running Desktop's native tool channel.

The channel is private and version-dependent. Failure never falls back to a
separate conversation writer. Only read_thread and send_message_to_thread are
exposed; model, approval and sandbox settings are never overridden.

Security constraints:
- The pipe server process path must match the expected Codex Desktop MSIX
  package and belong to the current Windows user before data is exchanged.
- ``callerSource`` and ``namespace`` are protocol-required values observed
  from Desktop's internal communication. They select the relay protocol;
  they are not an authentication mechanism. This application identifies itself via the App Server's
  ``clientInfo`` during ``initialize`` (see app_server.py).
- No debugging port is opened, no bundle is patched, and no approval or
  sandbox preference is overridden.
- The protocol was informed by codex-mcp-bridge's native relay
  (https://github.com/buidangminh23/codex-mcp-bridge).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import threading
from typing import TYPE_CHECKING
from uuid import uuid4

from codex_account_manager.codex.status import normalize_turn_status
from codex_account_manager.core.errors import (
    AppServerError,
    ConnectionNotReadyError,
    LocalResponseTooLargeError,
)

if TYPE_CHECKING:
    from codex_account_manager.adapters.app_server import CodexAppServer

MAX_FRAME = 1024 * 1024
PIPE_NAME = re.compile(r"codex-(?:app-tools|browser-use)-[0-9a-f-]{36}", re.I)
# Restrict connections to the expected installed MSIX package path.
DESKTOP_EXECUTABLE = re.compile(
    r"[a-z]:\\Program Files\\WindowsApps\\OpenAI\.Codex_[0-9.]+_"
    r"(?:x64|arm64)__2p2nqsd0c76g0\\app\\(?:chatgpt|codex)\.exe",
    re.I,
)
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
CONTINUE_PROMPT = (
    "Continue the previously requested unfinished work from its current state. "
    "Follow the existing goal, user instructions, permissions and budget. "
    "Do not redo completed work. If the work is complete or needs user input, "
    "report that and stop."
)


def decode_result(response: dict, request_id: str) -> dict:
    if (
        not isinstance(response, dict)
        or response.get("id") != request_id
        or response.get("jsonrpc") != "2.0"
    ):
        raise AppServerError("Desktop returned an unexpected response.")
    result = response.get("result")
    error = response.get("error")
    if isinstance(error, dict) and "No ready app window supports app tools" in str(
        error.get("message", "")
    ):
        raise ConnectionNotReadyError("Codex Desktop has not finished preparing its app channel.")
    if error or not isinstance(result, dict) or result.get("success") is not True:
        raise AppServerError("Desktop rejected the native operation.")
    items = result.get("contentItems")
    if not isinstance(items, list):
        raise AppServerError("Desktop returned no native result.")
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            try:
                data = json.loads(item["text"])
            except ValueError:
                continue
            if isinstance(data, dict) and not data.get("isError"):
                return data
    raise AppServerError("Desktop returned an unsupported native result.")


def read_snapshot(data: dict, thread_id: str) -> dict:
    thread = data.get("thread")
    turns = data.get("turns")
    if (
        not isinstance(thread, dict)
        or thread.get("id") != thread_id
        or thread.get("kind") != "codex"
        or thread.get("hostId") != "local"
        or not isinstance(turns, list)
        or not turns
        or not isinstance(turns[0], dict)
        or not isinstance(turns[0].get("id"), str)
        or not IDENTIFIER.fullmatch(turns[0]["id"])
        or normalize_turn_status(turns[0].get("status"))
        not in {"inProgress", "completed", "failed", "interrupted"}
    ):
        raise AppServerError("Desktop conversation identity could not be verified.")
    return {**turns[0], "status": normalize_turn_status(turns[0].get("status"))}


def _trusted_handle(handle) -> bool:
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes

    import psutil

    query = ctypes.WinDLL("kernel32", use_last_error=True).GetNamedPipeServerProcessId
    query.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG)]
    query.restype = wintypes.BOOL
    pid = wintypes.ULONG()
    if not query(int(handle), ctypes.byref(pid)):
        return False
    try:
        process = psutil.Process(pid.value)
        executable = process.exe().replace("/", "\\").casefold()
        return (
            DESKTOP_EXECUTABLE.fullmatch(executable) is not None
            and process.username() == psutil.Process().username()
        )
    except psutil.Error:
        return False


def _exchange(payload: dict, cancelled: threading.Event, timeout: float) -> dict:
    if os.name != "nt":
        raise AppServerError("Native Desktop continuation requires Windows.")
    import pywintypes
    import win32con
    import win32file

    prefix = "\\\\.\\pipe\\"
    candidates = []
    inherited = os.environ.get("CODEX_APP_TOOLS_PIPE_PATH", "")
    if inherited.startswith(prefix) and PIPE_NAME.fullmatch(inherited[len(prefix) :]):
        candidates.append(inherited)
    discovered = [name for name in os.listdir(prefix) if PIPE_NAME.fullmatch(name)]
    # New Desktop versions expose app tools separately from browser automation.
    discovered.sort(key=lambda name: not name.lower().startswith("codex-app-tools-"))
    candidates.extend(prefix + name for name in discovered)
    candidates.sort(key=lambda path: not path.lower().startswith(prefix + "codex-app-tools-"))
    if len(set(candidates)) > 16:
        raise AppServerError("Too many Desktop channels; identity is ambiguous.")
    handle = None
    for candidate in dict.fromkeys(candidates):
        if cancelled.is_set():
            raise AppServerError("Desktop operation cancelled before delivery.")
        try:
            opened = win32file.CreateFile(
                candidate,
                win32con.GENERIC_READ | win32con.GENERIC_WRITE,
                0,
                None,
                win32con.OPEN_EXISTING,
                win32con.FILE_FLAG_OVERLAPPED,
                None,
            )
        except pywintypes.error:
            continue
        try:
            if _trusted_handle(opened):
                handle = opened
                break
        finally:
            if handle is not opened:
                opened.Close()
    if handle is None:
        raise ConnectionNotReadyError("The verified Desktop native channel is unavailable.")
    from codex_account_manager.adapters.windows_pipe import FramedPipe

    with FramedPipe(handle, cancelled, timeout, MAX_FRAME) as pipe:
        pipe.send(payload)
        return pipe.receive()


class NativeDesktop:
    def __init__(self) -> None:
        self._exchange_lock = asyncio.Lock()

    async def call(self, thread_id: str, source_turn_id: str, *, send: bool = False) -> dict:
        if not IDENTIFIER.fullmatch(thread_id) or not IDENTIFIER.fullmatch(source_turn_id):
            raise AppServerError("Invalid native conversation identity.")
        request_id = str(uuid4())
        arguments = (
            {"threadId": thread_id, "prompt": CONTINUE_PROMPT}
            if send
            else {
                "threadId": thread_id,
                "hostId": "local",
                "turnLimit": 1,
                "maxOutputCharsPerItem": 1,
            }
        )
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {
                "arguments": arguments,
                # Routing values required by the observed private relay protocol.
                "callerSource": "codex",
                "callId": request_id,
                "namespace": "codex_app",
                "threadId": thread_id,
                "tool": "send_message_to_thread" if send else "read_thread",
                "turnId": source_turn_id,
            },
        }
        cancelled = threading.Event()
        async with self._exchange_lock:
            exchange_task = asyncio.create_task(
                asyncio.to_thread(_exchange, payload, cancelled, 60.0 if send else 15.0)
            )
            try:
                result = await asyncio.shield(exchange_task)
                return decode_result(result, request_id)
            except asyncio.CancelledError:
                cancelled.set()
                try:
                    await exchange_task
                except BaseException:
                    pass
                raise
            finally:
                cancelled.set()

    async def latest_turn(self, thread_id: str) -> dict:
        for attempt in range(3):
            try:
                return read_snapshot(
                    await self.call(thread_id, "account-manager-observation"), thread_id
                )
            except ConnectionNotReadyError:
                if attempt == 2:
                    raise
                await asyncio.sleep(0.75 * (2**attempt))
        raise ConnectionNotReadyError("Codex Desktop app channel is not ready.")

    async def wait_latest_turn(self, thread_id: str, timeout: float = 20.0) -> dict:
        """Allow Desktop to initialize after restart; retry only read operations."""
        try:
            async with asyncio.timeout(timeout):
                while True:
                    try:
                        return await self.latest_turn(thread_id)
                    except LocalResponseTooLargeError:
                        raise
                    except (AppServerError, OSError):
                        await asyncio.sleep(0.25)
        except TimeoutError as exc:
            raise ConnectionNotReadyError(
                "Desktop native connection did not become ready."
            ) from exc

    async def send(self, thread_id: str, source_turn_id: str) -> dict:
        # A restarted Desktop can expose its process before its app-tools window
        # is ready. Retry only that explicit pre-delivery condition; other
        # failures remain terminal and are never replayed.
        for attempt in range(3):
            try:
                result = await self.call(thread_id, source_turn_id, send=True)
                break
            except ConnectionNotReadyError:
                if attempt == 2:
                    raise
                await asyncio.sleep(0.75 * (2**attempt))
        if result.get("threadId") != thread_id:
            raise AppServerError("Desktop delivery receipt does not match the conversation.")
        return result


async def verified_desktop_turn(
    native: NativeDesktop, adapter: CodexAppServer, thread_id: str, *, wait: bool = False
) -> dict:
    turn = await native.wait_latest_turn(thread_id) if wait else await native.latest_turn(thread_id)
    error = turn.get("error")
    if normalize_turn_status(turn.get("status")) != "failed" or (
        isinstance(error, dict) and error.get("codexErrorInfo") is not None
    ):
        return turn
    # Desktop summaries omit the structured error. Accept it only from the same
    # stored turn; matching human-readable messages cannot establish a quota failure.
    stored = await adapter.latest_turn(thread_id)
    if (
        not stored
        or stored.get("id") != turn.get("id")
        or normalize_turn_status(stored.get("status")) != "failed"
    ):
        raise AppServerError("The Desktop failure could not be matched to its stored turn.")
    return {**turn, "error": stored.get("error")}
