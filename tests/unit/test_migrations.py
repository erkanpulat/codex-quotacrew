import sqlite3

from codex_account_manager.storage.migrations import TARGET_VERSION, migrate


async def test_migrate_creates_all_tables(tmp_paths):
    version = await migrate()
    assert version == TARGET_VERSION
    con = sqlite3.connect(tmp_paths.db_path)
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {
        "profiles",
        "profile_accounts",
        "settings",
        "threads",
        "goals",
        "handoffs",
        "events",
    } <= tables


async def test_migrate_is_idempotent(tmp_paths):
    await migrate()
    # Second run must not error and must keep the version.
    assert await migrate() == TARGET_VERSION


async def test_observed_work_migration_accepts_earlier_local_schema(tmp_paths):
    await migrate()
    with sqlite3.connect(tmp_paths.db_path) as con:
        con.execute("DROP TABLE pending_continuations")
        con.execute("DROP TABLE observed_work")
        con.execute(
            """CREATE TABLE observed_work (
                thread_id TEXT PRIMARY KEY, turn_id TEXT NOT NULL,
                turn_status TEXT NOT NULL, goal_json TEXT NOT NULL,
                limited INTEGER NOT NULL, observed_at REAL NOT NULL
            )"""
        )
        con.execute(
            "INSERT INTO observed_work VALUES ('old', 'turn', 'inProgress', '{\"present\": false}', 0, 1)"
        )
        con.execute("UPDATE schema_meta SET value='3' WHERE key='schema_version'")
    assert await migrate() == TARGET_VERSION
    with sqlite3.connect(tmp_paths.db_path) as con:
        columns = {row[1] for row in con.execute("PRAGMA table_info(observed_work)")}
        assert {"account_hash", "goal_fingerprint", "goal_status"} <= columns
        assert "goal_json" not in columns
        assert con.execute(
            "SELECT account_hash, goal_fingerprint FROM observed_work WHERE thread_id='old'"
        ).fetchone() == ("", '{"present": false}')
    from codex_account_manager.adapters.interfaces import GoalInfo
    from codex_account_manager.continuity.tracking import WorkTracker

    tracker = WorkTracker()
    await tracker.observe(
        "account", "new", {"id": "turn", "status": "inProgress"}, GoalInfo("new", None, None, False)
    )
    assert await tracker.ids("account") == ["new"]


async def test_migrate_preserves_existing_data(tmp_paths):
    await migrate()
    con = sqlite3.connect(tmp_paths.db_path)
    con.execute(
        "INSERT INTO profiles (id, alias, codex_home, created_at) VALUES ('p1','ana','home','2020-01-01')"
    )
    con.commit()
    con.close()

    await migrate()  # re-run

    con = sqlite3.connect(tmp_paths.db_path)
    count = con.execute("SELECT COUNT(*) FROM profiles").fetchone()[0]
    assert count == 1


async def test_migrate_repairs_missing_settings_table(tmp_paths):
    """A recorded schema version must not hide a missing settings table."""
    con = sqlite3.connect(tmp_paths.db_path)
    con.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    con.execute("INSERT INTO schema_meta VALUES ('schema_version','1')")
    con.execute(
        "CREATE TABLE profiles (id TEXT PRIMARY KEY, alias TEXT UNIQUE, codex_home TEXT UNIQUE, created_at TEXT)"
    )
    con.execute("INSERT INTO profiles VALUES ('p1','ana','home','2020')")
    con.commit()
    con.close()

    assert await migrate() == TARGET_VERSION
    con = sqlite3.connect(tmp_paths.db_path)
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "settings" in tables
    assert con.execute("SELECT COUNT(*) FROM profiles").fetchone()[0] == 1


async def test_forward_migration_preserves_data_and_releases_backup(tmp_paths, monkeypatch):
    import codex_account_manager.storage.migrations as module

    await migrate()
    with sqlite3.connect(tmp_paths.db_path) as con:
        con.execute("INSERT INTO profiles VALUES ('p1','work','home','2026-01-01')")
    con.close()

    async def next_schema(db):
        await db.execute("ALTER TABLE profiles ADD COLUMN test_metadata TEXT")

    monkeypatch.setattr(
        module,
        "MIGRATIONS",
        [*module.MIGRATIONS, module.Migration(TARGET_VERSION + 1, "test schema", next_schema)],
    )
    monkeypatch.setattr(module, "TARGET_VERSION", TARGET_VERSION + 1)
    assert await module.migrate() == TARGET_VERSION + 1
    assert await module.migrate() == TARGET_VERSION + 1
    with sqlite3.connect(tmp_paths.db_path) as con:
        assert con.execute("SELECT alias, test_metadata FROM profiles").fetchone() == ("work", None)
    con.close()
    backups = list(tmp_paths.backups_dir.glob("*.db.bak"))
    assert backups
    for backup in backups:
        backup.unlink()


async def test_failed_migration_rolls_back_schema_and_version(tmp_paths, monkeypatch):
    import pytest

    import codex_account_manager.storage.migrations as module

    await migrate()

    async def broken_schema(db):
        await db.execute("CREATE TABLE temporary_change (value TEXT)")
        raise RuntimeError("Simulated migration failure")

    monkeypatch.setattr(
        module,
        "MIGRATIONS",
        [*module.MIGRATIONS, module.Migration(TARGET_VERSION + 1, "failing schema", broken_schema)],
    )
    monkeypatch.setattr(module, "TARGET_VERSION", TARGET_VERSION + 1)
    with pytest.raises(RuntimeError, match="Simulated"):
        await module.migrate()
    with sqlite3.connect(tmp_paths.db_path) as con:
        assert con.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[
            0
        ] == str(TARGET_VERSION)
        assert (
            con.execute("SELECT name FROM sqlite_master WHERE name='temporary_change'").fetchone()
            is None
        )
    con.close()


async def test_repair_missing_goals_uses_current_columns(tmp_paths):
    await migrate()
    with sqlite3.connect(tmp_paths.db_path) as con:
        con.execute("DROP TABLE goals")
    con.close()
    assert await migrate() == TARGET_VERSION
    with sqlite3.connect(tmp_paths.db_path) as con:
        columns = {row[1] for row in con.execute("PRAGMA table_info(goals)")}
        assert "local_status" in columns
    con.close()


async def test_invalid_and_future_versions_are_not_overwritten(tmp_paths):
    import pytest

    await migrate()
    for value in ("invalid", "999"):
        with sqlite3.connect(tmp_paths.db_path) as con:
            con.execute("UPDATE schema_meta SET value=? WHERE key='schema_version'", (value,))
        con.close()
        with pytest.raises(RuntimeError):
            await migrate()
        with sqlite3.connect(tmp_paths.db_path) as con:
            assert (
                con.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[
                    0
                ]
                == value
            )
        con.close()


async def test_missing_database_with_signins_is_not_recreated(tmp_paths):
    import pytest

    from codex_account_manager.core.errors import AccountRecoveryRequired

    auth = tmp_paths.profiles_dir / "saved" / "auth.json"
    auth.parent.mkdir()
    auth.write_bytes(b"synthetic-login")
    with pytest.raises(AccountRecoveryRequired):
        await migrate()
    assert not tmp_paths.db_path.exists()
    assert auth.read_bytes() == b"synthetic-login"


async def test_empty_catalogue_with_signins_is_not_migrated(tmp_paths):
    import pytest

    from codex_account_manager.core.errors import AccountRecoveryRequired

    await migrate()
    auth = tmp_paths.profiles_dir / "saved" / "auth.json"
    auth.parent.mkdir()
    auth.write_bytes(b"synthetic-login")
    with sqlite3.connect(tmp_paths.db_path) as db:
        db.execute("UPDATE schema_meta SET value='1'")
    db.close()
    before = tmp_paths.db_path.read_bytes()
    with pytest.raises(AccountRecoveryRequired):
        await migrate()
    assert tmp_paths.db_path.read_bytes() == before
    assert not list(tmp_paths.backups_dir.glob("*.db.bak"))


async def test_existing_accounts_and_signins_survive_startup(tmp_paths):
    await migrate()
    with sqlite3.connect(tmp_paths.db_path) as db:
        db.execute("INSERT INTO profiles VALUES ('saved','work','home','2026-01-01')")
    db.close()
    auth = tmp_paths.profiles_dir / "saved" / "auth.json"
    auth.parent.mkdir()
    auth.write_bytes(b"synthetic-login")
    assert await migrate() == TARGET_VERSION
    assert auth.read_bytes() == b"synthetic-login"


async def test_pending_quota_intent_survives_manual_goal_migration(tmp_paths):
    await migrate()
    with sqlite3.connect(tmp_paths.db_path) as db:
        db.execute("ALTER TABLE pending_continuations DROP COLUMN resume_active_goal")
        db.execute("UPDATE schema_meta SET value='8'")
        db.execute(
            "INSERT INTO pending_continuations VALUES ('thread','turn','account','goal',1,NULL,100)"
        )
    assert await migrate() == TARGET_VERSION
    with sqlite3.connect(tmp_paths.db_path) as db:
        assert db.execute("SELECT * FROM pending_continuations").fetchall() == [
            ("thread", "turn", "account", "goal", 1, None, 100.0, 0)
        ]
