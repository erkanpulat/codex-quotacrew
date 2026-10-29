from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from codex_account_manager.continuity.automation import ContinuationSupervisor
from codex_account_manager.storage.database import connect
from tests.fakes import ExecutionServer, account_identity
from tests.unit.test_automation import goal, prepared
from tests.unit.test_native_desktop import setup_native


@pytest.mark.parametrize("owner", [None, "other-user"])
async def test_missing_or_other_owner_cannot_send_in_same_workspace(migrated_db, owner):
    server = ExecutionServer()
    supervisor = ContinuationSupervisor(factory=lambda: server)
    ticket = await prepared(supervisor)
    ticket = replace(
        ticket,
        owner_key=account_identity("acc-1", owner).fingerprint if owner else None,
    )
    await supervisor.run(ticket)
    assert not server.turn_calls
    async with connect() as db:
        assert not await (await db.execute("SELECT * FROM continuation_attempts")).fetchall()


async def test_user_changed_between_goal_turns_cannot_receive_next_message(migrated_db):
    class ChangedUser(ExecutionServer):
        async def run_continuation_turn(self, *args, **kwargs):
            result = await super().run_continuation_turn(*args, **kwargs)
            self.identity = account_identity("acc-1", "other-user")
            return result

    server = ChangedUser()
    server.native = goal("active")
    server.after_turn = [goal("active"), goal("complete")]
    supervisor = ContinuationSupervisor(factory=lambda: server)
    await supervisor.run(await prepared(supervisor))
    assert len(server.turn_calls) == 1


async def test_desktop_checks_owner_again_after_waiting_for_connection(migrated_db):
    supervisor, server, source, ticket = setup_native()

    async def turn(*_args, **_kwargs):
        server.identity = account_identity("acc-1", "other-user")
        return source

    supervisor.native.latest_turn = AsyncMock(side_effect=turn)
    await supervisor.run(ticket)
    supervisor.native.send.assert_not_awaited()
    async with connect() as db:
        assert not await (await db.execute("SELECT * FROM continuation_attempts")).fetchall()


async def test_legacy_pending_is_preserved_without_sending_or_guessing_owner(migrated_db):
    server = ExecutionServer()
    supervisor = ContinuationSupervisor(factory=lambda: server)
    ticket = replace(await prepared(supervisor), owner_key=None)
    await supervisor.save_pending([ticket])
    supervisor.report = AsyncMock()
    await supervisor.recover_pending(account_identity("acc-1").fingerprint)
    assert not supervisor.tasks and not server.turn_calls
    supervisor.report.assert_awaited_once_with("thread", "needs_user", "identity")
    async with connect() as db:
        assert await (
            await db.execute("SELECT owner_key FROM pending_continuations")
        ).fetchall() == [(None,)]


async def test_same_workspace_other_user_cannot_recover_pending(migrated_db):
    server = ExecutionServer()
    supervisor = ContinuationSupervisor(factory=lambda: server)
    await supervisor.save_pending([await prepared(supervisor)])
    await supervisor.recover_pending(account_identity("acc-1", "other-user").fingerprint)
    assert not supervisor.tasks and not server.turn_calls


async def test_old_queue_cleanup_preserves_replacement_users_pending_ticket(migrated_db):
    supervisor = ContinuationSupervisor(factory=lambda: ExecutionServer())
    old = await prepared(supervisor)
    replacement = replace(old, owner_key=account_identity("acc-1", "other-user").fingerprint)
    await supervisor.save_pending([replacement])
    await supervisor.discard_pending([old])
    async with connect() as db:
        assert await (
            await db.execute("SELECT owner_key FROM pending_continuations")
        ).fetchall() == [(replacement.owner_key,)]
