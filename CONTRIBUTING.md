# Contributing to QuotaCrew

## Setup and checks

Use Python 3.11–3.13. Live integration requires Codex CLI; automated tests use isolated fakes.

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[gui,dev]"
.venv\Scripts\python -m ruff check src tests scripts
.venv\Scripts\python -m ruff format --check src tests scripts
.venv\Scripts\python -m mypy src
.venv\Scripts\python -m pytest -q
```

CI also checks dependencies, secrets and Windows packages. Use the [Windows acceptance checklist](docs/manual-testing.tr.md) for live integration; record live results separately from automated tests.

## Code organization

| Location | Responsibility |
| --- | --- |
| `accounts`, `auth` | Profiles, sign-in, identity verification and transactional switching. |
| `adapters` | Codex protocols, credential storage and application lifecycle. |
| `continuity`, `goals`, `monitoring` | Work tracking, continuation, goals and scheduled checks. |
| `storage`, `core` | Database migrations, paths, locks, events and redaction. |
| `gui`, `cli` | User interfaces backed by the shared services. |
| `tests`, `scripts`, `packaging` | Regression tests, development tools and Windows builds. |

Keep business logic in services and external communication in adapters. Qt runs on the main thread; use `AsyncRunner` for asynchronous work. Reuse `account_table.py`, `view_base.py` and `widgets.py` instead of duplicating controls.

## Data and security

- Never include real credentials or private conversation data in code, tests, logs or reports.
- Preserve the shared Codex history in `~/.codex`; profile storage is for account credentials and configuration.
- Keep account switching serialized, identity-checked and recoverable. Use atomic writes and parameterized SQL.
- Require verified quota and destination capacity for automatic switching. Preserve user choices, approvals and goal budgets.
- Treat unknown protocol responses as unknown; do not infer permission or successful delivery.

See the [security policy](SECURITY.md) and [continuation behavior](docs/continuity.md).

## Interface changes

Add English/Turkish text through `gui/i18n.py` and use the shared theme and icons. Check both themes, small windows, long labels and empty states. `scripts/render_preview.py` produces screenshots with synthetic accounts; documentation images must not expose real user data.

## Pull requests

Keep changes focused. Explain the problem, resulting behavior, tests and material limitations. Add regression coverage for changed behavior. Follow the [packaging guide](packaging/README.md) for Windows releases.

Contributions are licensed under the project's MIT license.
