# Account switching and continuation

QuotaCrew keeps saved account credentials separate while using the shared Codex conversation history in `~/.codex`.

## Account switching

Automatic switching requires a verified limited account and a verified available destination. Free, unknown and stale account states are excluded; manual selection remains available.

An explicitly rejected sign-in can also trigger recovery to a verified available account. A workspace-routing 401 is treated separately: it does not prove quota exhaustion or permanent sign-in expiry. Failed routing checks remain visible and are retried by monitoring; manual switching does not depend on a successful conversation scan. Isolated profile authentication refresh is bounded to one attempt per check.

Before restarting Desktop, QuotaCrew validates the destination session. An invalidated session requires signing in again. Account operations are serialized, credential writes are atomic, and an interrupted switch retains a recovery snapshot. Failed activation restores the previous account when possible.

A confirmed manual switch stops QuotaCrew's continuation queue and interrupts any turn owned by that queue before changing credentials. It waits for connection cleanup and the account lock; clearing tracking is not required. Conversations and goal checkpoints are preserved.

Immediately before that restart, eligible Desktop goals are captured only after verifying their current goal, budget and live activity. After the switch, QuotaCrew opens each unchanged interrupted goal once without a message, or observes it if Desktop already resumed it. This also supports resuming the same turn identity. Full conversation history is not required. Ordinary running conversations are not sent a replacement prompt merely because of a manual switch. Desktop observations are invalidated after the restart, so an old running badge does not prove resumption.

A successful account switch does not by itself prove that a conversation resumed or that an IDE reloaded its cached account.

## Automatic continuation

With monitoring and the relevant continuation option enabled:

1. Observe running work and a subsequent verified quota interruption.
2. Check the destination account and ensure other work or approvals do not block the handoff.
3. Switch accounts and reconnect to the original conversation.
4. Recheck its turn, goal and connection. For eligible Desktop goals, verify Desktop readiness and open the conversation so Codex can resume its goal. For other supported work, send one continuation request.
5. Record the observed result in Jobs.

Goal opening sends no message. If a new turn is already running, QuotaCrew observes it without another action. A goal whose quota interruption is verified may be reactivated, preserving its budget. User-paused, blocked or completed goals are not automatically restarted; approval requests remain with the user. Other continuation requests are model input and can consume quota.

After opening a goal, an independent observer checks lightweight Desktop activity for up to 60 seconds. It releases the account lock between checks and does not hold up other conversations. After at least ten seconds it may open the conversation one more time, only after rechecking the same account, available capacity, unchanged eligible goal and quota turn, and idle Desktop state. The deadline and second-opening claim survive application restarts. No resumed work after that window requires a manual check; the conversation is not opened indefinitely. Manual switching or stopping continuation cancels these observers. Conditional shutdown waits while goal activation is pending.

Manually handed off active goals receive only one opening followed by the same bounded observation. A second opening could override a user stop when the turn identity stays unchanged, so it is disabled for these intents. A successful account switch remains committed even if an individual continuation fails; QuotaCrew does not send a fallback prompt or restart Desktop because of that failure.

Unsent requests can wait for a connection for up to ten minutes. An uncertain submission is not resent automatically. Pausing or clearing tracking cancels waiting requests; account information continues refreshing. Historical failures found on the first observation are not automatically revived.

If a previous history-size restriction no longer applies, successful revalidation releases that unsent restriction. Submitted or uncertain requests remain protected against duplicate delivery.

## Desktop and IDE support

Desktop and IDE continuation use separate local connections. A failure in one conversation does not redirect it to another client or prevent independent eligible conversations from proceeding.

Local IDE use requires OpenAI's Codex extension. Automatic continuation is experimental and depends on the installed client's connection support. Remote, WSL and cloud conversations are outside this local Windows integration.

**Refresh VS Code after switching** supports one local VS Code window. It requests a normal close, allows up to 30 seconds, then reopens the conversation's workspace and link. Save prompts and link-opening consent are not dismissed. Multiple windows and unknown running-work states prevent automatic refresh. Cursor and Windsurf are not supported by this refresh action.

The extension's cached account cannot be independently verified by QuotaCrew. If an authentication error remains, inspect the account in Codex and sign in again as needed. See [troubleshooting](troubleshooting.md).

## Work tracking

Jobs counts verified running work. Completed conversations are retired after a successful observation, including after account changes. Unknown status is not proof of running or finished work. Very short tasks may occur between checks.

Checks prioritize tracked and active conversations, compare metadata and validated local file revisions, and inspect older conversations in bounded batches. Unavailable conversations have independent retry delays. Paginated histories use bounded turn metadata from Codex; legacy histories also use a bounded local activity read. An obsolete rollout file is not used to cache a paginated history. Observation does not scan entire conversation files. If goal information cannot be read, the previous observation is kept as unverified rather than replaced with an absent goal.

A stored interrupted turn is confirmed against the running Desktop because a separate App Server can report an active turn as interrupted. If that live check fails, the state remains unknown. A previously observed job is also kept as unverified if its turn disappears. An empty response alone does not prove that known work finished.

Native Desktop reads may load conversation history. For legacy histories, QuotaCrew stops those reads and automatic message delivery when a verified local history exceeds 256 MiB. Paginated histories do not depend on the size or existence of an obsolete rollout file. Goal activation is separate: verified lightweight state permits opening the conversation once without sending a message, regardless of history size. If lightweight verification fails, oversized legacy histories remain isolated. Opening can still cause Codex itself to load history; these checks are not a guarantee about Codex's memory use. History is never deleted or modified.

Private Desktop channels can change between Codex versions. Transport parsing is kept in the adapter; unsupported responses stop that action. Conversation failures do not initiate a Desktop restart. Account activation can still require one restart, and a failed activation may require rollback.

Message delivery requires a successful read through Desktop's own channel; lightweight stored observations do not establish delivery readiness. After restart, rejected read operations are retried for up to 20 seconds. An unsent request remains pending for bounded recovery if that wait expires. A generic error after sending is not replayed because delivery may be ambiguous.

Clear tracking removes QuotaCrew observations and pauses automation; it does not delete Codex conversations. The latest continuation details are available from each job's menu.

Continuation labels distinguish a queued request from one already sent. **Check in Codex** means no automatic retry is queued or the last attempt's result could not be verified. Open the conversation in Codex Desktop to check it; changing accounts does not replay an uncertain request for the same interrupted turn. Hover over the status or open **Work details** for the explanation.

## Optional Windows shutdown

Plans require confirmation, apply only to the current application session and can be cancelled from the page or tray.

- **Selected work:** wait for the selected conversation to finish, including continuation turns. If it has a goal, that goal must complete. Errors, pauses and requests for input are not completion.
- **All limits:** require freshly verified exhausted five-hour quotas on every saved account, with no available account to switch to. Reset credits are not remaining five-hour capacity.
- **Timer:** choose 1–1440 minutes. The final two-minute warning is included in that duration; a one-minute timer warns immediately. This mode does not wait for work to finish.

Work and limit modes also wait for other observed Codex work to stop before a two-minute countdown. Checks run every 60 seconds while waiting, every 15 seconds during countdown and immediately before shutdown. Missing evidence resets the countdown.

Internal Codex guardian records are excluded from work checks; real child jobs are checked. A verified empty history is distinct from an unreadable one. Waiting messages identify examples of running or unreadable jobs. New work, uncertain work observations or newly available account capacity reset the applicable conditional countdown; they do not change a timer plan.

Pausing monitoring cancels conditional plans; quitting cancels every plan. QuotaCrew does not force-close applications. Windows can delay shutdown for unsaved work; activity in unrelated programs is not monitored.
