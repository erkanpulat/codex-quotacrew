"""Launch the packaged GUI in an isolated home and verify its real main window."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path
from xml.etree import ElementTree


def main() -> None:
    if sys.platform != "win32":
        raise SystemExit("This check requires Windows.")
    import win32api
    import win32con
    import win32gui
    import win32process

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", nargs="?", default="dist/QuotaCrew/QuotaCrew.exe")
    parser.add_argument(
        "--observe-seconds", type=int, choices=range(0, 301), default=0, metavar="0..300"
    )
    parser.add_argument(
        "--startup-timeout", type=int, choices=range(1, 121), default=25, metavar="1..120"
    )
    parser.add_argument(
        "--first-run",
        action="store_true",
        help="Verify the first-run setup window without accepting or installing anything.",
    )
    args = parser.parse_args()
    executable = Path(args.executable).resolve(strict=True)
    from codex_account_manager import __version__

    module = win32api.LoadLibraryEx(str(executable), 0, win32con.LOAD_LIBRARY_AS_DATAFILE)
    try:
        manifest = ElementTree.fromstring(win32api.LoadResource(module, 24, 1))
    finally:
        win32api.FreeLibrary(module)
    dpi = manifest.find(".//{http://schemas.microsoft.com/SMI/2016/WindowsSettings}dpiAwareness")
    if dpi is None or dpi.text != "PerMonitorV2":
        raise RuntimeError("The GUI executable must declare PerMonitorV2 DPI awareness.")
    execution = manifest.find(".//{urn:schemas-microsoft-com:asm.v3}requestedExecutionLevel")
    if execution is None or execution.get("level") != "asInvoker":
        raise RuntimeError("The GUI executable must run without requesting elevation.")
    for key, expected in (
        ("ProductName", "QuotaCrew for Codex"),
        ("FileDescription", "QuotaCrew"),
        ("ProductVersion", __version__),
        ("OriginalFilename", "QuotaCrew.exe"),
    ):
        if (
            win32api.GetFileVersionInfo(str(executable), rf"\StringFileInfo\040904B0\{key}")
            != expected
        ):
            raise RuntimeError(f"Unexpected Windows executable metadata: {key}")
    internal = executable.parent / "_internal"
    notices = (internal / "licenses" / "LICENSES.txt").read_text(encoding="utf-8")
    for required in (
        "GNU LESSER GENERAL PUBLIC LICENSE",
        "Python",
        "pydantic",
        "psutil",
        "pyinstaller",
    ):
        if required not in notices:
            raise RuntimeError(f"Missing binary license notice: {required}")
    qt = internal / "PySide6"
    unexpected = {p.name for p in qt.glob("Qt6*.dll")} - {
        "Qt6Core.dll",
        "Qt6Network.dll",
        "Qt6Gui.dll",
        "Qt6Widgets.dll",
    }
    if unexpected:
        raise RuntimeError(f"Unexpected Qt libraries bundled: {sorted(unexpected)}")
    with tempfile.TemporaryDirectory(prefix="account-manager-smoke-") as temporary:
        home = Path(temporary)
        env = os.environ.copy()
        env.update(
            USERPROFILE=str(home), LOCALAPPDATA=str(home / "local"), APPDATA=str(home / "roaming")
        )
        env["WIN_PD_OVERRIDE_LOCAL_APPDATA"] = str(home / "local")
        env["WIN_PD_OVERRIDE_APPDATA"] = str(home / "roaming")
        for key in ("CODEX_HOME", "QT_QPA_PLATFORM", "QT_PLUGIN_PATH", "QML2_IMPORT_PATH"):
            env.pop(key, None)
        for folder in (home / "local", home / "roaming"):
            folder.mkdir()
        if not args.first_run:
            import sqlite3

            app_data = home / "local" / "CodexAccountManager"
            app_data.mkdir()
            with closing(sqlite3.connect(app_data / "accounts.db")) as db:
                db.execute(
                    "CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)"
                )
                db.executemany(
                    "INSERT INTO settings VALUES (?, ?, 'smoke-test')",
                    [
                        ("setup_completed", "true"),
                        ("monitor_enabled", "false"),
                        ("check_updates", "false"),
                    ],
                )
                db.commit()
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = subprocess.SW_HIDE
        process = subprocess.Popen(
            [str(executable), "--from-windows-shell"],
            cwd=executable.parent,
            env=env,
            startupinfo=startup,
        )
        try:
            started_at = time.monotonic()
            deadline = started_at + args.startup_timeout
            while time.monotonic() < deadline:
                titles = []

                def collect(hwnd, _extra, titles=titles):
                    if win32process.GetWindowThreadProcessId(hwnd)[1] == process.pid:
                        title = win32gui.GetWindowText(hwnd)
                        if title:
                            titles.append(title)
                        win32gui.ShowWindow(hwnd, win32con.SW_HIDE)

                win32gui.EnumWindows(collect, None)
                if any("Unhandled exception" in title for title in titles):
                    raise RuntimeError("The packaged GUI opened a startup error dialog.")
                if any(
                    title
                    in (
                        {
                            "Welcome to QuotaCrew",
                            "QuotaCrew uygulamasına hoş geldiniz",
                            "Welcome to QuotaCrew - QuotaCrew",
                            "QuotaCrew uygulamasına hoş geldiniz - QuotaCrew",
                        }
                        if args.first_run
                        else {"QuotaCrew"}
                    )
                    for title in titles
                ):
                    time.sleep(0.5)
                    if process.poll() is not None:
                        raise RuntimeError("The GUI exited immediately after opening.")
                    if not (home / "local" / "CodexAccountManager" / "accounts.db").exists():
                        raise RuntimeError("The GUI did not use the isolated data directory.")
                    print(
                        f"Packaged GUI main window opened in {time.monotonic() - started_at:.1f}s "
                        "in an isolated home.",
                        flush=True,
                    )
                    if args.observe_seconds:
                        import psutil

                        observed = psutil.Process(process.pid)
                        before_cpu = observed.cpu_times()
                        before_rss = observed.memory_info().rss
                        before_handles = observed.num_handles()
                        peak_rss = before_rss
                        started = time.monotonic()
                        while time.monotonic() - started < args.observe_seconds:
                            time.sleep(1)
                            if process.poll() is not None:
                                raise RuntimeError("The packaged GUI exited during observation.")
                            peak_rss = max(peak_rss, observed.memory_info().rss)
                        after_cpu = observed.cpu_times()
                        cpu_seconds = (
                            after_cpu.user + after_cpu.system - before_cpu.user - before_cpu.system
                        )
                        print(
                            f"Isolated idle observation: {args.observe_seconds}s; "
                            f"GUI CPU {cpu_seconds:.3f}s; RSS peak {peak_rss / 1048576:.1f} MiB; "
                            f"RSS change {(observed.memory_info().rss - before_rss) / 1048576:.1f} MiB; "
                            f"handle change {observed.num_handles() - before_handles}. "
                            "Excludes child-process CPU and active account/model workloads."
                        )
                    return
                if process.poll() is not None:
                    raise RuntimeError(
                        f"GUI exited before opening a window (code {process.returncode}). Close other instances before running this check."
                    )
                time.sleep(0.1)
            raise TimeoutError(
                f"The packaged GUI did not open within {args.startup_timeout} seconds; windows: {titles!r}."
            )
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=10)


if __name__ == "__main__":
    main()
