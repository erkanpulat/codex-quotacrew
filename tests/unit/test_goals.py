from unittest.mock import AsyncMock

import pytest

from codex_account_manager.adapters.interfaces import GoalInfo
from codex_account_manager.domain.states import GoalState
from codex_account_manager.goals.service import GoalService
from tests.fakes import FakeAppServer


async def test_set_and_get_goal(migrated_db):
    svc = GoalService()
    goal = await svc.set_objective("t1", "Ship the feature")
    assert goal.objective == "Ship the feature"
    fetched = await svc.get("t1")
    assert fetched.objective == "Ship the feature"
    assert fetched.revision == 1


async def test_set_objective_bumps_revision(migrated_db):
    svc = GoalService()
    await svc.set_objective("t1", "v1")
    g2 = await svc.set_objective("t1", "v2")
    assert g2.revision == 2
    assert g2.objective == "v2"


async def test_local_note_does_not_create_missing_native_goal(migrated_db):
    svc = GoalService()
    await svc.set_objective("t1", "Keep going")
    adapter = FakeAppServer(goals={"t1": None})
    result = await svc.reconcile_after_resume("t1", adapter)
    assert result.action == "blocked"
    assert (await svc.get("t1")).local_status == GoalState.NEEDS_USER
    assert adapter.set_goal_calls == []


@pytest.mark.parametrize("tokens_used", [40, 100])
async def test_missing_goal_does_not_reset_native_budget(migrated_db, tokens_used):
    svc = GoalService()
    await svc.set_objective("t1", "Local note")
    adapter = FakeAppServer()
    adapter.get_goal = AsyncMock(
        return_value=GoalInfo(
            thread_id="t1",
            objective="Native goal",
            status="active",
            present=True,
            token_budget=100,
            tokens_used=tokens_used,
        )
    )
    await svc.reconcile_after_resume("t1", adapter)
    adapter.get_goal.return_value = GoalInfo(
        thread_id="t1", objective=None, status=None, present=False
    )
    result = await svc.reconcile_after_resume("t1", adapter)
    assert result.action == "blocked"
    assert adapter.set_goal_calls == []


async def test_reconcile_skips_user_cleared_goal(migrated_db):
    svc = GoalService()
    await svc.set_objective("t1", "Old goal")
    await svc.user_clear("t1")
    adapter = FakeAppServer(goals={"t1": None})
    result = await svc.reconcile_after_resume("t1", adapter)
    assert result.action == "skipped_user_cleared"
    assert adapter.set_goal_calls == []


async def test_reconcile_does_not_touch_terminal_goal(migrated_db):
    svc = GoalService()
    await svc.set_objective("t1", "Done work")
    await svc.mark_status("t1", GoalState.COMPLETE)
    adapter = FakeAppServer(goals={"t1": None})
    result = await svc.reconcile_after_resume("t1", adapter)
    assert result.action == "terminal"
    assert adapter.set_goal_calls == []


async def test_reconcile_no_action_when_native_present(migrated_db):
    svc = GoalService()
    await svc.set_objective("t1", "Active goal")
    adapter = FakeAppServer(goals={"t1": "Active goal"})
    result = await svc.reconcile_after_resume("t1", adapter)
    assert result.action == "none"


async def test_unknown_native_state_is_not_reported_missing(migrated_db):
    svc = GoalService()
    await svc.set_objective("t1", "Local note")
    adapter = FakeAppServer()
    adapter.get_goal = AsyncMock(return_value=None)
    result = await svc.reconcile_after_resume("t1", adapter)
    assert result.action == "blocked"
    assert (await svc.get("t1")).native_goal_present is None
    assert adapter.set_goal_calls == []


async def test_native_budget_limit_is_preserved_even_if_later_missing(migrated_db):
    svc = GoalService()
    await svc.set_objective("t1", "Local note")
    adapter = FakeAppServer()
    adapter.get_goal = AsyncMock(
        return_value=GoalInfo(
            thread_id="t1", objective="Native goal", status="budgetLimited", present=True
        )
    )
    assert (await svc.reconcile_after_resume("t1", adapter)).action == "native_status"
    await svc.mark_status("t1", GoalState.RESUMING)
    assert (await svc.get("t1")).local_status == GoalState.BLOCKED
    adapter.get_goal = AsyncMock(
        return_value=GoalInfo(thread_id="t1", objective=None, status=None, present=False)
    )
    assert (await svc.reconcile_after_resume("t1", adapter)).action == "suspended"
    assert adapter.set_goal_calls == []
