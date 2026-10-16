"""End-to-end continuity handoff using fake adapters (no real Codex)."""

from __future__ import annotations

from codex_account_manager.accounts.service import AccountService
from codex_account_manager.adapters.credential_store import FileCredentialStore
from codex_account_manager.auth.transaction import AuthTransaction
from codex_account_manager.continuity.service import ContinuityService
from codex_account_manager.goals.service import GoalService
from tests.fakes import FakeAppServer, FakeDesktop


def _factory(account_id):
    def make(_home):
        return FakeAppServer(account_id=account_id)

    return make


async def test_manual_handoff_switches_and_reconciles(migrated_db, monkeypatch):
    # Two profiles, each bound to its own account.
    accounts = AccountService(app_server_factory=_factory("acc-2"))
    p1 = await accounts.create_profile("ana")
    p2 = await accounts.create_profile("hesap2")
    for p, acc in ((p1, "acc-1"), (p2, "acc-2")):
        (migrated_db.profiles_dir / p.id / "auth.json").write_bytes(b"{}")
        await accounts.profiles.bind_account(p.id, acc)

    # Active auth currently = acc-1.
    store = FileCredentialStore(shared_home=migrated_db.shared_codex_home)
    store.write_active_atomic(b"acc-1-auth")

    # Local notes must not recreate a missing native goal or reset its budget.
    goals = GoalService()
    await goals.set_objective("t1", "Finish the refactor")

    # Patch the App Server used by ContinuityService.resume_conversation and by
    # AccountService.resolve_active_alias to our fake (acc-2 after switch).
    import codex_account_manager.continuity.service as cs

    fake = FakeAppServer(account_id="acc-2", goals={"t1": None})
    monkeypatch.setattr(cs, "CodexAppServer", lambda home: fake)

    async def verify(_home):
        return "acc-2"

    tx = AuthTransaction(credential_store=store, desktop=FakeDesktop(), verify_account=verify)
    service = ContinuityService(accounts=accounts, goals=goals)

    result = await service.handoff("hesap2", thread_id="t1", transaction=tx)

    assert result.success is True
    assert store.read_active() == b"{}"  # profile hesap2's auth is now active
    assert fake.set_goal_calls == []
    # Handoff recorded.
    handoffs = await service.recent_handoffs()
    assert handoffs and handoffs[0].success is True
