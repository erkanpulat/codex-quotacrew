"""``cx doctor`` diagnostic checks.

Each check returns a structured :class:`CheckResult` with an ``ok`` flag and a
safe, redactable message. No check ever reads or emits credential contents — it
only reports *presence*, not values.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import UTC, datetime

from codex_account_manager.adapters.credential_store import FileCredentialStore
from codex_account_manager.codex.runtime import codex_command, find_codex
from codex_account_manager.core.errors import CodexNotFoundError
from codex_account_manager.core.paths import paths
from codex_account_manager.core.redaction import redact_text
from codex_account_manager.storage.repositories import ProfileRepository


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str


async def run_diagnostics() -> list[CheckResult]:
    results: list[CheckResult] = []

    try:
        codex = find_codex()
    except CodexNotFoundError:
        codex = None
    results.append(
        CheckResult("codex_cli", codex is not None, codex or "Codex CLI not found on PATH.")
    )

    if codex:
        try:
            version = await _codex_version(codex)
            results.append(CheckResult("codex_version", True, version))
        except (OSError, ValueError, TimeoutError) as exc:
            results.append(CheckResult("codex_version", False, redact_text(str(exc))))

    shared = paths.shared_codex_home
    results.append(
        CheckResult(
            "shared_codex_home",
            shared.exists(),
            str(shared) if shared.exists() else f"Missing: {shared}",
        )
    )

    # Sessions history present (do not enumerate contents).
    sessions = shared / "sessions"
    results.append(
        CheckResult(
            "session_history", sessions.exists(), "present" if sessions.exists() else "none"
        )
    )

    results.append(
        CheckResult(
            "database",
            paths.db_path.exists(),
            str(paths.db_path) if paths.db_path.exists() else "missing",
        )
    )

    # Profiles and auth-file presence (presence only, never contents).
    try:
        profiles = await ProfileRepository().list()
        for p in profiles:
            auth_present = FileCredentialStore().profile_auth_path(p.codex_home).is_file()
            bound = p.bound_account_id is not None
            results.append(
                CheckResult(
                    f"profile:{p.alias}",
                    auth_present and bound,
                    f"auth={'yes' if auth_present else 'no'}, bound={'yes' if bound else 'no'}",
                )
            )
        results.append(CheckResult("profile_count", True, str(len(profiles))))
    except Exception as exc:
        results.append(CheckResult("profiles", False, f"error: {exc}"))

    if sys.platform == "win32":
        import asyncio

        from codex_account_manager.platform import windows

        try:
            installed = await asyncio.to_thread(windows.is_desktop_installed)
            results.append(
                CheckResult(
                    "desktop_installed",
                    installed,
                    "Desktop is installed." if installed else windows.DESKTOP_MISSING_MESSAGE,
                )
            )
        except OSError:
            results.append(
                CheckResult(
                    "desktop_installed",
                    False,
                    "Codex Desktop installation could not be checked. Retry in Settings.",
                )
            )

        results.append(
            CheckResult(
                "desktop_running",
                True,
                "running" if windows.is_desktop_running() else "not running",
            )
        )

    pending = (paths.data_dir / "switch.recovery.json").exists()
    results.append(
        CheckResult(
            "pending_recovery",
            not pending,
            "Interrupted switch requires recovery." if pending else "No interrupted switch.",
        )
    )
    results.append(CheckResult("timestamp", True, datetime.now(UTC).isoformat()))
    return results


async def _codex_version(codex: str) -> str:
    import asyncio

    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *codex_command("--version", executable=codex),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=15)
        text = out.decode(errors="replace").strip()
        if proc.returncode != 0:
            detail = redact_text(err.decode(errors="replace").strip())[:400]
            raise ValueError(
                f"Codex exited with code {proc.returncode}. {detail} Run codex --version in a terminal, then restart this application."
            )
        if not text:
            raise ValueError(
                "Codex returned no version. Run codex --version in a terminal and check the CLI installation."
            )
        return text
    except TimeoutError as exc:
        raise TimeoutError(
            "Codex did not respond within 15 seconds. Try again; if this repeats, run codex --version in a terminal."
        ) from exc
    except OSError as exc:
        raise OSError(
            f"Could not start Codex ({exc}). Check the CLI installation and restart this application."
        ) from exc
    finally:
        if proc is not None and proc.returncode is None:
            proc.kill()
            await proc.wait()
