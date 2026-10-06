# Account switching and continuation

QuotaCrew keeps saved account credentials separate while using the shared Codex conversation history in `~/.codex`.

## Account switching

Automatic switching requires a verified limited account and a verified available destination. Free, unknown and stale account states are excluded; manual selection remains available.

Before restarting Desktop, QuotaCrew validates the destination session. An invalidated session requires signing in again. Account operations are serialized, credential writes are atomic, and an interrupted switch retains a recovery snapshot. Failed activation restores the previous account when possible.

A successful account switch does not by itself prove that a conversation resumed or that an IDE reloaded its cached account.

## Automatic continuation

With monitoring and the relevant continuation option enabled:

1. Observe running work and a subsequent verified quota interruption.
2. Check the destination account and ensure other work or approvals do not block the handoff.
3. Switch accounts and reconnect to the original conversation.
4. Recheck its turn, goal and connection before sending one continuation request.
5. Record the observed result in Jobs.

The request preserves existing instructions, permissions and budgets. It is model input and can consume quota. User-paused, blocked or completed goals are not automatically restarted; approval requests remain with the user.

Unsent requests can wait for a connection for up to ten minutes. An uncertain submission is not resent automatically. Pausing or clearing tracking cancels waiting requests; account information continues refreshing. Historical failures found on the first observation are not automatically revived.

## Desktop and IDE support

Desktop and IDE continuation use separate local connections. A failure in one conversation does not redirect it to another client or prevent independent eligible conversations from proceeding.

Local IDE use requires OpenAI's Codex extension. Automatic continuation is experimental and depends on the installed client's connection support. Remote, WSL and cloud conversations are outside this local Windows integration.

**Refresh VS Code after switching** supports one local VS Code window. It requests a normal close, allows up to 30 seconds, then reopens the conversation's workspace and link. Save prompts and link-opening consent are not dismissed. Multiple windows and unknown running-work states prevent automatic refresh. Cursor and Windsurf are not supported by this refresh action.

The extension's cached account cannot be independently verified by QuotaCrew. If an authentication error remains, inspect the account in Codex and sign in again as needed. See [troubleshooting](troubleshooting.md).

## Work tracking

Jobs counts verified running work. Completed conversations are retired after a successful observation, including after account changes. Unknown status is not proof of running or finished work. Very short tasks may occur between checks.

Clear tracking removes QuotaCrew observations and pauses automation; it does not delete Codex conversations. The latest continuation details are available from each job's menu.

## Optional Windows shutdown

Plans require confirmation, apply only to the current application session and can be cancelled from the page or tray.

- **Selected work:** wait for the selected conversation to finish, including continuation turns. If it has a goal, that goal must complete. Errors, pauses and requests for input are not completion.
- **All limits:** require freshly verified exhausted five-hour quotas on every saved account, with no available account to switch to. Reset credits are not remaining five-hour capacity.
- **Timer:** choose 1–1440 minutes. The final two-minute warning is included in that duration; a one-minute timer warns immediately. This mode does not wait for work to finish.

Work and limit modes also wait for other observed Codex work to stop before a two-minute countdown. Checks run every 60 seconds while waiting, every 15 seconds during countdown and immediately before shutdown. Missing evidence resets the countdown.

Pausing monitoring cancels conditional plans; quitting cancels every plan. QuotaCrew does not force-close applications. Windows can delay shutdown for unsaved work; activity in unrelated programs is not monitored.
