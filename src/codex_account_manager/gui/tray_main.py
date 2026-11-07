"""Startup entry point used by the Windows logon task.

Enforces a single instance and launches the GUI (which lives in the tray).
"""

from __future__ import annotations

import sys

from codex_account_manager.core.single_instance import SingleInstance


def main() -> int:
    from codex_account_manager.core.windows_shell import delegate_gui_launch

    if delegate_gui_launch():
        return 0
    lock = SingleInstance()
    if not lock.acquire():
        lock.request_activation()
        return 0
    try:
        from codex_account_manager.gui.app import run_gui

        return run_gui()
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
