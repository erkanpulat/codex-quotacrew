import json
from unittest.mock import AsyncMock

import pytest

from codex_account_manager.adapters import native_desktop as native
from codex_account_manager.adapters.interfaces import GoalInfo
from codex_account_manager.continuity.automation import ContinuationSupervisor, ContinuationTicket
from codex_account_manager.continuity.tracking import goal_signature
from codex_account_manager.core.errors import AppServerError
from codex_account_manager.storage.database import connect
from tests.fakes import ExecutionServer

REAL_EXCHANGE = native._exchange


def reply(value):
    return {
        "jsonrpc": "2.0",
        "id": "request",
        "result": {"success": True, "contentItems": [{"type": "text", "text": json.dumps(value)}]},
    }


def test_decode_native_result_requires_positive_receipt():
    assert native.decode_result(reply({"accepted": True}), "request") == {"accepted": True}
    for value in [{}, {"error": {}}, {"result": {"success": False}}, reply({"isError": True})]:
        with pytest.raises(AppServerError):
            native.decode_result(value, "request")
    with pytest.raises(AppServerError):
        native.decode_result(reply({}), "wrong-id")


@pytest.mark.parametrize("change", [{"id": "other"}, {"kind": "chatgpt"}, {"hostId": "remote"}])
def test_native_snapshot_rejects_wrong_thread_or_host(change):
    data = {
        "thread": {"id": "thread", "kind": "codex", "hostId": "local", **change},
        "turns": [{"id": "turn", "status": "inProgress"}],
    }
    with pytest.raises(AppServerError):
        native.read_snapshot(data, "thread")


async def test_native_send_preserves_model_permissions_and_uses_target_context(monkeypatch):
    seen = []

    def exchange(payload, cancelled, timeout):
        seen.append(payload)
        result = reply({"threadId": "thread"})
        result["id"] = payload["id"]
        return result

    monkeypatch.setattr(native, "_exchange", exchange)
    await native.NativeDesktop().send("thread", "turn")
    params = seen[0]["params"]
    assert params["tool"] == "send_message_to_thread"
    assert params["threadId"] == "thread" and params["turnId"] == "turn"
    assert set(params["arguments"]) == {"threadId", "prompt"}
    assert "budget" in params["arguments"]["prompt"]


@pytest.mark.parametrize("bad", ["", "../thread", "a\n", "a?prompt=b", "a" * 129])
async def test_native_send_rejects_invalid_identity(bad):
    with pytest.raises(AppServerError):
        await native.NativeDesktop().send(bad, "turn")


def setup_native():
    server = ExecutionServer()
    supervisor = ContinuationSupervisor(factory=lambda: server)
    source = {"id": "last", "status": "failed", "error": {"codexErrorInfo": "usageLimitExceeded"}}
    supervisor.native.latest_turn = AsyncMock(return_value=source)
    supervisor.native.send = AsyncMock(return_value={"accepted": True})
    ticket = ContinuationTicket(
        "thread", "last", "acc-1", goal_signature(GoalInfo("thread", None, None, False)), True
    )
    return supervisor, server, source, ticket


async def test_native_delivery_stays_in_desktop_and_tracks_next_account_limit(migrated_db):
    supervisor, server, source, ticket = setup_native()
    running = {"id": "native-next", "status": "inProgress"}
    supervisor.native.latest_turn.side_effect = [source, source, running]
    await supervisor.run(ticket)
    supervisor.native.send.assert_awaited_once_with("thread", "last")
    assert not server.resume_calls and not server.turn_calls
    assert (await supervisor.tracker.visible())[0].turn_status == "inProgress"
    await supervisor.tracker.observe(
        "acc-1",
        "thread",
        {**source, "id": "native-next"},
        GoalInfo("thread", None, None, False),
        limited=True,
    )
    assert (await supervisor.tracker.limited("acc-1"))[0][1] == "native-next"
    supervisor.native.latest_turn.side_effect = None
    supervisor.native.latest_turn.return_value = source
    await supervisor.run(ticket)
    assert supervisor.native.send.await_count == 1


async def test_routing_401_is_reported_as_authentication_not_quota(migrated_db):
    supervisor, server, source, ticket = setup_native()
    rejected = {
        "id": "native-next",
        "status": "failed",
        "error": {"message": "workspace routing discovery unauthorized (401)"},
    }
    supervisor.native.latest_turn.side_effect = [source, source, rejected]
    server.latest_turn = AsyncMock(return_value=rejected)
    supervisor.report = AsyncMock()
    await supervisor.run(ticket)
    supervisor.report.assert_any_await("thread", "needs_user", "authentication")
    supervisor.native.send.assert_awaited_once()
    assert not await supervisor.tracker.limited("acc-1")


async def test_routing_401_during_delivery_is_not_replayed(migrated_db):
    supervisor, server, source, ticket = setup_native()
    supervisor.native.send.side_effect = AppServerError(
        "workspace routing discovery unauthorized (401)"
    )
    supervisor.report = AsyncMock()
    await supervisor.run(ticket)
    supervisor.report.assert_any_await("thread", "needs_user", "authentication")
    await supervisor.run(ticket)
    supervisor.native.send.assert_awaited_once()


@pytest.mark.parametrize("fault", ["account", "new_turn", "running", "goal", "uncertain"])
async def test_native_delivery_stops_on_conflicts_and_never_retries_uncertain(migrated_db, fault):
    supervisor, server, source, ticket = setup_native()
    if fault == "account":
        ticket = ContinuationTicket("thread", "last", "other", ticket.goal_signature, True)
    elif fault == "new_turn":
        supervisor.native.latest_turn.return_value = {**source, "id": "new"}
    elif fault == "running":
        supervisor.native.latest_turn.return_value = {**source, "status": "inProgress"}
    elif fault == "goal":
        ticket = ContinuationTicket("thread", "last", "acc-1", "changed", True)
    else:
        supervisor.native.send.side_effect = TimeoutError()
    await supervisor.run(ticket)
    await supervisor.run(ticket)
    assert supervisor.native.send.await_count == (1 if fault == "uncertain" else 0)
    assert not server.resume_calls and not server.turn_calls
    if fault == "uncertain":
        async with connect() as db:
            row = await (await db.execute("SELECT status FROM continuation_attempts")).fetchone()
        assert row == ("uncertain",)


@pytest.mark.parametrize(
    "path,valid",
    [
        (
            r"C:\Program Files\WindowsApps\OpenAI.Codex_26.1.0.0_x64__2p2nqsd0c76g0\app\ChatGPT.exe",
            True,
        ),
        (
            r"C:\Users\Example\WindowsApps\OpenAI.Codex_26.1.0.0_x64__2p2nqsd0c76g0\app\ChatGPT.exe",
            False,
        ),
        (r"C:\Program Files\WindowsApps\OpenAI.Codex_26.1.0.0_x64__other\app\ChatGPT.exe", False),
    ],
)
def test_only_official_packaged_executable_path_is_trusted(path, valid):
    assert bool(native.DESKTOP_EXECUTABLE.fullmatch(path)) is valid


@pytest.mark.parametrize("fault", [None, "oversize", "short-write", "cancel-read", "untrusted"])
def test_native_transport_bounds_and_closes_all_handles(monkeypatch, fault):
    import struct
    import sys
    import threading
    from types import SimpleNamespace

    token = threading.Event()
    closed = []

    class Handle:
        def Close(self):
            closed.append(self)

    handle = Handle()
    wire = json.dumps(reply({"ok": True})).encode()
    incoming = bytearray(
        struct.pack("<I", native.MAX_FRAME + 1 if fault == "oversize" else len(wire)) + wire
    )
    counts = {}
    phase = []

    def write(_handle, data, op):
        phase.append("write")
        counts[id(op)] = len(data) - (fault == "short-write")

    def read(_handle, buffer, op):
        phase.append("read")
        count = min(len(buffer), len(incoming), 7)
        buffer[:count] = incoming[:count]
        del incoming[:count]
        counts[id(op)] = count

    def wait(_event, _timeout):
        if fault == "cancel-read" and phase[-1] == "read":
            token.set()
            return 258
        return 0

    monkeypatch.setattr(
        native,
        "os",
        SimpleNamespace(name="nt", environ={}, listdir=lambda _: ["codex-browser-use-" + "a" * 36]),
    )
    monkeypatch.setattr(native, "_trusted_handle", lambda _: fault != "untrusted")
    monkeypatch.setitem(
        sys.modules, "pywintypes", SimpleNamespace(OVERLAPPED=SimpleNamespace, error=OSError)
    )
    monkeypatch.setitem(
        sys.modules,
        "win32con",
        SimpleNamespace(
            GENERIC_READ=1,
            GENERIC_WRITE=2,
            OPEN_EXISTING=3,
            FILE_FLAG_OVERLAPPED=4,
            WAIT_TIMEOUT=258,
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "win32event",
        SimpleNamespace(CreateEvent=lambda *args: Handle(), WaitForSingleObject=wait),
    )
    monkeypatch.setitem(
        sys.modules,
        "win32file",
        SimpleNamespace(
            CreateFile=lambda *args: handle,
            AllocateReadBuffer=bytearray,
            ReadFile=read,
            WriteFile=write,
            CancelIo=lambda _: None,
            GetOverlappedResult=lambda h, op, wait: counts[id(op)],
        ),
    )
    if fault:
        with pytest.raises(AppServerError):
            REAL_EXCHANGE({"id": "request"}, token, 1)
    else:
        assert REAL_EXCHANGE({"id": "request"}, token, 1)["id"] == "request"
    assert handle in closed


async def test_native_readiness_retries_reads_only():
    client = native.NativeDesktop()
    client.latest_turn = AsyncMock(side_effect=[AppServerError("starting"), {"id": "turn"}])
    client.send = AsyncMock()
    assert await client.wait_latest_turn("thread") == {"id": "turn"}
    assert client.latest_turn.await_count == 2
    client.send.assert_not_awaited()


async def test_native_readiness_timeout_does_not_send():
    client = native.NativeDesktop()
    client.latest_turn = AsyncMock(side_effect=AppServerError("starting"))
    client.send = AsyncMock()
    with pytest.raises(AppServerError, match="ready"):
        await client.wait_latest_turn("thread", timeout=0.01)
    client.send.assert_not_awaited()


async def test_native_send_rejects_receipt_for_another_thread(monkeypatch):
    client = native.NativeDesktop()
    client.call = AsyncMock(return_value={"threadId": "other"})
    with pytest.raises(AppServerError, match="receipt"):
        await client.send("thread", "turn")


@pytest.mark.parametrize("status", ["paused", "budgetLimited", "complete"])
async def test_goal_changed_during_desktop_verification_is_not_resumed(migrated_db, status):
    supervisor, server, source, ticket = setup_native()
    original = GoalInfo("thread", "Test objective", "active", True, 1000, 50)
    changed = GoalInfo("thread", "Test objective", status, True, 1000, 50)
    server.get_goal = AsyncMock(side_effect=[original, changed])
    ticket = ContinuationTicket("thread", "last", "acc-1", goal_signature(original), True)
    await supervisor.run(ticket)
    supervisor.native.send.assert_not_awaited()
    async with connect() as db:
        assert await (
            await db.execute("SELECT COUNT(*) FROM continuation_attempts")
        ).fetchone() == (0,)


@pytest.mark.parametrize(
    "turn",
    [
        {"id": "", "status": "failed"},
        {"id": "turn", "status": "unknown"},
        {"id": "a?b", "status": "completed"},
    ],
)
def test_native_snapshot_rejects_malformed_turn(turn):
    with pytest.raises(AppServerError):
        native.read_snapshot(
            {"thread": {"id": "thread", "kind": "codex", "hostId": "local"}, "turns": [turn]},
            "thread",
        )


@pytest.mark.parametrize("value", [None, [], "invalid"])
def test_native_result_rejects_non_object(value):
    with pytest.raises(AppServerError):
        native.decode_result(value, "request")


@pytest.mark.parametrize("close_fails", [False, True])
async def test_desktop_holds_account_lock_until_connection_closes(migrated_db, close_fails):
    from codex_account_manager.core.errors import TransactionError
    from codex_account_manager.core.operation_lock import OperationLock

    supervisor, server, source, ticket = setup_native()
    checked = []

    async def close():
        with pytest.raises(TransactionError):
            with OperationLock(migrated_db.data_dir / "account-operation.lock"):
                pass
        checked.append(True)
        if close_fails:
            raise RuntimeError("Synthetic connection close failure")

    server.aclose = close
    if close_fails:
        with pytest.raises(RuntimeError, match="Synthetic connection close failure"):
            await supervisor.run(ticket)
    else:
        await supervisor.run(ticket)
    assert checked == [True]
    with OperationLock(migrated_db.data_dir / "account-operation.lock"):
        pass


async def test_stalled_post_send_observation_releases_lock_without_resending(
    migrated_db, monkeypatch
):
    import asyncio

    from codex_account_manager.continuity import automation
    from codex_account_manager.core.operation_lock import OperationLock

    observation_timeout = asyncio.timeout(None)
    real_timeout = asyncio.timeout
    monkeypatch.setattr(
        automation.asyncio,
        "timeout",
        lambda seconds: (
            observation_timeout
            if seconds == automation.DESKTOP_OBSERVATION_SECONDS
            else real_timeout(seconds)
        ),
    )
    supervisor, server, source, ticket = setup_native()
    cancelled = asyncio.Event()
    calls = 0

    async def latest(_thread):
        nonlocal calls
        calls += 1
        if calls <= 2:
            return source
        try:
            observation_timeout.reschedule(asyncio.get_running_loop().time())
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    supervisor.native.latest_turn.side_effect = latest
    await asyncio.wait_for(supervisor.run(ticket), 2)
    assert cancelled.is_set()
    assert server.closed
    async with connect() as db:
        assert await (await db.execute("SELECT status FROM continuation_attempts")).fetchone() == (
            "submitted",
        )
    with OperationLock(migrated_db.data_dir / "account-operation.lock"):
        pass
    supervisor.native.latest_turn.side_effect = None
    supervisor.native.latest_turn.return_value = source
    await supervisor.run(ticket)
    supervisor.native.send.assert_awaited_once()


async def test_desktop_summary_uses_matching_structured_quota_error(migrated_db):
    supervisor, server, source, ticket = setup_native()
    server.latest = source
    summary = {**source, "error": {"message": "A localized limit message"}}
    supervisor.native.latest_turn.side_effect = [
        summary,
        summary,
        {"id": "next", "status": "inProgress"},
    ]
    await supervisor.run(ticket)
    supervisor.native.send.assert_awaited_once_with("thread", "last")
    assert (await supervisor.tracker.visible())[0].turn_status == "inProgress"


@pytest.mark.parametrize(
    "stored", [None, {"id": "old", "status": "failed"}, {"id": "last", "status": "inProgress"}]
)
async def test_desktop_summary_rejects_unmatched_stored_failure(migrated_db, stored):
    supervisor, server, source, ticket = setup_native()
    supervisor.native.latest_turn.return_value = {
        **source,
        "error": {"message": "usage limit exceeded"},
    }
    server.latest = stored
    await supervisor.run(ticket)
    supervisor.native.send.assert_not_awaited()


async def test_desktop_summary_does_not_infer_quota_from_error_message(migrated_db):
    supervisor, server, source, ticket = setup_native()
    summary = {**source, "error": {"message": "usage limit exceeded"}}
    supervisor.native.latest_turn.return_value = summary
    server.latest = {**source, "error": {"codexErrorInfo": "internalServerError"}}
    await supervisor.run(ticket)
    supervisor.native.send.assert_not_awaited()
