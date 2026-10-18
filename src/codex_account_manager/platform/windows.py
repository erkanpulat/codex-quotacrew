"""Windows integration primitives.

Encapsulates the two things that must be done a specific way on Windows:

- Launch the packaged (MSIX) Codex Desktop via its App ID through
  ``explorer.exe shell:AppsFolder\\<AppId>`` (direct EXE launch fails with
  "no package identity" on some builds).
- Stop/detect only the packaged Desktop processes, whose main executable is
  ChatGPT.exe on current releases (older releases may use Codex.exe), without
  touching the Codex CLI or another ChatGPT installation. No console windows
  (``CREATE_NO_WINDOW``).

"""

from __future__ import annotations

import ctypes
import os
import re
import subprocess
import sys
import time
from ctypes import wintypes

import psutil

from codex_account_manager.core.logging import get_logger

log = get_logger(__name__)

#: Confirmed packaged app id for Codex Desktop.
CODEX_APP_ID = r"OpenAI.Codex_2p2nqsd0c76g0!App"
CODEX_DESKTOP_DOWNLOAD_URL = "https://learn.chatgpt.com/docs/windows/windows-app"
DESKTOP_MISSING_MESSAGE = "Codex Desktop is not installed. Install the official ChatGPT desktop app with Codex, then check again. Account switching has not started."


def is_desktop_installed() -> bool:
    """Check the current user's registered package without launching Explorer."""
    if sys.platform != "win32":
        return False
    if _desktop_processes():
        return True
    function = ctypes.WinDLL("kernel32", use_last_error=True).GetPackagesByPackageFamily
    function.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(wintypes.UINT),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(wintypes.UINT),
        wintypes.LPWSTR,
    ]
    function.restype = wintypes.LONG
    count, length = wintypes.UINT(), wintypes.UINT()
    code = function(
        CODEX_APP_ID.split("!", 1)[0], ctypes.byref(count), None, ctypes.byref(length), None
    )
    if code not in (0, 122):
        raise OSError("Codex Desktop installation could not be checked. Retry in Settings.")
    return count.value > 0


def require_desktop_installed() -> None:
    from codex_account_manager.core.errors import DesktopLaunchError

    if not is_desktop_installed():
        raise DesktopLaunchError(DESKTOP_MISSING_MESSAGE)


def _process_has_exited(pid: int) -> bool:
    import pywintypes
    import win32api
    import win32con
    import win32event
    import winerror

    try:
        handle = win32api.OpenProcess(win32con.SYNCHRONIZE, False, pid)
    except pywintypes.error as exc:
        if exc.winerror == winerror.ERROR_INVALID_PARAMETER:
            return True
        if exc.winerror == getattr(winerror, "ERROR_ACCESS_DENIED", 5):
            return False
        raise
    try:
        status = win32event.WaitForSingleObject(handle, 0)
        if status == win32con.WAIT_OBJECT_0:
            return True
        if status == win32con.WAIT_TIMEOUT:
            return False
        raise RuntimeError("Could not verify Desktop process termination.")
    finally:
        handle.Close()


def _desktop_processes() -> list[psutil.Process]:
    found = []
    for process in psutil.process_iter(["name", "exe"]):
        try:
            executable = (process.info.get("exe") or "").replace("/", "\\").casefold()
            name = (process.info.get("name") or "").casefold()
            if (
                name in {"chatgpt.exe", "codex.exe"}
                and "\\windowsapps\\openai.codex_" in executable
                and executable.endswith("\\app\\" + name)
                and not _process_has_exited(process.pid)
            ):
                found.append(process)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return found


def stop_desktop() -> None:
    """Stop only the packaged Desktop, never unrelated CLI or ChatGPT processes."""
    deadline = time.monotonic() + 30.0
    while True:
        processes = _desktop_processes()
        if not processes:
            return
        ancestors = {process.pid for process in psutil.Process().parents()}
        if ancestors.intersection(process.pid for process in processes):
            raise RuntimeError(
                "QuotaCrew was launched by Codex Desktop. Close QuotaCrew and open it "
                "from its Start menu shortcut before switching accounts. Desktop was not stopped."
            )
        # Stop the Electron parent before its children so it cannot replace them.
        identities = {process.pid for process in processes}
        ordered = []
        for process in processes:
            try:
                ordered.append((process.ppid() in identities, process))
            except psutil.NoSuchProcess:
                continue
        for _, process in sorted(ordered, key=lambda item: item[0]):
            try:
                process.terminate()
            except psutil.NoSuchProcess:
                pass
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            log.warning(
                "Desktop shutdown timed out with %d packaged processes remaining.", len(processes)
            )
            raise RuntimeError("Codex Desktop could not be stopped; account switch aborted.")
        # Windows can retain terminated PIDs while another process holds a handle.
        # Re-enumerate and check the process signal, also catching replacement children.
        time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))


def is_desktop_running() -> bool:
    return bool(_desktop_processes())


def launch_desktop() -> None:
    """Launch the packaged Codex Desktop by App ID (the reliable method)."""
    require_desktop_installed()
    subprocess.Popen(
        [
            os.path.join(os.environ.get("SystemRoot", "C:/Windows"), "explorer.exe"),
            rf"shell:AppsFolder\{CODEX_APP_ID}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def conversation_url(thread_id: str) -> str:
    if (
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", thread_id)
        or thread_id.lower() == "new"
    ):
        raise ValueError("Invalid conversation identifier.")
    return f"codex://threads/{thread_id}"


def open_conversation(thread_id: str) -> None:
    """Open an existing local conversation through the documented Desktop protocol."""
    url = conversation_url(thread_id)
    if sys.platform != "win32":
        raise RuntimeError("Opening Codex Desktop requires Windows.")
    os.startfile(url)
