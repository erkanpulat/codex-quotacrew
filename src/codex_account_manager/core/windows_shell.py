"""Windows application identity and shortcuts for a source installation."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from codex_account_manager.platform import package

APP_USER_MODEL_ID = "CodexAccountManager.Desktop"
SHELL_LAUNCH_ARGUMENT = "--from-windows-shell"


def launch_from_explorer(executable: Path, arguments: list[str], *, visible: bool = True) -> None:
    """Delegate creation to the existing Explorer, outside the caller's job objects."""
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    try:
        desktop = (
            win32com.client.Dispatch("Shell.Application").Windows().FindWindowSW(0, 0, 8, 0, 1)
        )
        desktop.Document.Application.ShellExecute(
            str(executable),
            subprocess.list2cmdline(arguments),
            str(executable.parent),
            "open",
            1 if visible else 0,
        )
    finally:
        pythoncom.CoUninitialize()


def delegate_gui_launch() -> bool:
    if sys.platform != "win32" or SHELL_LAUNCH_ARGUMENT in sys.argv:
        return False
    if package.is_packaged():
        return False
    executable = Path(sys.executable)
    arguments = [SHELL_LAUNCH_ARGUMENT]
    if not getattr(sys, "frozen", False):
        executable = executable.with_name("pythonw.exe")
        arguments = ["-m", "codex_account_manager.gui.tray_main", *arguments]
    launch_from_explorer(executable, arguments)
    return True


def set_application_identity() -> None:
    if sys.platform == "win32" and not package.is_packaged():
        from win32com.shell import shell

        shell.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)


def create_desktop_shortcut(*, destination: Path | None = None) -> Path:
    """Create a per-user link on a worker thread, using MSIX shell activation."""
    if sys.platform != "win32":
        raise OSError("Desktop shortcuts are supported on Windows only.")
    import pythoncom
    from win32com.shell import shell, shellcon

    pythoncom.CoInitialize()
    try:
        identity = package.current_package()
        directory = destination or Path(
            shell.SHGetFolderPath(0, shellcon.CSIDL_DESKTOPDIRECTORY, None, 0)
        )
        path = directory / (
            "QuotaCrew (Development).lnk"
            if identity and ".Development_" in identity.family_name
            else "QuotaCrew.lnk"
        )
        if path.exists():
            raise FileExistsError("A QuotaCrew desktop shortcut already exists.")
        directory.mkdir(parents=True, exist_ok=True)
        if not getattr(sys, "frozen", False):
            return create_shortcuts(Path(__file__).resolve().parents[3], destinations=(directory,))[
                0
            ]
        shortcut = pythoncom.CoCreateInstance(
            shell.CLSID_ShellLink, None, pythoncom.CLSCTX_INPROC_SERVER, shell.IID_IShellLink
        )
        if identity:
            pidl, _attributes = shell.SHParseDisplayName(
                f"shell:AppsFolder\\{identity.family_name}!App", 0, None
            )
            shortcut.SetIDList(pidl)
        else:
            shortcut.SetPath(sys.executable)
            shortcut.SetWorkingDirectory(str(Path(sys.executable).parent))
            shortcut.SetIconLocation(sys.executable, 0)
        shortcut.SetDescription("QuotaCrew")
        shortcut.QueryInterface(pythoncom.IID_IPersistFile).Save(str(path), 1)
        return path
    finally:
        pythoncom.CoUninitialize()


def create_shortcuts(
    project_root: Path, *, destinations: tuple[Path, ...] | None = None
) -> list[Path]:
    if sys.platform != "win32":
        raise OSError("Desktop shortcuts are supported on Windows only.")

    import pythoncom
    from win32com.propsys import propsys, pscon
    from win32com.shell import shell, shellcon

    root = project_root.resolve()
    executable = root / ".venv" / "Scripts" / "quotacrew.exe"
    icon = root / "packaging" / "assets" / "app.ico"
    for required in (executable, icon):
        if not required.is_file():
            raise FileNotFoundError(f"Run scripts/bootstrap.ps1 first. Missing: {required}")
    if destinations is None:
        destinations = tuple(
            Path(shell.SHGetFolderPath(0, folder, None, 0))
            for folder in (shellcon.CSIDL_DESKTOPDIRECTORY, shellcon.CSIDL_PROGRAMS)
        )
    created = []
    for directory in destinations:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "QuotaCrew.lnk"
        shortcut = pythoncom.CoCreateInstance(
            shell.CLSID_ShellLink, None, pythoncom.CLSCTX_INPROC_SERVER, shell.IID_IShellLink
        )
        shortcut.SetPath(str(executable))
        shortcut.SetArguments("")
        shortcut.SetWorkingDirectory(str(root))
        shortcut.SetIconLocation(str(icon), 0)
        shortcut.SetDescription("QuotaCrew")
        shortcut.QueryInterface(pythoncom.IID_IPersistFile).Save(str(path), 1)
        properties = propsys.SHGetPropertyStoreFromParsingName(
            str(path), None, shellcon.GPS_READWRITE, propsys.IID_IPropertyStore
        )
        properties.SetValue(pscon.PKEY_AppUserModel_ID, propsys.PROPVARIANTType(APP_USER_MODEL_ID))
        properties.Commit()
        created.append(path)
        legacy = directory / "Codex Account Manager.lnk"
        if legacy.is_file():
            previous = pythoncom.CoCreateInstance(
                shell.CLSID_ShellLink, None, pythoncom.CLSCTX_INPROC_SERVER, shell.IID_IShellLink
            )
            previous.QueryInterface(pythoncom.IID_IPersistFile).Load(str(legacy))
            if (
                Path(previous.GetPath(0)[0]).resolve()
                == root / ".venv/Scripts/codex-account-manager.exe"
            ):
                legacy.unlink()
    return created


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Create desktop and Start Menu shortcuts.")
    parser.add_argument("project_root", type=Path)
    for shortcut_path in create_shortcuts(parser.parse_args().project_root):
        print(f"Shortcut created: {shortcut_path}")
