import asyncio
import base64
import json
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from codex_account_manager.adapters.app_server import CodexAppServer
from codex_account_manager.adapters.auth_identity import (
    MAX_AUTH_BYTES,
    auth_evidence,
    read_auth_evidence,
    stable_auth_identity,
)
from codex_account_manager.core.errors import AppServerError, UnsupportedMethodError
from codex_account_manager.domain.identity import IdentityState


def token(user="U1", workspace="W1", **extra):
    claims = {
        "https://api.openai.com/auth": {
            "chatgpt_user_id": user,
            "chatgpt_account_id": workspace,
            **extra,
        }
    }
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


def auth(user="U1", workspace="W1", **extra):
    return json.dumps(
        {
            "auth_mode": "chatgpt",
            "tokens": {
                "id_token": token(user, workspace),
                "access_token": token(user, workspace),
                "account_id": workspace,
                **extra,
            },
        }
    ).encode()


def test_same_workspace_four_local_users_are_distinct():
    keys = {auth_evidence(auth(f"U{number}")).identity.owner_key for number in range(4)}
    assert keys == {("chatgpt", f"U{number}", "W1") for number in range(4)}


@pytest.mark.parametrize(
    "raw",
    [b"", b"{}", b"null", b"[]", b"{", b"x" * (MAX_AUTH_BYTES + 1)],
    ids=["empty", "object", "null", "array", "malformed", "oversized"],
)
def test_bad_or_oversized_auth_never_provides_identity(raw):
    assert auth_evidence(raw).identity.state is IdentityState.UNKNOWN


@pytest.mark.parametrize(
    "field,value",
    [
        ("account_id", "W2"),
        ("access_token", token("U2")),
        ("id_token", token("U1", user_id="U2")),
        ("id_token", token("U1", "W2")),
    ],
)
def test_conflicting_token_claims_cannot_provide_identity(field, value):
    assert auth_evidence(auth(**{field: value})).identity.state is IdentityState.CONFLICT


@pytest.mark.parametrize(
    "field,value",
    [
        ("id_token", "invalid"),
        ("id_token", "header.!!!.signature"),
        ("access_token", "broken.jwt"),
        ("account_id", []),
        ("login_owner_id", True),
    ],
)
def test_malformed_fields_are_unknown(field, value):
    assert auth_evidence(auth(**{field: value})).identity.state is IdentityState.UNKNOWN


def test_fallback_user_claim_is_supported_but_membership_id_is_not_a_user():
    result = auth_evidence(auth(id_token=token(None, user_id="U1"), access_token="opaque"))
    assert result.identity.owner_key == ("chatgpt", "U1", "W1")
    membership = auth_evidence(
        auth(id_token=token(None, chatgpt_account_user_id="member"), access_token="opaque")
    )
    assert membership.identity.state is IdentityState.PARTIAL


def test_user_claim_without_workspace_binding_is_partial():
    result = auth_evidence(auth(id_token=token("U1", None), access_token="opaque"))
    assert result.identity.state is IdentityState.PARTIAL


def test_token_rotation_is_not_a_user_change():
    old = auth_evidence(auth(access_token="old-opaque", login_owner_id="login"))
    new = auth_evidence(auth(access_token="new-opaque", login_owner_id="login"))
    assert stable_auth_identity(old, old, new).owner_key == ("chatgpt", "U1", "W1")


@pytest.mark.parametrize("new", [auth("U2"), auth(login_owner_id="another-login")])
def test_same_workspace_user_or_login_change_is_conflict(new):
    old = auth_evidence(auth())
    assert stable_auth_identity(old, old, auth_evidence(new)).state is IdentityState.CONFLICT


def test_process_start_identity_cannot_be_overwritten_by_later_file():
    startup, current = auth_evidence(auth("U1")), auth_evidence(auth("U2"))
    assert stable_auth_identity(startup, current, current).state is IdentityState.CONFLICT


@pytest.mark.parametrize("before_read", [False, True])
def test_same_bytes_replaced_during_read_are_not_treated_as_unchanged(before_read):
    old = replace(auth_evidence(auth()), revision=(1, 2, 3, "same-digest"))
    new = replace(old, revision=(4, 5, 3, "same-digest"))
    assert (
        stable_auth_identity(old, new if before_read else old, new).state is IdentityState.CONFLICT
    )


def test_file_read_is_bounded_and_metadata_contains_no_credentials(tmp_path):
    path = tmp_path / "auth.json"
    path.write_bytes(auth(login_owner_id="private-login"))
    result = read_auth_evidence(tmp_path)
    assert result.identity.owner_key == ("chatgpt", "U1", "W1")
    assert "private-login" not in repr(result) and "header" not in repr(result)
    path.write_bytes(b"x" * (MAX_AUTH_BYTES + 1))
    assert read_auth_evidence(tmp_path).identity.owner_key is None


async def test_old_cli_falls_back_only_on_structured_method_not_found(tmp_path, monkeypatch):
    (tmp_path / "auth.json").write_bytes(auth())
    server = CodexAppServer(tmp_path)
    server._starting_auth = read_auth_evidence(tmp_path)
    request = AsyncMock(
        side_effect=[
            UnsupportedMethodError("Unavailable"),
            {"account": {"type": "chatgpt"}},
            {"accountId": "W1", "ordinaryUsageAllowed": False},
            UnsupportedMethodError("Unavailable"),
        ]
    )
    monkeypatch.setattr(server, "_request", request)
    result = await server.read_account(include_identity=True)
    assert result.identity.owner_key == ("chatgpt", "U1", "W1")
    assert result.ordinary_usage_allowed is False


async def test_unknown_session_variant_uses_stable_auth_through_real_rpc_reader(
    tmp_path, monkeypatch
):
    (tmp_path / "auth.json").write_bytes(auth())
    server = CodexAppServer(tmp_path)
    server._starting_auth = read_auth_evidence(tmp_path)
    calls = []

    async def send(request):
        method = request["method"]
        calls.append(method)
        response = {"id": request["id"]}
        if method == "accountSession/list":
            response["error"] = {
                "code": -32600,
                "message": "Invalid request: unknown variant `accountSession/list`, expected one of `initialize`, `account/read`",
            }
        elif method == "account/read":
            response["result"] = {"account": {"type": "chatgpt", "planType": "plus"}}
        else:
            assert method == "account/rateLimits/read"
            response["result"] = {"accountId": "W1", "ordinaryUsageAllowed": True}
        stream = asyncio.StreamReader()
        stream.feed_data(json.dumps(response).encode() + b"\n")
        stream.feed_eof()
        await server._read_messages(stream)

    monkeypatch.setattr(server, "_send", send)
    result = await server.read_account(include_identity=True)
    assert result.identity.owner_key == ("chatgpt", "U1", "W1")
    assert result.ordinary_usage_allowed is True
    assert calls == [
        "accountSession/list",
        "account/read",
        "account/rateLimits/read",
        "accountSession/list",
    ]


@pytest.mark.parametrize(
    "error", ["401", "Method not found text without error code", "connection lost"]
)
async def test_token_metadata_never_masks_session_error(tmp_path, monkeypatch, error):
    (tmp_path / "auth.json").write_bytes(auth())
    server = CodexAppServer(tmp_path)
    server._starting_auth = read_auth_evidence(tmp_path)
    monkeypatch.setattr(
        server,
        "_request",
        AsyncMock(
            side_effect=[
                AppServerError(error),
                {"account": {"type": "chatgpt"}},
                {"accountId": "W1"},
                AppServerError(error),
            ]
        ),
    )
    result = await server.read_account(include_identity=True)
    assert result.identity.owner_key is None


@pytest.mark.parametrize(
    "code,message,expected",
    [
        (-32601, "not available", UnsupportedMethodError),
        (
            -32600,
            "Invalid request: unknown variant `accountSession/list`, expected one of `initialize`, `account/read`",
            UnsupportedMethodError,
        ),
        (401, "not available", AppServerError),
        (
            401,
            "Invalid request: unknown variant `accountSession/list`, expected one of `initialize`",
            AppServerError,
        ),
        (
            -32600,
            "Invalid request: unknown variant `account/read`, expected one of `initialize`",
            AppServerError,
        ),
        (
            -32600,
            "Invalid request: unknown variant `refreshWorkspaceMetadata`, expected one of `false`",
            AppServerError,
        ),
        (-32600, "accountSession/list unavailable", AppServerError),
        (-32600, "Invalid request: unknown variant `accountSession/list`", AppServerError),
    ],
)
async def test_rpc_preserves_method_not_found_as_a_typed_error(code, message, expected):
    server = CodexAppServer("test")
    stream = asyncio.StreamReader()
    stream.feed_data(
        json.dumps({"id": 1, "error": {"code": code, "message": message}}).encode() + b"\n"
    )
    stream.feed_eof()
    future = asyncio.get_running_loop().create_future()
    server._pending[1] = future
    await server._read_messages(stream)
    with pytest.raises(expected) as error:
        await future
    assert type(error.value) is expected
