import os
import shutil
import subprocess
from pathlib import Path

from codex_account_manager.core.errors import AccountManagerError, CodexNotFoundError
from codex_account_manager.platform import package


def find_codex() -> str:
    codex = shutil.which("codex.exe") or shutil.which("codex") or shutil.which("codex.cmd")
    if not codex and os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            standalone = Path(local) / "Programs/OpenAI/Codex/bin/codex.exe"
            if standalone.is_file():
                codex = str(standalone)

    if not codex:
        raise CodexNotFoundError("Codex CLI not found on PATH.")

    return codex


def profile_environment(codex_home: str | Path) -> dict[str, str]:
    env = os.environ.copy()
    env["CODEX_HOME"] = str(codex_home)
    return env


def run_sandbox_setup_elevated(codex_home: str | Path) -> int:
    if package.is_packaged():
        raise AccountManagerError(
            "Administrator sandbox setup is unavailable in the Microsoft Store edition. "
            "Manage sandbox setup separately through the official Codex CLI."
        )
    codex = find_codex()

    if Path(codex).suffix.lower() != ".exe":
        raise ValueError("Elevated sandbox setup requires a native codex.exe on PATH.")
    arguments = subprocess.list2cmdline(
        ["sandbox", "setup", "--elevated", "--current-user", "--codex-home", str(codex_home)]
    )
    executable_literal = codex.replace("'", "''")
    arguments_literal = arguments.replace("'", "''")
    script = f"$p = Start-Process -FilePath '{executable_literal}' -ArgumentList '{arguments_literal}' -WindowStyle Hidden -Verb RunAs -Wait -PassThru; exit $p.ExitCode"
    powershell = (
        Path(os.environ.get("SystemRoot", "C:/Windows"))
        / "System32/WindowsPowerShell/v1.0/powershell.exe"
    )
    return subprocess.run(
        [str(powershell), "-NoProfile", "-Command", script], check=False
    ).returncode


def codex_command(*args: str, executable: str | None = None) -> list[str]:
    executable = executable or find_codex()
    if Path(executable).suffix.lower() not in {".cmd", ".bat"}:
        return [executable, *args]
    parts = [executable, *args]
    if any(any(char in part for char in '"%!&|<>^()\r\n') for part in parts):
        raise ValueError(
            "Unsafe shell characters in Codex command; use a native codex.exe installation."
        )
    # A pre-quoted command is escaped again by subprocess.list2cmdline on Windows.
    # CALL keeps the command after /c unquoted while Python quotes each path/argument.
    return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", "call", *parts]
