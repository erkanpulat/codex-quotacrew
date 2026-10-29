import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from codex_account_manager.adapters.account_identity import account_scope, session_identity
from codex_account_manager.adapters.app_server import CodexAppServer
from codex_account_manager.core.errors import AppServerError, WorkspaceRoutingUnauthorizedError
from codex_account_manager.domain.identity import IdentityState, reconcile_identity


def sessions(user="U1", workspace="W1", session="S1"):
    return {
        "activeSessionId": session,
        "sessions": [
            {
                "sessionId": session,
                "userId": user,
                "selectedWorkspaceAccountId": workspace,
                "isActive": True,
                "workspaces": [{"accountId": workspace}],
            }
        ],
    }


ACCOUNT = {"account": {"type": "chatgpt"}, "workspaceRouting": {"chatgptAccountId": "W1"}}
RATE = {"accountId": "W1", "ordinaryUsageAllowed": False}


def test_four_team_users_have_distinct_owners_and_select_explicit_active_session():
    result = {"sessions": []}
    for index in range(1, 5):
        result["sessions"].extend(sessions(f"U{index}", session=f"S{index}")["sessions"])
    keys = set()
    for index in range(1, 5):
        result["activeSessionId"] = f"S{index}"
        for row in result["sessions"]:
            row["isActive"] = row["sessionId"] == result["activeSessionId"]
        identity = session_identity(result)
        assert identity.owner_key == ("chatgpt", f"U{index}", "W1")
        keys.add(identity.owner_key)
    assert len(keys) == 4


def test_new_session_same_owner_and_same_user_different_workspace():
    first = session_identity(sessions())
    assert first.owner_key == session_identity(sessions(session="S2")).owner_key
    assert first.owner_key != session_identity(sessions(workspace="W2")).owner_key


@pytest.mark.parametrize(
    "value", [None, [], {}, {"sessions": []}, {"activeSessionId": "S1", "sessions": [None]}]
)
def test_unreadable_sessions_never_provide_an_owner(value):
    identity = session_identity(value)
    assert identity.state is IdentityState.UNKNOWN
    assert identity.owner_key is None


@pytest.mark.parametrize("field", ["userId", "selectedWorkspaceAccountId", "sessionId"])
@pytest.mark.parametrize("value", [True, 7, [], "", " U1", "U1\n", "x" * 321])
def test_malformed_identifier_is_not_silently_ignored(field, value):
    result = sessions()
    result["sessions"][0][field] = value
    identity = session_identity(result)
    assert identity.state is IdentityState.UNKNOWN
    assert identity.owner_key is None


@pytest.mark.parametrize("change", ["duplicate", "other_active", "no_active", "missing_selected"])
def test_ambiguous_session_is_conflict(change):
    result = sessions()
    if change == "duplicate":
        result["sessions"].append(deepcopy(result["sessions"][0]))
    elif change == "other_active":
        result["sessions"].extend(sessions("U2", session="S2")["sessions"])
    elif change == "no_active":
        result["sessions"][0]["isActive"] = False
    else:
        result["activeSessionId"] = "absent"
    assert session_identity(result).state is IdentityState.CONFLICT


def test_email_and_unknown_user_fields_are_not_identity_evidence():
    result = sessions(user=None)
    result["sessions"][0].update(email="person@example.test", sub="U1", user_id="U1")
    identity = session_identity(result)
    assert identity.state is IdentityState.PARTIAL
    assert identity.owner_key is None
    account = deepcopy(ACCOUNT)
    account["account"]["userId"] = "U1"
    assert account_scope(account, {**RATE, "userId": "U1"}).state is IdentityState.PARTIAL


@pytest.mark.parametrize("account", [{}, {"account": None}, {"account": {"type": "apiKey"}}])
def test_other_auth_modes_cannot_be_complete(account):
    scope = account_scope(account, RATE)
    current = session_identity(sessions())
    assert reconcile_identity(scope, current, current).owner_key is None


def test_conflicting_workspace_sources_are_not_resolved_by_precedence():
    scope = account_scope(ACCOUNT, {"accountId": "W2"})
    assert scope.state is IdentityState.CONFLICT
    current = session_identity(sessions())
    assert reconcile_identity(scope, current, current).state is IdentityState.CONFLICT


@pytest.mark.parametrize("field,value", [("user", "U2"), ("workspace", "W2"), ("session", "S2")])
def test_change_during_quota_read_invalidates_identity(field, value):
    identity = reconcile_identity(
        account_scope(ACCOUNT, RATE),
        session_identity(sessions()),
        session_identity(sessions(**{field: value})),
    )
    assert identity.state is IdentityState.CONFLICT
    assert identity.owner_key is None


def test_identifier_values_do_not_appear_in_repr():
    result = session_identity(sessions("private-user", "private-workspace", "private-session"))
    assert "private" not in repr(result)


def test_session_workspace_must_match_the_quota_scope():
    current = session_identity(sessions(workspace="W2"))
    result = reconcile_identity(account_scope(ACCOUNT, RATE), current, current)
    assert result.state is IdentityState.CONFLICT


def test_missing_selected_workspace_is_partial_not_a_conflict():
    current = session_identity(sessions(workspace=None))
    result = reconcile_identity(account_scope(ACCOUNT, RATE), current, current)
    assert result.state is IdentityState.PARTIAL
    assert result.owner_key is None


@pytest.mark.parametrize("value", [[], True, "", " W1", "W1\n"])
def test_bad_workspace_evidence_cannot_be_filled_from_session(value):
    scope = account_scope(ACCOUNT, {"accountId": value})
    current = session_identity(sessions())
    assert reconcile_identity(scope, current, current).owner_key is None


async def test_opt_in_read_checks_session_on_both_sides_and_preserves_quota(monkeypatch):
    server = CodexAppServer("test")
    request = AsyncMock(side_effect=[sessions(), ACCOUNT, RATE, sessions()])
    monkeypatch.setattr(server, "_request", request)
    snapshot = await server.read_account(include_identity=True)
    assert snapshot.identity.owner_key == ("chatgpt", "U1", "W1")
    assert snapshot.ordinary_usage_allowed is False
    assert [call.args[0] for call in request.await_args_list] == [
        "accountSession/list",
        "account/read",
        "account/rateLimits/read",
        "accountSession/list",
    ]
    assert request.await_args_list[0].args[1] == {"refreshWorkspaceMetadata": False}


@pytest.mark.parametrize(
    "failure", ["Method not found (-32601)", "401 Unauthorized", "broken pipe"]
)
async def test_session_failure_is_unknown_without_losing_quota_or_refreshing(monkeypatch, failure):
    server = CodexAppServer("test")
    request = AsyncMock(
        side_effect=[AppServerError(failure), ACCOUNT, RATE, AppServerError(failure)]
    )
    monkeypatch.setattr(server, "_request", request)
    snapshot = await server.read_account(include_identity=True)
    assert snapshot.identity.state is IdentityState.UNKNOWN
    assert snapshot.ordinary_usage_allowed is False
    assert request.await_count == 4
    assert request.await_args_list[1].args[1] == {"refreshToken": False}


async def test_opt_in_read_detects_same_workspace_user_switch(monkeypatch):
    server = CodexAppServer("test")
    monkeypatch.setattr(
        server, "_request", AsyncMock(side_effect=[sessions(), ACCOUNT, RATE, sessions("U2")])
    )
    snapshot = await server.read_account(include_identity=True)
    assert snapshot.identity.state is IdentityState.CONFLICT


async def test_quota_routing_401_is_still_an_error(monkeypatch):
    server = CodexAppServer("test")
    request = AsyncMock(
        side_effect=[
            sessions(),
            ACCOUNT,
            AppServerError("workspace routing discovery unauthorized (401)"),
        ]
    )
    monkeypatch.setattr(server, "_request", request)
    with pytest.raises(WorkspaceRoutingUnauthorizedError):
        await server.read_account(include_identity=True)
    assert request.await_count == 3


async def test_session_probe_timeout_is_bounded(monkeypatch):
    server = CodexAppServer("test", timeout=0.01)

    async def request(method, params=None):
        if method == "accountSession/list":
            await asyncio.Event().wait()
        return ACCOUNT if method == "account/read" else RATE

    monkeypatch.setattr(server, "_request", request)
    snapshot = await asyncio.wait_for(server.read_account(include_identity=True), 1)
    assert snapshot.identity.state is IdentityState.UNKNOWN


async def test_cancellation_during_probe_is_not_swallowed(monkeypatch):
    server = CodexAppServer("test")
    request = AsyncMock(side_effect=asyncio.CancelledError)
    monkeypatch.setattr(server, "_request", request)
    with pytest.raises(asyncio.CancelledError):
        await server.read_account(include_identity=True)
    request.assert_awaited_once()


async def test_default_read_adds_no_session_calls(monkeypatch):
    server = CodexAppServer("test")
    request = AsyncMock(side_effect=[ACCOUNT, RATE])
    monkeypatch.setattr(server, "_request", request)
    snapshot = await server.read_account()
    assert snapshot.identity.state is IdentityState.PARTIAL
    assert request.await_count == 2


async def test_failed_probe_is_not_cached_as_permanent_lack_of_support(monkeypatch):
    server = CodexAppServer("test")
    monkeypatch.setattr(
        server, "_request", AsyncMock(side_effect=[AppServerError("401"), sessions()])
    )
    assert (await server._session_identity()).state is IdentityState.UNKNOWN
    assert (await server._session_identity()).owner_key == ("chatgpt", "U1", "W1")


async def test_probe_timeout_removes_pending_rpc(monkeypatch):
    server = CodexAppServer("test", timeout=0.01)
    monkeypatch.setattr(server, "_send", AsyncMock())
    assert (await server._session_identity()).state is IdentityState.UNKNOWN
    assert not server._pending


async def test_forbidden_shared_refresh_does_not_start_identity_probe(monkeypatch):
    server = CodexAppServer("test")
    request = AsyncMock()
    monkeypatch.setattr(server, "_request", request)
    with pytest.raises(AppServerError, match="isolated"):
        await server.read_account(force_refresh=True, include_identity=True)
    request.assert_not_awaited()
