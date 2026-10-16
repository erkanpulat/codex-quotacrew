"""QuotaCrew command line interface (``cx``).

Thin Typer app over the core services. Business logic lives in
``codex_account_manager.accounts``/``auth``/``continuity``/``goals``/``diagnostics``;
these commands only parse arguments and render results.

Commands: ``init``, ``status``, ``gui``, ``doctor``, ``profiles``, ``login``,
``bind``, ``account``, ``accounts``, ``quota``, ``switch``, ``resume``, plus the
``profile``, ``continuity`` and ``goal`` command groups.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from codex_account_manager.accounts.service import AccountService
from codex_account_manager.codex.quota import evaluate_quota
from codex_account_manager.codex.runtime import (
    run_sandbox_setup_elevated,
)
from codex_account_manager.core.errors import AccountManagerError
from codex_account_manager.core.logging import configure_logging
from codex_account_manager.core.paths import paths
from codex_account_manager.core.redaction import redact_text
from codex_account_manager.storage.database import initialize_database

app = typer.Typer(no_args_is_help=True, help="QuotaCrew — continuity & profile control center.")
profile_app = typer.Typer(no_args_is_help=True, help="Profile management.")
continuity_app = typer.Typer(no_args_is_help=True, help="Thread & goal continuity.")
goal_app = typer.Typer(no_args_is_help=True, help="Local goal notes and native Codex goal status.")
app.add_typer(profile_app, name="profile")
app.add_typer(continuity_app, name="continuity")
app.add_typer(goal_app, name="goal")

console = Console()
_accounts = AccountService()


@app.callback()
def main() -> None:
    # Ensure Unicode renders on legacy Windows code pages (e.g. cp1252).
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except (ValueError, OSError):
                pass
    try:
        configure_logging()
    except OSError:
        console.print(
            "[bold red]Error:[/bold red] Local data could not be accessed or protected. "
            "Check the data folder permissions; do not delete account data."
        )
        raise typer.Exit(1) from None
    _run(initialize_database())


def _format_reset(value: int | None) -> str:
    if not value:
        return "-"
    try:
        return datetime.fromtimestamp(value).astimezone().strftime("%d.%m.%Y %H:%M")
    except (OSError, OverflowError, ValueError):
        return str(value)


def _format_percent(value: float | None) -> str:
    return "-" if value is None else f"%{value:g}"


def _usage(allowed: bool | None) -> str:
    if allowed is True:
        return "[green]OK[/green]"
    if allowed is False:
        return "[red]LIMIT[/red]"
    return "[yellow]UNKNOWN[/yellow]"


def _run(coro):
    try:
        return asyncio.run(coro)
    except (AccountManagerError, ValueError, OSError) as exc:
        console.print(f"[bold red]Error:[/bold red] {escape(redact_text(str(exc)))}")
        raise typer.Exit(1) from exc


@app.command()
def init() -> None:
    """Create/upgrade the database (runs migrations)."""
    version = _run(initialize_database())
    console.print(f"[bold green]Ready:[/bold green] {paths.db_path} (schema v{version})")


@app.command()
def status() -> None:
    """Show a one-line status."""
    console.print("[bold green]QuotaCrew is installed.[/bold green]")


@app.command()
def gui() -> None:
    """Launch the graphical control center."""
    try:
        from PySide6 import QtWidgets  # noqa: F401

        from codex_account_manager.gui.tray_main import main as run_gui
    except ImportError:
        console.print(
            "[bold red]GUI dependencies are not installed.[/bold red] "
            "Install with: pip install 'codex-quotacrew[gui]'"
        )
        raise typer.Exit(1) from None
    raise typer.Exit(run_gui())


@app.command()
def doctor(
    bundle: bool = typer.Option(False, "--bundle", help="Export a redacted diagnostics zip."),
) -> None:
    """Run diagnostics. With --bundle, write a shareable redacted archive."""
    from codex_account_manager.diagnostics import export_bundle, run_diagnostics

    results = _run(run_diagnostics())
    table = Table(title="QuotaCrew Diagnostics")
    table.add_column("Check")
    table.add_column("Status")
    table.add_column("Detail")
    for r in results:
        table.add_row(r.name, "[green]OK[/green]" if r.ok else "[red]FAIL[/red]", r.detail)
    console.print(table)
    if bundle:
        path = _run(export_bundle())
        console.print(f"[green]Diagnostics bundle:[/green] {path}")


@profile_app.command("add")
def profile_add(alias: str) -> None:
    """Create a new profile (uses file credential storage)."""
    profile = _run(_accounts.create_profile(alias))
    console.print(f"[green]Profile created:[/green] {alias}")
    console.print(f"  id: {profile.id}")
    console.print(f"  codex_home: {profile.codex_home}")


@profile_app.command("login")
def profile_login(alias: str) -> None:
    """Run ``codex login`` inside the profile's home."""
    _run(_accounts.login_profile(alias))
    console.print("[green]Signed in and bound the profile.[/green]")


@profile_app.command("rename")
def profile_rename(alias: str, new_alias: str) -> None:
    _run(_accounts.rename_profile(alias, new_alias))
    console.print(f"[green]Renamed[/green] {alias} -> {new_alias}")


@profile_app.command("remove")
def profile_remove(alias: str) -> None:
    _run(_accounts.remove_profile(alias))
    console.print(f"[green]Removed profile:[/green] {alias}")


@app.command("profiles")
def profiles() -> None:
    """List profiles (backward-compatible)."""
    rows = _run(_accounts.list_profiles())
    if not rows:
        console.print("No profiles yet.")
        return
    for p in rows:
        console.print(f"[bold]{p.alias}[/bold]  ({'bound' if p.bound_account_id else 'unbound'})")
        console.print(f"  id: {p.id}")
        console.print(f"  codex_home: {p.codex_home}")


@app.command("login")
def login(alias: str) -> None:
    """Backward-compatible alias for ``cx profile login``."""
    profile_login(alias)


@app.command("bind")
def bind(alias: str) -> None:
    """Bind the profile to the account currently in its auth.json."""
    account_id = _run(_accounts.bind_current_account(alias))
    console.print(f"[green]Bound[/green] {alias} -> account {account_id}")


@app.command("sandbox-setup")
def sandbox_setup(alias: str) -> None:
    profile = _run(_accounts.profiles.get_by_alias(alias))
    if not profile:
        console.print(f"[red]Profile not found:[/red] {alias}")
        raise typer.Exit(1)
    try:
        code = run_sandbox_setup_elevated(profile.codex_home)
    except AccountManagerError as exc:
        console.print(f"[bold red]Error:[/bold red] {escape(str(exc))}")
        raise typer.Exit(1) from exc
    raise typer.Exit(code)


@app.command("account")
def account(alias: str) -> None:
    """Show account details and quota for one profile."""
    health = _run(_accounts.health(alias))
    console.print()
    console.print(f"[bold cyan]Profile:[/bold cyan] {health.alias}")
    console.print(f"[bold]Plan:[/bold] {health.plan_type or '-'}")
    console.print(f"[bold]Usage allowed:[/bold] {_usage(health.ordinary_usage_allowed)}")
    console.print(
        f"[bold]5-hour used:[/bold] {_format_percent(health.primary_used_percent)} "
        f"(reset {_format_reset(health.primary_resets_at)})"
    )
    console.print(
        f"[bold]Weekly used:[/bold] {_format_percent(health.secondary_used_percent)} "
        f"(reset {_format_reset(health.secondary_resets_at)})"
    )
    if health.account_match is None:
        console.print("[yellow]Account binding: not bound[/yellow]")
    elif health.account_match:
        console.print("[green]Account match: OK[/green]")
    else:
        console.print("[bold red]Account match: MISMATCH[/bold red]")
    if health.error:
        console.print(f"[red]{health.error}[/red]")


@app.command("accounts")
def accounts() -> None:
    """Structured dashboard of all profiles."""
    health = _run(_accounts.all_health())
    if not health:
        console.print("No profiles yet.")
        return
    table = Table(title="QuotaCrew accounts")
    for col in (
        "Profile",
        "Plan",
        "5h",
        "5h Reset",
        "Weekly",
        "Weekly Reset",
        "Usage",
        "Match",
        "Active",
    ):
        table.add_column(col)
    for h in health:
        match = (
            "-"
            if h.account_match is None
            else ("[green]OK[/green]" if h.account_match else "[red]MISMATCH[/red]")
        )
        table.add_row(
            h.alias,
            h.plan_type or "-",
            _format_percent(h.primary_used_percent),
            _format_reset(h.primary_resets_at),
            _format_percent(h.secondary_used_percent),
            _format_reset(h.secondary_resets_at),
            "[red]ERR[/red]" if h.error else _usage(h.ordinary_usage_allowed),
            match,
            "[cyan]active[/cyan]" if h.is_active else "",
        )
    console.print(table)


@app.command("quota")
def quota(alias: str) -> None:
    """Evaluate the quota decision for a profile."""
    snapshot = _run(_quota_snapshot(alias))
    decision = evaluate_quota(snapshot)
    console.print(f"[bold cyan]Profile:[/bold cyan] {alias}")
    console.print(f"[bold]Quota state:[/bold] {decision.state.value}")
    console.print(
        f"[bold]Usable:[/bold] {'[green]YES[/green]' if decision.allowed else '[red]NO[/red]'}"
    )
    console.print(f"[bold]Reason:[/bold] {decision.reason}")
    console.print(f"[bold]Next reset:[/bold] {_format_reset(decision.reset_at)}")


async def _quota_snapshot(alias: str):
    profile = await _accounts.profiles.get_by_alias(alias)
    if not profile:
        raise AccountManagerError(f"Profile not found: {alias}")
    return await _accounts.read_snapshot(profile.codex_home)


@app.command("switch")
def switch(
    alias: str,
    thread: str | None = typer.Option(None, "--thread", help="Track continuity for this thread."),
) -> None:
    """Switch to a profile with a transactional, crash-safe auth handoff."""
    from codex_account_manager.continuity.service import ContinuityService

    async def _do():
        service = ContinuityService(accounts=_accounts)
        return await service.handoff(alias, thread_id=thread)

    result = _run(_do())
    if result.success:
        console.print(f"[bold green]Switched to {alias}.[/bold green] Shared history preserved.")
        if result.detail:
            console.print(f"[yellow]Conversation could not be loaded: {result.detail}[/yellow]")
    else:
        console.print(f"[red]Switch did not complete (stage {result.final_stage.value}).[/red]")


@continuity_app.command("status")
def continuity_status() -> None:
    """Show tracked threads and recent handoffs."""
    from codex_account_manager.storage.repositories import ThreadRepository

    async def _do():
        threads = await ThreadRepository().list(limit=20)
        from codex_account_manager.continuity.service import ContinuityService

        handoffs = await ContinuityService(accounts=_accounts).recent_handoffs(10)
        return threads, handoffs

    threads, handoffs = _run(_do())
    if threads:
        table = Table(title="Tracked Threads")
        table.add_column("Thread")
        table.add_column("Workspace")
        table.add_column("Preview")
        for t in threads:
            table.add_row(t.id[:12], (t.workspace or "-")[:40], (t.preview or "-")[:50])
        console.print(table)
    else:
        console.print("No tracked threads yet. Run 'cx continuity sync'.")
    if handoffs:
        htable = Table(title="Recent Handoffs")
        htable.add_column("When")
        htable.add_column("Reason")
        htable.add_column("Result")
        for h in handoffs:
            result = "…" if h.success is None else ("OK" if h.success else "FAILED")
            htable.add_row(h.started_at.strftime("%d.%m %H:%M"), h.reason.value, result)
        console.print(htable)


@continuity_app.command("sync")
def continuity_sync() -> None:
    """Read threads from Codex and persist continuity records."""
    from codex_account_manager.continuity.service import ContinuityService

    records = _run(ContinuityService(accounts=_accounts).sync_threads())
    console.print(f"[green]Synced {len(records)} threads.[/green]")


@app.command("resume")
def resume(thread: str) -> None:
    """Resume a thread via the App Server and reconcile its goal."""
    from codex_account_manager.continuity.service import ContinuityService

    async def _do():
        await ContinuityService(accounts=_accounts).resume_conversation(thread)

    _run(_do())
    console.print(f"[green]Resumed thread {thread}.[/green]")


@goal_app.command("status")
def goal_status(thread: str) -> None:
    from codex_account_manager.goals.service import GoalService

    goal = _run(GoalService().get(thread))
    if not goal:
        console.print("No local goal for this thread.")
        return
    console.print(f"[bold]Objective:[/bold] {goal.objective}")
    console.print(f"[bold]Local status:[/bold] {goal.local_status.value}")
    console.print(f"[bold]Native goal present:[/bold] {goal.native_goal_present}")
    console.print(f"[bold]Revision:[/bold] {goal.revision}  user_cleared={goal.user_cleared}")


@goal_app.command("set")
def goal_set(thread: str, objective: str) -> None:
    from codex_account_manager.goals.service import GoalService

    _run(GoalService().set_objective(thread, objective))
    console.print("[green]Goal checkpoint saved.[/green]")


@goal_app.command("clear")
def goal_clear(thread: str) -> None:
    from codex_account_manager.goals.service import GoalService

    _run(GoalService().user_clear(thread))
    console.print("[green]Local goal note cleared. Native Codex goals are unchanged.[/green]")


if __name__ == "__main__":
    app()
