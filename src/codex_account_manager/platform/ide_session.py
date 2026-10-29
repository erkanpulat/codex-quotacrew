"""Refresh one local VS Code window without terminating unsaved work."""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path

import psutil

from codex_account_manager.core.errors import AppServerError
from codex_account_manager.core.windows_shell import launch_from_explorer
from codex_account_manager.platform.editors import installed_editors


@dataclass(frozen=True)
class EditorWindow:
    executable: Path
    pid: int
    created_at: float
    handle: int


def running_window() -> EditorWindow:
    if sys.platform != "win32":
        raise AppServerError("Automatic IDE refresh requires local VS Code.")
    import win32gui
    import win32process

    executable = installed_editors().get("VS Code")
    if executable is None:
        raise AppServerError("Automatic IDE refresh requires local VS Code.")
    windows = []
    user = psutil.Process().username()

    def collect(handle, _extra):
        if not win32gui.IsWindowVisible(handle) or win32gui.GetWindow(handle, 4):
            return
        if win32gui.GetClassName(handle) != "Chrome_WidgetWin_1":
            return
        try:
            process = psutil.Process(win32process.GetWindowThreadProcessId(handle)[1])
            if Path(process.exe()).resolve() == executable.resolve() and process.username() == user:
                windows.append(EditorWindow(executable, process.pid, process.create_time(), handle))
        except (psutil.Error, OSError):
            return

    win32gui.EnumWindows(collect, None)
    if len(windows) != 1:
        raise AppServerError("Automatic IDE refresh requires exactly one local VS Code window.")
    return windows[0]


def request_close(window: EditorWindow) -> None:
    import win32con
    import win32gui

    process = psutil.Process(window.pid)
    import win32process

    if (
        process.create_time() != window.created_at
        or Path(process.exe()).resolve() != window.executable.resolve()
        or win32process.GetWindowThreadProcessId(window.handle)[1] != window.pid
        or process.username() != psutil.Process().username()
    ):
        raise AppServerError("The VS Code process changed before refresh.")
    win32gui.PostMessage(window.handle, win32con.WM_CLOSE, 0, 0)


def has_exited(window: EditorWindow) -> bool:
    from codex_account_manager.platform.windows import _process_has_exited

    try:
        process = psutil.Process(window.pid)
        return process.create_time() != window.created_at or _process_has_exited(window.pid)
    except psutil.NoSuchProcess:
        return True


def open_thread(executable: Path, thread_id: str, workspace: str | None = None) -> None:
    from codex_account_manager.adapters.native_desktop import IDENTIFIER

    if not IDENTIFIER.fullmatch(thread_id):
        raise AppServerError("Invalid IDE conversation identity.")
    arguments = ["--reuse-window"]
    if workspace is not None:
        path = Path(workspace)
        if workspace.startswith(("\\\\", "//")) or not path.is_absolute() or not path.is_dir():
            raise AppServerError("IDE refresh requires an existing local workspace.")
        launch_from_explorer(executable, ["--reuse-window", str(path.resolve())])
    arguments.extend(["--open-url", f"vscode://openai.chatgpt/local/{thread_id}"])
    launch_from_explorer(executable, arguments)


class IDESession:
    def __init__(self):
        self._refreshed: tuple[str, int, float] | None = None

    async def needs_refresh(self, owner_key: str) -> bool:
        window = await asyncio.to_thread(running_window)
        return self._refreshed != (owner_key, window.pid, window.created_at)

    async def refresh(self, owner_key: str, thread_id: str, workspace: str) -> None:
        window = await asyncio.to_thread(running_window)
        if self._refreshed == (owner_key, window.pid, window.created_at):
            await asyncio.to_thread(open_thread, window.executable, thread_id)
            return
        path = Path(workspace)
        if workspace.startswith(("\\\\", "//")) or not path.is_absolute() or not path.is_dir():
            raise AppServerError("IDE refresh requires an existing local workspace.")
        await asyncio.to_thread(request_close, window)
        try:
            async with asyncio.timeout(30):
                while not await asyncio.to_thread(has_exited, window):
                    await asyncio.sleep(0.25)
        except TimeoutError as exc:
            raise AppServerError(
                "VS Code did not close. Resolve its save dialog before retrying."
            ) from exc
        await asyncio.to_thread(open_thread, window.executable, thread_id, workspace)
        try:
            async with asyncio.timeout(30):
                while True:
                    try:
                        opened = await asyncio.to_thread(running_window)
                        if (opened.pid, opened.created_at) != (window.pid, window.created_at):
                            self._refreshed = (owner_key, opened.pid, opened.created_at)
                            return
                    except AppServerError:
                        pass
                    await asyncio.sleep(0.25)
        except TimeoutError as exc:
            raise AppServerError("VS Code did not reopen after account refresh.") from exc
