"""Versioned SQLite migrations.

Migrations are ordered, forward-only steps applied inside a transaction. The
current version is tracked in ``schema_meta.schema_version``. Migrations
preserve account and goal data. Obsolete observation columns may be normalized
through a temporary table. A pre-migration backup is taken automatically.

To add a migration, append a ``Migration`` to :data:`MIGRATIONS` with the next
integer version. Never edit an already-shipped migration.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Awaitable, Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime

import aiosqlite

from codex_account_manager.core.errors import AccountRecoveryRequired
from codex_account_manager.core.files import restrict_access
from codex_account_manager.core.logging import get_logger
from codex_account_manager.core.paths import paths

log = get_logger(__name__)

MigrationFn = Callable[[aiosqlite.Connection], Awaitable[None]]


@dataclass(frozen=True)
class Migration:
    version: int
    description: str
    apply: MigrationFn


async def _create_profiles(db: aiosqlite.Connection) -> None:
    """Account identity and managed profile schema."""
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS profiles (
            id TEXT PRIMARY KEY,
            alias TEXT UNIQUE NOT NULL,
            codex_home TEXT UNIQUE NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS profile_accounts (
            profile_id TEXT PRIMARY KEY,
            account_id TEXT UNIQUE NOT NULL,
            bound_at TEXT NOT NULL,
            FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE CASCADE
        )
        """
    )


async def _create_settings(db: aiosqlite.Connection) -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )


async def _create_conversations(db: aiosqlite.Connection) -> None:
    """Threads, goals, handoffs and diagnostic event history."""
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS threads (
            id TEXT PRIMARY KEY,
            profile_id TEXT,
            workspace TEXT,
            cwd TEXT,
            preview TEXT,
            last_turn_id TEXT,
            title TEXT,
            source TEXT,
            project_id TEXT,
            model_provider TEXT,
            is_listed INTEGER NOT NULL DEFAULT 1,
            last_seen_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS goals (
            id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL,
            objective TEXT NOT NULL,
            local_status TEXT NOT NULL,
            native_goal_status TEXT,
            native_goal_present INTEGER,
            workspace TEXT,
            profile_id TEXT,
            revision INTEGER NOT NULL DEFAULT 1,
            user_cleared INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            last_handoff_at TEXT
        )
        """
    )
    await db.execute("CREATE INDEX IF NOT EXISTS idx_goals_thread ON goals(thread_id)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_threads_updated ON threads(updated_at DESC)")
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS handoffs (
            id TEXT PRIMARY KEY,
            thread_id TEXT,
            from_profile_id TEXT,
            to_profile_id TEXT,
            reason TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            success INTEGER,
            rolled_back INTEGER NOT NULL DEFAULT 0,
            final_stage TEXT,
            detail TEXT
        )
        """
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            topic TEXT NOT NULL,
            thread_id TEXT,
            profile_id TEXT,
            payload TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    await db.execute("CREATE INDEX IF NOT EXISTS idx_events_topic ON events(topic)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_events_thread ON events(thread_id)")


async def _initial_schema(db: aiosqlite.Connection) -> None:
    await _create_profiles(db)
    await _create_settings(db)
    await _create_conversations(db)


async def _create_continuation_attempts(db: aiosqlite.Connection) -> None:
    await db.execute("""
        CREATE TABLE IF NOT EXISTS continuation_attempts (
            thread_id TEXT NOT NULL,
            source_turn_id TEXT NOT NULL,
            message_id TEXT NOT NULL,
            status TEXT NOT NULL,
            PRIMARY KEY (thread_id, source_turn_id)
        )
    """)


async def _create_observed_work(db: aiosqlite.Connection) -> None:
    await db.execute("""
        CREATE TABLE IF NOT EXISTS observed_work (
            thread_id TEXT PRIMARY KEY,
            account_hash TEXT NOT NULL,
            turn_id TEXT NOT NULL,
            turn_status TEXT NOT NULL,
            goal_fingerprint TEXT NOT NULL,
            goal_status TEXT,
            limited INTEGER NOT NULL,
            observed_at REAL NOT NULL
        )
    """)


async def _complete_observed_work(db: aiosqlite.Connection) -> None:
    columns = {
        row[1] for row in await (await db.execute("PRAGMA table_info(observed_work)")).fetchall()
    }
    if "account_hash" not in columns:
        await db.execute(
            "ALTER TABLE observed_work ADD COLUMN account_hash TEXT NOT NULL DEFAULT ''"
        )
    if "goal_fingerprint" not in columns:
        await db.execute("ALTER TABLE observed_work ADD COLUMN goal_fingerprint TEXT")
        if "goal_json" in columns:
            await db.execute("UPDATE observed_work SET goal_fingerprint=goal_json")
    if "goal_status" not in columns:
        await db.execute("ALTER TABLE observed_work ADD COLUMN goal_status TEXT")


async def _normalize_observed_work(db: aiosqlite.Connection) -> None:
    columns = {
        row[1] for row in await (await db.execute("PRAGMA table_info(observed_work)")).fetchall()
    }
    if "goal_json" not in columns:
        return
    # The obsolete NOT NULL column prevents new observations from being inserted.
    await db.execute("ALTER TABLE observed_work RENAME TO observed_work_legacy")
    await _create_observed_work(db)
    await db.execute("""
        INSERT INTO observed_work
        SELECT thread_id, account_hash, turn_id, turn_status,
            CASE WHEN account_hash = '' THEN '{"present": false}'
            ELSE COALESCE(goal_fingerprint, '{"present": false}') END,
            goal_status, limited, observed_at
        FROM observed_work_legacy
    """)
    await db.execute("DROP TABLE observed_work_legacy")


async def _work_verification(db: aiosqlite.Connection) -> None:
    await db.execute("ALTER TABLE observed_work ADD COLUMN verified INTEGER NOT NULL DEFAULT 0")


async def _pending_continuations(db: aiosqlite.Connection) -> None:
    await db.execute("""
        CREATE TABLE pending_continuations (
            thread_id TEXT NOT NULL,
            turn_id TEXT NOT NULL,
            account_id TEXT NOT NULL,
            goal_signature TEXT,
            desktop INTEGER NOT NULL,
            owner_id TEXT,
            created_at REAL NOT NULL,
            PRIMARY KEY (thread_id, turn_id)
        )
    """)


async def _unavailable_work_state(db: aiosqlite.Connection) -> None:
    columns = {
        row[1] for row in await (await db.execute("PRAGMA table_info(observed_work)")).fetchall()
    }
    if "unavailable_reason" not in columns:
        await db.execute("ALTER TABLE observed_work ADD COLUMN unavailable_reason TEXT")
    if "unavailable_until" not in columns:
        await db.execute("ALTER TABLE observed_work ADD COLUMN unavailable_until REAL")
    await db.execute(
        """CREATE TABLE IF NOT EXISTS conversation_cooldowns (
            thread_id TEXT PRIMARY KEY,
            reason TEXT NOT NULL,
            retry_after REAL NOT NULL,
            updated_at REAL NOT NULL
        )"""
    )


async def _manual_goal_intent(db: aiosqlite.Connection) -> None:
    await db.execute(
        "ALTER TABLE pending_continuations ADD COLUMN resume_active_goal INTEGER NOT NULL DEFAULT 0"
    )


MIGRATIONS: list[Migration] = [
    Migration(1, "initial application schema", _initial_schema),
    Migration(2, "durable automatic continuation attempts", _create_continuation_attempts),
    Migration(3, "observed conversation checkpoints", _create_observed_work),
    Migration(4, "complete observed work metadata", _complete_observed_work),
    Migration(5, "normalize observed work schema", _normalize_observed_work),
    Migration(6, "separate saved observations from verified activity", _work_verification),
    Migration(7, "preserve prepared continuation across process exits", _pending_continuations),
    Migration(8, "remember conversations awaiting revalidation", _unavailable_work_state),
    Migration(9, "preserve manually handed off active goals", _manual_goal_intent),
]

TARGET_VERSION = MIGRATIONS[-1].version


async def _current_version(db: aiosqlite.Connection) -> int:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    cursor = await db.execute("SELECT value FROM schema_meta WHERE key='schema_version'")
    row = await cursor.fetchone()
    if not row:
        return 0
    try:
        return int(row[0])
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Invalid database schema version; restore a verified backup.") from exc


def _backup_db() -> None:
    if not paths.db_path.exists():
        return
    paths.backups_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
    target = paths.backups_dir / f"accounts-{stamp}.db.bak"
    with (
        closing(sqlite3.connect(paths.db_path)) as source,
        closing(sqlite3.connect(target)) as backup,
    ):
        restrict_access(target)
        source.backup(backup)
    log.info("Pre-migration DB backup written to %s", target.name)


async def _verify_account_catalogue() -> None:
    if not any(
        next(paths.profiles_dir.glob(pattern), None) for pattern in ("*/auth.json", "*/auth.dpapi")
    ):
        return
    if paths.db_path.is_file():
        async with aiosqlite.connect(paths.db_path.as_uri() + "?mode=ro", uri=True) as db:
            table = await (
                await db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='profiles'"
                )
            ).fetchone()
            if table and await (await db.execute("SELECT 1 FROM profiles LIMIT 1")).fetchone():
                return
    raise AccountRecoveryRequired(
        "Saved sign-in files exist, but the account list is missing. "
        "Startup was stopped to protect your data. Do not reset or delete the data folder. "
        "Restore the account database from a verified backup."
    )


async def migrate() -> int:
    """Apply all pending migrations. Returns the resulting schema version.

    Existing data is preserved; a backup is taken before any change is applied.
    """
    await _verify_account_catalogue()
    paths.ensure()
    async with aiosqlite.connect(paths.db_path) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("PRAGMA foreign_keys=ON;")
        await db.execute("PRAGMA busy_timeout=5000;")

        current = await _current_version(db)
        if current > TARGET_VERSION:
            raise RuntimeError("Database was created by a newer version; upgrade QuotaCrew.")

        cursor = await db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        table_names = {row[0] for row in await cursor.fetchall()}
        required = {
            "profiles",
            "profile_accounts",
            "settings",
            "threads",
            "goals",
            "handoffs",
            "events",
        }
        pending = [m for m in MIGRATIONS if m.version > current]
        if current >= 1 and not required <= table_names:
            pending.insert(0, MIGRATIONS[0])
        if not pending:
            return current

        if current or table_names - {"schema_meta"}:
            _backup_db()

        await db.execute("BEGIN IMMEDIATE")
        for migration in pending:
            log.info("Applying migration %s: %s", migration.version, migration.description)
            await migration.apply(db)

        # Record the highest known schema version once all steps are applied.
        await db.execute(
            """
            INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (str(TARGET_VERSION),),
        )
        await db.commit()
        return TARGET_VERSION
