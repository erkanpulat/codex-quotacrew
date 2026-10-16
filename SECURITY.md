# Security policy

Security fixes target the latest development/release version. This is independent community software.

## Reporting

Use the repository's **Security → Report a vulnerability** feature if private reporting is enabled. Otherwise contact the repository maintainer privately before disclosing credentials or exploitation details. Do not post secrets in public issues.

Include the affected version, platform, reproduction steps using synthetic data, and impact. Never attach `auth.json`, profile directories, `switch.recovery.json`, unredacted logs or a database. Current diagnostic exports omit aliases, personal paths and logs; inspect archives from older versions before sharing.

## Storage and guarantees

On Windows, QuotaCrew uses current-user DPAPI for managed profile credentials (`auth.dpapi`) and durable switch recovery snapshots. Encryption is round-trip verified before a plaintext profile is removed; errors never silently fall back to plaintext. The official Codex process uses a temporary restricted `auth.json`; after the process exits, the latest token is sealed. The active shared Codex file remains plaintext for compatibility. SQLite metadata/backups are not encrypted. Non-Windows source installations retain the file-permission model. Token-shaped text and email addresses are redacted from logs, including tracebacks, while diagnostic exports omit raw text entirely.

Recovery snapshots intentionally contain credentials and are excluded from exports and Git. SQLite stores account identifiers, conversation titles/previews, workspace paths and saved local goal notes, but is not the Codex sign-in credential store. Cached text and user-written notes can contain sensitive information and remain unencrypted. Verified account emails are held in memory for optional display; they are omitted from snapshot representations and diagnostic exports.

Passing tests and scanners does not guarantee the absence of every vulnerability.

## Trust boundaries and limitations

QuotaCrew is a local desktop utility operating with the current user's permissions. Its main sensitive operations are copying Codex credentials, changing the shared auth-store setting, starting/stopping the packaged Desktop, and requesting continuation of verified quota-interrupted conversations. Local goal notes do not recreate native goals or reset their budgets.

### Protected boundaries

- Credential writes use randomly named exclusive temporary files, restricted ACLs or Unix `0600`, file flush/fsync and atomic replacement. Temporary files are removed after errors.
- Recovery snapshots include credentials and configuration inside a user-scoped DPAPI envelope on Windows. Existing legacy JSON snapshots are encrypted before recovery attempts. Snapshots are removed after commit or successful rollback; unreadable snapshots are retained without modifying active credentials.
- A per-profile OS lock spans the entire owned CLI process lifetime. Windows job objects terminate assigned children when QuotaCrew dies. A crash can leave a restricted plaintext working file; idle profiles are resealed at the next GUI startup or access. There is a small process-start/job-assignment interval, and a system crash prevents cleanup. These mechanisms do not promise zero plaintext exposure or secure erasure on physical media.
- Old plaintext profiles migrate on startup/access. A failed migration retains its usable original and reports the failure. Pre-DPAPI versions cannot read `auth.dpapi`; downgrading can require signing in again. Windows account/key loss can also require reauthentication. Reauthentication preserves an existing vault until a new login is written.
- A non-blocking OS file lock excludes account health batches from concurrent account mutations. Per-home read locks also prevent overlapping QuotaCrew authentication reads. Only managed inactive profiles permit one Codex-owned refresh after a 401 response; the shared home is never force-refreshed by this client. These locks do not coordinate other applications.
- A non-blocking OS file lock excludes concurrent account mutations. Cancellation triggers rollback; process death leaves a durable recovery snapshot.
- Profile deletion verifies that the resolved directory is a direct child of managed profiles. Shared homes, external directories and redirected paths are rejected.
- Logs redact formatted arguments and exception tracebacks. Stored event payloads and diagnostic text are also redacted. Redaction is a defensive filter, not a proof that arbitrary upstream text can never contain a secret.
- Diagnostic bundles use an allowlist of check names and status values. Profile aliases become sequential numbers. Only a validated CLI version and profile count retain detail; paths, raw errors and logs are omitted along with credentials, databases and conversation files.
- SQLite queries are parameterized. The GUI treats user-supplied account labels as plain text.
- Git ignores credential, recovery and runtime files. CI secret scanning has no blanket exclusion for docs or tests.

## Updates and CLI setup

In direct-download copies, automatic update checks contact the fixed public GitHub repository with the
application version as User-Agent; no accounts, tokens or conversation content
are sent. Users can disable checks. Installers are downloaded only on request,
limited to 512 MiB and verified against release asset size and SHA-256. HTTPS
redirects are restricted to GitHub asset hosts. Digests share the repository's
trust boundary and do not replace code signing. Uncertain native continuation
delivery remains unrelated to updater retries.
MSIX copies use Microsoft Store updates and cannot install GitHub EXE updates.

First-run CLI setup requires a separate affirmative confirmation. It executes
OpenAI's official HTTPS Windows installer in noninteractive mode, preserves an
existing CLI and verifies `codex --version`. The upstream installer controls its
per-user files and PATH change; QuotaCrew does not uninstall or prune that
separate installation. Neither setup nor its tests start model work.

CLI installer redirects are rejected and inherited `CODEX_HOME`/installer
overrides are removed. If shared account verification fails, saved accounts may
still be read, but explicit token refresh is disabled until the active identity
is known. Diagnostic history retains the newest 10,000 events; continuation
delivery claims and recovery snapshots are not pruned with that history.

## Limits

Malware or another process running as the same OS user can invoke DPAPI, inspect memory or read the active shared Codex file. DPAPI protects application-owned secrets at rest from other users; it does not isolate applications running under the same user. Disk encryption and OS account security remain relevant. QuotaCrew cannot coordinate writes made by independently running Codex clients or an older QuotaCrew version that ignores the profile lock; avoid running old and new builds against the same account data. Stop/save active work before switching.

Recovery covers auth/configuration files and Desktop restart attempts. Native goal writes and unrelated client activity are not a distributed transaction. A failed OS launch or unavailable Codex service can require manual recovery; diagnostics report pending snapshots.

Testing uses fake credentials and isolated storage. Release validation must distinguish those tests from a live signed-in Desktop handoff.


Automatic continuation sends model input only after a committed, identity-verified handoff and a structured usage-limit interruption. The worker inherits Codex permissions; it never supplies approval/sandbox overrides or accepts an interactive request. Native goal budgets and explicit pauses are preserved. The attempt journal records identifiers and statuses, not conversation bodies. An ambiguous send is not replayed automatically. Disable continuation in Settings to interrupt owned work; this does not stop unrelated Codex turns.


## Native Desktop delivery

Desktop continuation uses the existing local named pipe; it opens no listener or
debugging port. Before writing, the adapter checks the pipe server process path
against the official packaged Codex publisher and compares its Windows user with
the caller. Only conversation reads and continuation messages are exposed. Frame
sizes, operation time and candidate counts are bounded. Cancellation closes the
pending I/O and handles. Replies and conversation contents are not logged.

The private native protocol has no stable compatibility contract. Readiness retries
only read operations. A durable attempt is recorded before sending; uncertain
replies never cause automatic resend. Native sends do not override model, approval
or sandbox preferences. Idle checks and delivery are not atomic, so a concurrent
user action may result in Desktop queueing the follow-up. Local process compromise
under the same user remains outside this isolation boundary.


The experimental IDE adapter verifies the local router process and exact conversation owner, caps frames at 4 MiB, and closes each connection after the request. Snapshot content is transient and not logged or copied into the tracking database. Owner discovery does not independently verify the IDE's signed-in account; unsupported or uncertain delivery stops for review.

Optional Windows shutdown requires a new explicit confirmation each session. Plans are held in memory, surfaced in the window and tray, cancellable during a 1–1440 minute countdown (two minutes by default), and cleared on exit. The command is a fixed system executable invocation without a shell, `/f` or elevation. Automation does not inspect or save work in unrelated applications. Monitoring is enabled by default on new installations and can be paused; staying in the tray requires a separate opt-in. Clearing tracking pauses monitoring and this app's continuation, invalidates in-flight observations and removes saved observations without deleting profiles or Codex conversations.
