"""Single-instance guard.

Ensures only one QuotaCrew background/tray process runs at a time. Uses a
named Windows mutex when available and falls back to an exclusive lock file on
other platforms (so the core stays importable and testable off-Windows).
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

from codex_account_manager.core.paths import paths

_MUTEX_NAME = "Local\\CodexAccountManagerSingleton"


class SingleInstance:
    """Acquire a process-wide single-instance lock.

    Usage::

        with SingleInstance() as lock:
            if not lock.acquired:
                sys.exit(0)
            ...
    """

    def __init__(self, name: str | None = None):
        self.name = name or _MUTEX_NAME
        if name is None and os.environ.get("WIN_PD_OVERRIDE_LOCAL_APPDATA"):
            # An explicitly isolated data home must not acquire the daily app's
            # mutex. Keep the legacy mutex for normal installs and upgrades.
            directory = os.path.normcase(str(paths.data_dir.resolve()))
            self.name += "." + hashlib.sha256(directory.encode("utf-8")).hexdigest()[:16]
        self.acquired = False
        self._handle = None
        self._lock_path: Path | None = None
        self._lock_fd: int | None = None

    def acquire(self) -> bool:
        if sys.platform == "win32":
            return self._acquire_windows()
        return self._acquire_posix()

    def activation_message(self) -> int:
        if sys.platform != "win32":
            return 0
        import ctypes
        from ctypes import wintypes

        register = ctypes.WinDLL("user32", use_last_error=True).RegisterWindowMessageW
        register.argtypes = [wintypes.LPCWSTR]
        register.restype = wintypes.UINT
        return register(self.name + ".ShowWindow")

    def request_activation(self) -> bool:
        if sys.platform != "win32":
            return False
        message = self.activation_message()
        if not message:
            return False
        import ctypes
        from ctypes import wintypes

        post = ctypes.WinDLL("user32", use_last_error=True).PostMessageW
        post.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        post.restype = wintypes.BOOL
        return bool(post(0xFFFF, message, 0, 0))  # HWND_BROADCAST reaches hidden top-level windows.

    def _acquire_windows(self) -> bool:
        if sys.platform != "win32":
            return False
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.CreateMutexW(None, False, self.name)
        if not handle:
            return False
        # ERROR_ALREADY_EXISTS == 183
        if ctypes.get_last_error() == 183:
            kernel32.CloseHandle(handle)
            return False
        self._handle = handle
        self.acquired = True
        return True

    def _acquire_posix(self) -> bool:
        paths.data_dir.mkdir(parents=True, exist_ok=True)
        self._lock_path = paths.data_dir / "account-manager.lock"
        try:
            import fcntl

            fd = os.open(self._lock_path, os.O_CREAT | os.O_RDWR)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[attr-defined]
            self._lock_fd = fd
            self.acquired = True
            return True
        except (OSError, BlockingIOError):
            if "fd" in locals():
                os.close(fd)
            return False

    def release(self) -> None:
        if self._handle is not None:
            import ctypes
            from ctypes import wintypes

            close = ctypes.windll.kernel32.CloseHandle
            close.argtypes = [wintypes.HANDLE]
            close.restype = wintypes.BOOL
            close(self._handle)
            self._handle = None
        if self._lock_fd is not None:
            try:
                import fcntl

                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)  # type: ignore[attr-defined]
                os.close(self._lock_fd)
            except OSError:
                pass
            self._lock_fd = None
        self.acquired = False

    def __enter__(self) -> SingleInstance:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()
