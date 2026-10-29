# Changelog

## 0.4.0 — 2026-10-29

- Distinguish users sharing a workspace when binding profiles, displaying quotas and identifying the active account.
- Verify account ownership before credential writes, switching and conversation continuation.
- Preserve legacy records without guessing their owner and invalidate shutdown evidence when account ownership changes.
- Support older Codex identity responses and negotiate the capabilities required by newer session APIs.
- Retry account refresh after account operations instead of treating a temporary lock as stale account information.
- Keep failed automatic-switch targets on separate retry delays so other available accounts can still be selected.
- Distinguish unavailable identity checks from account mismatches and show sanitized switch failure details in Activity.

## 0.3.1 — 2026-10-23

- Verify Desktop activity independently of persisted interrupted turns.
- Preserve eligible active goals across manual account changes without duplicate messages.
- Observe goal activation independently with bounded retries and per-conversation failures.
- Keep shutdown checks aware of pending goal activation and distinguish blocked, paused, changed and unverified goals.
- Clarify continuation and shutdown messages without treating unknown results as failures.

## 0.3.0 — 2026-10-19

- Separate account activation from individual continuation failures and reserve profile access through credential verification to prevent background-reader collisions.
- Open eligible Desktop goal conversations once without an extra message; observe goals that resume themselves and retain at-most-once continuation records.
- Remove Desktop restart recovery from the continuation queue. A failed conversation waits or requires attention without restarting successful work.
- Prioritize active and changed conversations, bound background checks, and retry unavailable conversations independently. Avoid loading oversized histories automatically.
- Clarify shutdown blockers, cache verified unchanged idle histories, and check pending continuations before shutdown after selected work.
- Aggregate observation errors without recording conversation contents.
- Wait for the Desktop message channel after account switching; keep unsent requests pending without replaying ambiguous deliveries.

## 0.2.7 — 2026-10-18

- Isolate unavailable conversations so one job does not block eligible account switches or independent continuations.
- Preserve unsent continuation requests across application exits and check Desktop readiness before sending.
- Improve work discovery, manual-switch tracking and conditional shutdown checks.
- Handle Windows shell launch failures without terminating startup.
- Distinguish queued, submitted and unverified continuation requests in Jobs and Overview; explain when a conversation needs manual attention.

## 0.2.6 — 2026-10-16

- Support the current Codex Desktop app-tools channel during conversation tracking and continuation.

## 0.2.5 — 2026-10-14

- Keep rejected sign-ins out of automatic selection until their saved credentials change.
- Recover from an expired active sign-in while retaining conversation restart safety checks.
- Queue manual account switches behind active checks and release locks after cancellation cleanup.
- Preserve typed busy errors for automatic UI retries; translate and expire transient error notices.

## 0.2.4 — 2026-10-06

- Improve account-switch authentication and handling of expired sessions.
- Refresh account information while automatic switching is paused.
- Standardize account tables, add subscription-date sorting and remove loading layout jumps.
- Fix stale work tracking, account diagnostics and conditional shutdown.

## 0.2.3 — 2026-10-05

- Show subscription-period dates when available.
- Improve VS Code refresh and account-status recovery.
- Clear outdated continuation warnings.

## 0.2.2 — 2026-10-03

- Redesign setup and shutdown screens; clarify CLI installation progress.
- Improve credential protection and independent Desktop/IDE controls.
- Exclude Free and unknown plans from automatic account selection.
- Improve Turkish text, shortcuts and default preferences.

## 0.2.1 — 2026-10-02

- Add shutdown after selected work, exhausted quotas or a timer.
- Add confirmation, countdown and cancellation controls.

## 0.2.0 — 2026-10-01

- Rename the application to QuotaCrew and refresh its interface.
- Add guided setup and in-app updates.
- Add Jobs, Activity and experimental VS Code continuation.
- Improve account switching, recovery, work tracking and privacy controls.

## 0.1.1 — 2026-09-28

- Display available usage-reset credits.
- Update account information and installation checks.

## 0.1.0 — 2026-09-28

- Initial Windows desktop and CLI release.
- Add account management, quota monitoring and local conversation browsing.
- Add Turkish/English, themes, tray controls and account-switch recovery.
