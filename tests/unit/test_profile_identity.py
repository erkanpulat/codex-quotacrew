import sqlite3

import pytest

from codex_account_manager.accounts.service import AccountService
from codex_account_manager.domain.identity import AccountIdentity, IdentityState
from codex_account_manager.domain.models import Profile
from codex_account_manager.storage.repositories import ProfileRepository
from tests.fakes import FakeAppServer


def owner(user="U1", workspace="W1"):
    return AccountIdentity(user_id=user, workspace_id=workspace, state=IdentityState.COMPLETE)


async def test_team_binding_health_and_active_alias_use_user_and_workspace(migrated_db, qt_app):
    servers = {}
    shared = str(migrated_db.shared_codex_home)
    service = AccountService(app_server_factory=lambda home: servers[home])
    servers[shared] = FakeAppServer(account_id="W1", identity=owner("U2"), plan="team", primary=25)
    for index in range(1, 5):
        profile = await service.create_profile(f"person-{index}")
        servers[profile.codex_home] = FakeAppServer(
            account_id="W1", identity=owner(f"U{index}"), plan="team", primary=index * 10
        )
        service.credentials.profile_auth_path(profile.codex_home).write_bytes(b"synthetic")
        assert await service.bind_current_account(profile.alias) == "W1"
    health = await service.all_health()
    assert [row.alias for row in health if row.is_active] == ["person-2"]
    assert [row.primary_used_percent for row in health] == [10, 25, 30, 40]
    assert all(row.account_match is True for row in health)
    assert await service.resolve_active_alias() == "person-2"
    from codex_account_manager.gui.account_table import AccountTable

    table = AccountTable()
    try:
        table.set_profiles(health, email_visible=False)
        assert set(table.rows) == {f"person-{index}" for index in range(1, 5)}
        assert [alias for alias, row in table.rows.items() if row.property("active") == "true"] == [
            "person-2"
        ]
    finally:
        table.close()
        table.deleteLater()
        qt_app.processEvents()


@pytest.mark.parametrize(
    "identity",
    [
        None,
        AccountIdentity(workspace_id="W1", state=IdentityState.PARTIAL),
        AccountIdentity(state=IdentityState.CONFLICT),
    ],
)
async def test_bind_never_guesses_missing_or_conflicting_user(migrated_db, identity):
    from codex_account_manager.core.errors import AccountMismatchError

    service = AccountService(
        app_server_factory=lambda home: FakeAppServer(account_id="W1", identity=identity)
    )
    profile = await service.create_profile("unknown")
    with pytest.raises(AccountMismatchError, match="user and workspace"):
        await service.bind_current_account(profile.alias)
    saved = await service.profiles.get_by_alias(profile.alias)
    assert saved.bound_account_id is None and saved.bound_user_id is None


async def test_wrong_user_never_inherits_health_or_cached_quota(migrated_db):
    from codex_account_manager.continuity.policy import SwitchPolicy
    from codex_account_manager.core.errors import AppServerError

    server = FakeAppServer(account_id="W1", identity=owner())
    service = AccountService(app_server_factory=lambda home: server)
    profile = await service.create_profile("known")
    await service.bind_current_account(profile.alias)
    profile = await service.profiles.get_by_alias(profile.alias)
    service.credentials.profile_auth_path(profile.codex_home).write_bytes(b"synthetic")
    first = await service._health_for(profile, active_identity=owner())
    assert SwitchPolicy._is_available(first)
    server.identity = owner("U2")
    wrong = await service._health_for(profile, active_identity=owner("U2"))
    assert wrong.account_match is False and not wrong.is_active
    assert wrong.email is None and wrong.ordinary_usage_allowed is None
    assert wrong.plan_type is None
    assert wrong.primary_used_percent is None and wrong.secondary_used_percent is None
    assert wrong.primary_resets_at is None and wrong.secondary_resets_at is None
    assert not SwitchPolicy._is_available(wrong)
    failed = service._failed_health(profile, AppServerError("offline"))
    assert not failed.stale and failed.primary_used_percent is None


@pytest.mark.parametrize(
    "plan,upgraded",
    [
        ("plus", True),
        ("pro", True),
        ("free", True),
        ("team", False),
        ("business", False),
        ("enterprise", False),
        ("future", False),
    ],
)
async def test_only_single_personal_legacy_binding_is_enriched(migrated_db, plan, upgraded):
    service = AccountService(
        app_server_factory=lambda home: FakeAppServer(account_id="W1", identity=owner(), plan=plan)
    )
    profile = await service.create_profile("legacy")
    await service.profiles.bind_account(profile.id, "W1")
    health = await service.health(profile.alias)
    saved = await service.profiles.get_by_alias(profile.alias)
    assert bool(saved.bound_user_id) is upgraded
    assert (health.account_match is True) is upgraded
    if not upgraded:
        assert health.error and not health.is_active


async def test_legacy_binding_is_not_enriched_when_workspace_has_another_profile(migrated_db):
    service = AccountService(
        app_server_factory=lambda home: FakeAppServer(account_id="W1", identity=owner())
    )
    legacy = await service.create_profile("legacy")
    other = await service.create_profile("other")
    await service.profiles.bind_account(legacy.id, "W1")
    await service.profiles.bind_account(other.id, "W1", user_id="U2")
    health = await service.health(legacy.alias)
    assert health.account_match is None and health.error
    assert (await service.profiles.get_by_alias(legacy.alias)).bound_user_id is None


async def schema_nine(tmp_paths, monkeypatch):
    import codex_account_manager.storage.migrations as module

    with monkeypatch.context() as context:
        context.setattr(module, "MIGRATIONS", module.MIGRATIONS[:9])
        context.setattr(module, "TARGET_VERSION", 9)
        await module.migrate()
    with sqlite3.connect(tmp_paths.db_path) as db:
        db.execute("INSERT INTO profiles VALUES ('P1','legacy','home','2026-01-01')")
        db.execute("INSERT INTO profile_accounts VALUES ('P1','W1','2026-01-02')")
        db.execute("INSERT INTO settings VALUES ('preference','keep','2026-01-02')")
        db.execute("INSERT INTO pending_continuations VALUES ('T1','turn','W1',NULL,1,NULL,123,0)")
        db.execute("INSERT INTO continuation_attempts VALUES ('T1','turn','message','uncertain')")


async def test_upgrade_preserves_legacy_binding_delivery_and_settings(tmp_paths, monkeypatch):
    from codex_account_manager.storage.migrations import migrate

    await schema_nine(tmp_paths, monkeypatch)
    assert await migrate() == 10
    assert await migrate() == 10
    with sqlite3.connect(tmp_paths.db_path) as db:
        assert db.execute("SELECT * FROM profile_accounts").fetchall() == [
            ("P1", "W1", "2026-01-02", None)
        ]
        assert db.execute("SELECT * FROM profiles").fetchall() == [
            ("P1", "legacy", "home", "2026-01-01")
        ]
        assert db.execute("SELECT value FROM settings WHERE key='preference'").fetchone() == (
            "keep",
        )
        assert db.execute("SELECT * FROM pending_continuations").fetchall() == [
            ("T1", "turn", "W1", None, 1, None, 123, 0, None)
        ]
        assert db.execute("SELECT * FROM continuation_attempts").fetchall() == [
            ("T1", "turn", "message", "uncertain")
        ]
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
    backups = list(tmp_paths.backups_dir.glob("*.db.bak"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as db:
        assert db.execute("SELECT value FROM schema_meta").fetchone() == ("9",)
        assert db.execute("SELECT * FROM profile_accounts").fetchall() == [
            ("P1", "W1", "2026-01-02")
        ]


async def test_failed_identity_migration_preserves_original_table(tmp_paths, monkeypatch):
    import codex_account_manager.storage.migrations as module

    await schema_nine(tmp_paths, monkeypatch)
    original = module.MIGRATIONS[-1]

    async def fail_after_rebuild(db):
        await original.apply(db)
        raise RuntimeError("simulated failure")

    with monkeypatch.context() as context:
        context.setattr(
            module,
            "MIGRATIONS",
            [*module.MIGRATIONS[:-1], module.Migration(10, "test", fail_after_rebuild)],
        )
        with pytest.raises(RuntimeError, match="simulated"):
            await module.migrate()
    with sqlite3.connect(tmp_paths.db_path) as db:
        assert db.execute("SELECT value FROM schema_meta").fetchone() == ("9",)
        assert db.execute("SELECT * FROM profile_accounts").fetchall() == [
            ("P1", "W1", "2026-01-02")
        ]
        assert not db.execute(
            "SELECT name FROM sqlite_master WHERE name='profile_accounts_identity'"
        ).fetchall()
    assert await module.migrate() == 10


async def test_four_team_users_bind_independently_and_duplicate_owner_is_rejected(migrated_db):
    repository = ProfileRepository()
    for index in range(4):
        profile = Profile(id=f"P{index}", alias=f"person-{index}", codex_home=f"home-{index}")
        await repository.create(profile)
        await repository.bind_account(profile.id, "W1", user_id=f"U{index}")
    profiles = await repository.list()
    assert [profile.bound_user_id for profile in profiles] == ["U0", "U1", "U2", "U3"]
    assert {profile.bound_account_id for profile in profiles} == {"W1"}
    assert len({profile.identity.owner_key for profile in profiles}) == 4
    with pytest.raises(sqlite3.IntegrityError):
        await repository.bind_account("P3", "W1", user_id="U0")
    assert (await repository.get_by_alias("person-3")).bound_user_id == "U3"


async def test_same_user_can_bind_two_workspaces(migrated_db):
    repository = ProfileRepository()
    for index in range(2):
        profile = Profile(id=f"P{index}", alias=f"scope-{index}", codex_home=f"home-{index}")
        await repository.create(profile)
        await repository.bind_account(profile.id, f"W{index}", user_id="U1")
    assert len({profile.identity.owner_key for profile in await repository.list()}) == 2


async def test_legacy_binding_does_not_fabricate_a_user(migrated_db):
    repository = ProfileRepository()
    profile = Profile(alias="legacy", codex_home="home")
    await repository.create(profile)
    await repository.bind_account(profile.id, "W1")
    saved = await repository.get_by_alias("legacy")
    assert saved.bound_user_id is None
    assert saved.identity.owner_key is None


async def test_legacy_binding_cannot_erase_a_confirmed_user(migrated_db):
    repository = ProfileRepository()
    profile = Profile(alias="known", codex_home="home")
    await repository.create(profile)
    await repository.bind_account(profile.id, "W1", user_id="U1")
    with pytest.raises(ValueError):
        await repository.bind_account(profile.id, "W1")
    assert (await repository.get_by_alias("known")).bound_user_id == "U1"
