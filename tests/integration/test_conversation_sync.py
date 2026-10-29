from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from codex_account_manager.continuity.service import ContinuityService
from codex_account_manager.domain.models import ThreadInfo, ThreadRecord
from codex_account_manager.storage.repositories import ThreadRepository
from tests.fakes import FakeAppServer, account_identity


def thread(index, **values):
    return ThreadInfo(
        id=f"thread-{index}",
        cwd="C:/Work/project",
        preview="Example",
        path=None,
        model=None,
        reasoning_effort=None,
        created_at=None,
        recency_at=None,
        status=None,
        **values,
    )


async def test_complete_snapshot_uses_activity_not_import_time(migrated_db, monkeypatch):
    adapter = FakeAppServer(
        threads=[
            thread(
                i,
                updated_at=1000 + i,
                title=f"Conversation {i}",
                source="vscode",
                project_id="project",
            )
            for i in reversed(range(125))
        ]
    )
    monkeypatch.setattr(
        "codex_account_manager.continuity.service.CodexAppServer", lambda _, **kwargs: adapter
    )
    service = ContinuityService()
    result = await service.sync_threads()
    stored = await service.threads.list()
    assert len(result) == len(stored) == 125
    assert [t.id for t in stored] == [t.id for t in result]
    assert stored[0].id == "thread-124"
    assert stored[0].title == "Conversation 124"
    assert stored[0].project_id == "project"
    assert stored[0].source == "vscode"
    assert stored[0].updated_at == datetime.fromtimestamp(1124, UTC)
    assert adapter.closed


async def test_removed_conversation_disappears_without_deleting_its_checkpoint(migrated_db):
    service = ContinuityService()
    await service.threads.upsert(ThreadRecord(id="old", title="Archived"))
    await service.goals.set_objective("old", "Keep the checkpoint")
    await service.threads.replace_listed([ThreadRecord(id="new")])
    assert [t.id for t in await service.threads.list()] == ["new"]
    assert await service.threads.get("old") is not None
    assert (await service.goals.get("old")).objective == "Keep the checkpoint"
    await service.threads.replace_listed([])
    assert await service.threads.list() == []


async def test_failed_refresh_keeps_previous_snapshot(migrated_db, monkeypatch):
    service = ContinuityService()
    await service.threads.upsert(ThreadRecord(id="existing"))
    adapter = FakeAppServer()
    adapter.list_threads = AsyncMock(side_effect=RuntimeError("page two failed"))
    monkeypatch.setattr(
        "codex_account_manager.continuity.service.CodexAppServer", lambda _, **kwargs: adapter
    )
    with pytest.raises(RuntimeError, match="page two"):
        await service.sync_threads()
    assert [t.id for t in await service.threads.list()] == ["existing"]
    assert adapter.closed


async def test_failed_snapshot_write_rolls_back_visibility(migrated_db):
    repo = ThreadRepository()
    await repo.upsert(ThreadRecord(id="existing"))
    import sqlite3

    from codex_account_manager.storage.database import connect

    async with connect() as db:
        await db.execute(
            "CREATE TRIGGER reject_invalid BEFORE INSERT ON threads WHEN NEW.id = 'invalid' BEGIN SELECT RAISE(ABORT, 'write failed'); END"
        )
        await db.commit()
    with pytest.raises(sqlite3.IntegrityError, match="write failed"):
        await repo.replace_listed([ThreadRecord(id="new"), ThreadRecord(id="invalid")])
    assert [t.id for t in await repo.list()] == ["existing"]


async def test_automatic_selection_excludes_newer_subagent(migrated_db, monkeypatch):
    from unittest.mock import AsyncMock

    adapter = FakeAppServer(
        threads=[
            thread(1, updated_at=10, source="vscode"),
            thread(2, updated_at=20, source="subAgent"),
            thread(3, updated_at=30, source="subAgentReview"),
        ],
        turns={
            "thread-1": {
                "id": "limited",
                "status": "failed",
                "error": {"codexErrorInfo": "usageLimitExceeded"},
            }
        },
    )
    monkeypatch.setattr(
        "codex_account_manager.continuity.service.CodexAppServer", lambda _, **kwargs: adapter
    )
    from codex_account_manager.adapters.interfaces import GoalInfo
    from codex_account_manager.continuity.tracking import WorkTracker

    await WorkTracker().observe(
        account_identity(adapter.account_id).fingerprint,
        "thread-1",
        {"id": "limited", "status": "inProgress"},
        GoalInfo("thread-1", None, None, False),
    )
    service = ContinuityService()
    adapter.is_desktop_thread = AsyncMock(return_value=True)
    service.automation.native.latest_turn = adapter.observed_turn
    await service.observe_work()
    assert (await WorkTracker().limited(account_identity(adapter.account_id).fingerprint))[0][
        0
    ] == "thread-1"
    assert adapter.resume_calls == []


async def test_read_native_goal_never_resumes_or_mutates(monkeypatch):
    adapter = FakeAppServer(goals={"selected": "Native objective"})
    monkeypatch.setattr(
        "codex_account_manager.continuity.service.CodexAppServer", lambda _, **kwargs: adapter
    )
    result = await ContinuityService().read_native_goal("selected")
    assert result.present and result.objective == "Native objective"
    assert adapter.closed
    assert adapter.resume_calls == adapter.set_goal_calls == []
