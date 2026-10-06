# Troubleshooting

Run `codex-accounts doctor` first. Use `--bundle` only when you need an archive, and review it before sharing.

| Symptom | Action |
| --- | --- |
| Codex CLI missing | Install Codex and ensure its executable is on PATH. Restart QuotaCrew after changing PATH. |
| No profiles on a new installation | My accounts → Add account, then select it and Sign in. |
| Account recovery required or existing accounts disappeared | Do not reset or delete data. Check the data directory in System check and preserve the database, backups and profile folders privately before investigating. |
| Unbound or mismatched account | Sign in again or choose More actions → Verify account again to check the profile identity. |
| Unknown quota | The server did not provide a definitive state. Refresh later; unknown capacity is excluded from automatic failover. |
| Workspace routing discovery unauthorized (401) | QuotaCrew makes at most one managed refresh attempt for an isolated profile. A repeated 401 or invalidated refresh token requires signing in again from My accounts. This is an authentication failure, not proof of exhausted quota. Uncertain continuation messages are not replayed. |
| Switch failed | Read the error and handoff detail. A successful rollback restores the previous auth/config; a pending recovery snapshot is retried on the next GUI launch or switch. |
| Desktop readiness timeout | Quit/reopen QuotaCrew from the tray after an update. Desktop detection verifies the new account before launch and waits for the packaged Desktop process; it rolls back if that process does not appear. |
| Goal needs user action | Native goal support is unavailable or restoration failed. Check the checkpoint and installed Codex version. |
| UI closes but tray remains | Use the tray's Quit action to exit. On systems without a tray, closing the window exits. |
| Conversation loaded but no work starts | Ordinary loading does not start work. Automatic continuation requires the Settings toggle, a successful limit-driven switch, and a turn that was observed running before its verified usage-limit interruption. Inspect the status bar; unsupported APIs, changed turns, pauses and unknown state stop automation. |
| Automatic continuation needs attention | Open/reload the same conversation in Codex and inspect approvals, user questions or errors. The application never auto-approves requests or blindly retries uncertain deliveries. |
| Account operation is busy during automatic work | Disable automatic continuation in Settings before manually switching or changing credentials. |

Do not delete a pending recovery snapshot blindly: it may be the only retained copy of the previous credentials. Back up relevant state privately before manual repair.

## Windows installation and shortcuts

From the checkout, run `scripts/bootstrap.ps1`. It installs the application and creates **QuotaCrew** shortcuts on the desktop and in the Start menu. Open either shortcut without a terminal. You can also run `.venv/Scripts/quotacrew.exe` directly.

The source shortcuts use the project icon and the same Windows application identity as the running GUI. Keep the checkout in place; if it moves, rerun bootstrap to update the shortcuts. Close an older running instance from the system tray before reopening after an update.
The portable build starts with `QuotaCrew.exe`; keep its accompanying files together.

Desktop restart support targets the packaged `OpenAI.Codex` app. Process detection accepts the current packaged `ChatGPT.exe` main process and older packaged `Codex.exe` builds, checking the exact WindowsApps package path so unrelated Codex CLI and ChatGPT processes are not stopped. The account identity is verified through App Server before Desktop starts. Restart readiness then checks for the packaged Desktop process; it does not prove a particular conversation is visible.

The optional Start with Windows setting uses the current user's Run registry key and requires no administrator access. A packaged install registers its executable; a source install registers the virtual environment's Python entry point.

Application data is stored under `%LOCALAPPDATA%/CodexAccountManager`. Codex history remains in `~/.codex`. The System check page shows the paths in use. Uninstalling the application does not delete account data or conversation history.

### Different account lists when launched from another app

Windows can redirect LocalAppData writes made by a packaged application into its
MSIX LocalCache. Launch QuotaCrew from its desktop or Start menu shortcut.
System check displays the resolved physical data directory. Compare that path
before restoring a backup; do not merge credential folders by filename or replace
one database with another without verifying account identities.
