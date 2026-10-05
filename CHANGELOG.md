# Changelog

## 0.2.3 — 2026-10-05

- Show the recorded subscription period beside the plan when matching sign-in metadata is available, with localized dates and a last-checked tooltip; omit expired or mismatched metadata.
- Recognize router discovery replies for conversations that are no longer open, preventing unrelated closed IDE conversations from blocking VS Code refresh after an account switch.
- Preserve account health while another account operation holds the lock and retry UI refreshes when it becomes available.
- Dismiss transient continuation notices after 30 seconds, retain explicit dismissal and distinguish verification delays from quota failures.

## 0.2.2 — 2026-10-03

- Keep direct-download updates separate from packaged Windows installations and add an in-app bilingual privacy policy.
- Protect saved Windows profile credentials and switch-recovery snapshots with user-scoped DPAPI; preserve refreshed credentials, serialize profile access and clean up owned Codex child processes before sealing credentials.
- Redesign onboarding with a four-step sidebar, setup cards, clear preferences and an optional desktop shortcut action shared with Settings.
- Show CLI installation phases, an indeterminate progress indicator and elapsed time; keep retries available after errors and prevent duplicate installations.
- Default new setup to automatic switching when usage is limited, with automatic continuation, experimental IDE continuation and VS Code refresh enabled; preserve saved preferences during updates.
- Clear stale attention banners when the affected conversation resumes, while retaining issues for other conversations.
- Complete Turkish setup descriptions and keep disabled/paused continuation visible in status badges and the tray menu.
- Check optional Desktop and VS Code prerequisites independently in onboarding and Settings. Account switching works without Desktop; disabled IDE conversations never fall back to the Desktop connection.
- Add a separate Desktop continuation preference and scope cancellation/recovery to the selected surface so other CLI/IDE/Desktop work remains independent.
- Exclude Free and unknown plans from automatic/suggested switching while preserving manual activation; reject incomplete or invalid quota figures instead of treating missing usage as zero. Explain automatic-selection exclusions on account rows.
- Redesign automatic shutdown with selectable condition cards, duration presets, a live plan summary and countdown; keep confirmation, session-only plans and page/tray cancellation, with scrollable controls on small screens.

## 0.2.1 — 2026-10-02

- Redesign automatic shutdown with selected-work, all-limits and elapsed-time modes, contextual inputs and conversation selection on the same page.
- Use a fixed two-minute countdown for verified conditions; include the warning in the timer's selected 1–1440 minute duration.
- Keep timers independent of monitoring and connection failures, with cancellation available on the page and in the tray.
- Preserve accounts, application data and existing Desktop/IDE continuation behavior during updates.

## 0.2.0 — 2026-10-01

- Recheck Desktop work inside the account transaction after target verification, before stopping Desktop. Defer a quota handoff if new work starts, monitoring stops or ownership cannot be verified during preparation.
- Limit the IDE continuation stop action to IDE workers; preserve independent Desktop/CLI work and clear disabled IDE recovery requests.
- Include a compact bilingual Windows package guide with working documentation links; retain the illustrated READMEs in the source distribution.
- Rename the application, Windows packages and Python distribution to QuotaCrew for Codex. Preserve the existing account store and Windows identity; replace legacy launchers and shortcuts during upgrades.
- Introduce a shared vector logo, regenerate application screenshots and present the account-to-continuation workflow in both READMEs, with English and Turkish illustrations.

- Add public GitHub support links, bound diagnostic event history, close update backup connections, retry failed update checks and isolate CLI setup from inherited Codex home overrides. Require dependency, static-security and secret checks before publishing.

- Add first-run preferences, an optional repeatable tour and consent-based setup using OpenAI's Windows Codex CLI installer. Preserve existing installations and preferences.
- Check stable GitHub releases automatically; show version availability and release notes in Settings. Download and verify the installer only on request, preserve account data, and restart QuotaCrew after installation.
- Bound update downloads, clean stale download files, retain two update database backups and replace packaged dependency folders instead of accumulating old versions.
- Retry Windows pipe reconnection errors before IDE delivery, preserve pending tickets and capture short Desktop quota turns between polls. Revalidated pending work is displayed as waiting for continuation.

- Fix nested account locking during explicit continuation recovery and defer pending dispatch until account checks finish. Preserve unsent tickets on lock contention.
- Track new IDE turns between polls, without reviving old quota failures; add guarded single-window VS Code close/reopen and exact-thread owner revalidation.

- Check saved profiles independently after a confirmed signed-out response from the shared Codex home, even if its credential file remains. Keep unknown/rejected active identities protected against conflicting refreshes.
- Explain the continuation input, Desktop relay label and VS Code refresh setup in both READMEs; separate live recovery evidence from remaining unattended acceptance checks.
- Retain unsent Desktop/IDE continuation tickets after owner-readiness timeouts; recheck on monitoring polls within the original expiry and clear them when monitoring stops.
- Add confirmed recovery of a selected verified quota failure, with active-capacity and duplicate-delivery checks; distinguish connection waiting from user intervention in the interface.
- Delegate GUI startup to the existing Explorer to escape inherited launcher job lifetimes; verify survival using real isolated Windows processes.
- Defer quota switches while work is running or unverified, persist prepared continuation before shutdown, and revalidate recent pending work after an unexpected exit without replaying uncertain sends.
- Preserve Turkish dotted/dotless I in account status labels instead of applying locale-insensitive capitalization.
- Correct Desktop/IDE channel selection using verified thread metadata; enabling IDE continuation no longer forces Desktop conversations through the IDE owner registry. Complete IDE text-input metadata, surface failed post-send observations and refuse to stop Desktop when it is QuotaCrew's ancestor. The failed live acceptance test and remaining release gates are documented.
- Add a shared animated loading indicator to Overview, Accounts, Jobs, Activity and Diagnostics. Preserve existing rows during refresh, wait for concurrent loads to finish, and clear loading state on failures. Prevent duplicate diagnostic checks and allow retry after failure.
- Recover verified quota failures when the local owner reports `systemError`; rediscover owners after the account handoff restarts Desktop, then pin ownership throughout verification and submission. Process each interrupted conversation independently and persist safe per-conversation continuation results in Jobs.
- Render Overview and My accounts through one account table component, with the same row, header, quota, status and action layout. Keep their page-specific filters and account actions.
- Tighten account row spacing and keep account names next to plan badges. Fit diagnostics and other Qt tables to the available width without treating automatic layout changes as user resizing.
- Use a minimal shared layout for every page, fill the remaining area with account/work/activity tables, align account headers and rows, and elide long account labels with accessible full-text tooltips.
- Open Codex sign-in immediately after adding an account; prevent duplicate additions while signing in and retain the profile for retry.
- Coalesce concurrent account health refreshes, keep failed observations separate from verified running activity, reconcile deleted conversations, and provide a tracking reset that pauses monitoring and invalidates in-flight observations.
- Synchronize Desktop/IDE continuation settings with top-bar badges and tray actions. Display verified running work in both, with a tray icon activity marker and an action to open Jobs.
- Replace the Windows startup button with an On/Off selection. Expand automatic shutdown countdowns to 1–1440 minutes with explicit hour examples and a shared settings row.
- Introduce Jobs with a selectable work list and goal/account details, separate conversation history, an Activity page, shared monitoring controls and keyboard-accessible settings switches.

- Replace account cards with aligned rows, verified quota-window labels, persistent sorting and clear light/dark themes. Surface tracked work in Overview with verified emails and a global privacy toggle.
- Save monitoring preferences and make active checks visible in Overview and the system tray; background operation in the tray requires a separate opt-in.
- Add session-only Windows shutdown after verified quota exhaustion or selected work completion, with a configurable countdown (two minutes by default), bottom notice and tray clock/status/cancel action.
- Add an opt-in experimental local IDE owner transport and optional single-window VS Code refresh. Verify controlled live continuation after editor refresh; document URI consent and remaining combined-cycle limits.
- Reuse the live account snapshot, serialize account checks and limit authentication recovery to one Codex-managed attempt after an inactive profile returns 401.
- Preserve last verified quota information on temporary failures without allowing stale data to trigger a switch. Distinguish unavailable checks from an explicitly rejected sign-in.
- Display only verified email addresses, support hiding them, mask emails in logs and retain the latest credential generation during handoff verification and rollback.
- Keep conversation lists usable in small windows with scrollable content and optional tracking help.
- Check conversation continuation support without starting work; reject unknown sources and verify native Desktop turns before preparing handoffs.
- Open project folders in installed VS Code, Cursor or Windsurf; clarify the limits of IDE integration in English and Turkish.
- Filter accounts by availability, reset credits or required attention. Sort quota columns from their headers without changing the switching policy; show the next eligible candidate independently of display order.
- Show recoverable errors in a persistent banner and successful account switches in a short, non-blocking notification.

## 0.1.1 — 2026-09-28

- Display usage reset credits on each account card. Shows available count and per-credit details (status, scope, granted/expiry times, description) in a plain-text dialog. Credit IDs are never stored or displayed; no credit is consumed by the app. Users are directed to Codex to redeem credits.
- Updated account overview text, English/Turkish README and synthetic screenshots for clarity.
- Keep installer and application versions aligned with an automated regression check.

## 0.1.0 — 2026-09-28

- Windows desktop application and CLI for managing multiple OpenAI Codex accounts.
- Quota monitoring with automatic switching, confirmation and manual modes; configurable 30–3600 second checks.
- Local conversation browser with project/source filters, search, resizable columns and full-path details.
- Turkish and English interface, dark/light themes, system tray, readable account/goal dialogs and optional Windows startup.
- Local goal notes and read-only native goal inspection. Verified usage-limit interruptions can initiate guarded automatic continuation after a committed handoff; approval/input requests still stop for the user.
- Identity verification, protected credential writes, account operation locks and recovery from interrupted switches. Current packaged Codex Desktop process detection, failed-switch retry backoff, and separate reporting when a conversation cannot load after a verified switch.
- Automated tests, dependency/secret scanning, Windows/Linux CI and Windows packaging tools.
