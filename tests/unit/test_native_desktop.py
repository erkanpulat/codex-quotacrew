import json
from unittest.mock import AsyncMock

import pytest

from codex_account_manager.adapters import native_desktop as native
from codex_account_manager.adapters.interfaces import GoalInfo
from codex_account_manager.continuity.automation import ContinuationSupervisor, ContinuationTicket
from codex_account_manager.continuity.tracking import goal_signature
from codex_account_manager.core.errors import (
    AppServerError,
    ConnectionNotReadyError,
    NativeOperationRejectedError,
)
from codex_account_manager.storage.database import connect
from tests.fakes import ExecutionServer, account_identity

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


async def test_delivery_has_time_for_desktop_history_loading(monkeypatch):
    budgets = []

    def exchange(payload, cancelled, timeout):
        budgets.append(timeout)
        result = reply({"threadId": "thread"})
        result["id"] = payload["id"]
        return result

    monkeypatch.setattr(native, "_exchange", exchange)
    client = native.NativeDesktop()
    await client.call("thread", "turn")
    await client.send("thread", "turn")
    assert budgets == [15.0, 60.0]


async def test_native_channel_serializes_overlapping_calls(monkeypatch):
    import asyncio
    import threading
    import time

    started = threading.Event()
    release = threading.Event()
    active = 0
    maximum = 0

    def exchange(payload, _cancelled, _timeout):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        started.set()
        while not release.is_set():
            time.sleep(0.001)
        active -= 1
        result = reply({"thread": {"id": "thread"}})
        result["id"] = payload["id"]
        return result

    monkeypatch.setattr(native, "_exchange", exchange)
    client = native.NativeDesktop()
    first = asyncio.create_task(client.call("thread", "turn"))
    await asyncio.to_thread(started.wait, 1)
    second = asyncio.create_task(client.call("thread", "turn"))
    await asyncio.sleep(0.02)
    assert maximum == 1
    release.set()
    await asyncio.gather(first, second)


@pytest.mark.parametrize("activity", ["active", "idle", "notLoaded"])
async def test_native_activity_uses_only_bounded_list_metadata(activity):
    client = native.NativeDesktop()
    data = {
        "schemaVersion": 4,
        "pinnedThreads": [],
        "threads": [{"id": "thread", "kind": "codex", "hostId": "local", "status": activity}],
    }
    client._call = AsyncMock(return_value=data)
    assert await client.thread_status("thread") == activity
    client._call.assert_awaited_once_with(
        "thread", "account-manager-observation", "list_threads", {"limit": 50}
    )


@pytest.mark.parametrize("fault", ["schema", "missing", "remote", "chatgpt", "unknown", "conflict"])
async def test_native_activity_rejects_unverified_identity(fault):
    item = {"id": "thread", "kind": "codex", "hostId": "local", "status": "idle"}
    data = {"schemaVersion": 4, "pinnedThreads": [], "threads": [item]}
    if fault == "schema":
        data["schemaVersion"] = 5
    elif fault == "missing":
        data["threads"] = []
    elif fault == "remote":
        item["hostId"] = "remote"
    elif fault == "chatgpt":
        item["kind"] = "chatgpt"
    elif fault == "unknown":
        item["status"] = "unknown"
    else:
        data["pinnedThreads"] = [{**item, "status": "active"}]
    client = native.NativeDesktop()
    client._call = AsyncMock(return_value=data)
    with pytest.raises(AppServerError):
        await client.thread_status("thread")


async def test_desktop_continuation_does_not_navigate_renderer(migrated_db, monkeypatch):
    from codex_account_manager.platform import windows

    supervisor, server, source, ticket = setup_native()
    events = []
    monkeypatch.setattr(windows, "open_conversation", lambda tid: events.append(("open", tid)))

    async def ready(tid):
        events.append(("ready", tid))
        return source

    supervisor.native.wait_latest_turn = ready
    supervisor.native.latest_turn.side_effect = [source, {"id": "next", "status": "inProgress"}]
    await supervisor.run(ticket)
    supervisor.native.send.assert_awaited_once_with("thread", "last")
    assert events == [("ready", "thread")]


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
    supervisor.native.thread_status = AsyncMock(return_value="idle")
    ticket = ContinuationTicket(
        "thread",
        "last",
        "acc-1",
        goal_signature(GoalInfo("thread", None, None, False)),
        True,
        owner_key=account_identity("acc-1").fingerprint,
    )
    return supervisor, server, source, ticket


async def test_verified_lifecycle_does_not_require_native_history_loading():
    server = ExecutionServer()
    server.verified_stored_turn = AsyncMock(return_value=server.latest)
    client = native.NativeDesktop()
    client.latest_turn = AsyncMock(side_effect=ConnectionNotReadyError("native unavailable"))

    result = await native.verified_desktop_turn(client, server, "thread", prefer_stored=True)

    assert result == server.latest
    client.latest_turn.assert_not_awaited()


@pytest.mark.parametrize("status", ["inProgress", "completed", "interrupted"])
async def test_stored_interruption_requires_desktop_liveness(status):
    server = ExecutionServer()
    server.verified_stored_turn = AsyncMock(return_value={"id": "turn", "status": "interrupted"})
    live = {"id": "turn", "status": status}
    client = native.NativeDesktop()
    client.latest_turn = AsyncMock(return_value=live)

    assert await native.verified_desktop_turn(client, server, "thread", prefer_stored=True) == live
    client.latest_turn.assert_awaited_once_with("thread")


async def test_stored_interruption_is_unknown_when_desktop_cannot_be_read():
    server = ExecutionServer()
    server.verified_stored_turn = AsyncMock(return_value={"id": "turn", "status": "interrupted"})
    client = native.NativeDesktop()
    client.latest_turn = AsyncMock(side_effect=ConnectionNotReadyError("native unavailable"))

    with pytest.raises(ConnectionNotReadyError):
        await native.verified_desktop_turn(client, server, "thread", prefer_stored=True)


async def test_verified_empty_history_does_not_trigger_native_loading():
    server = ExecutionServer()
    server.verified_stored_turn = AsyncMock(return_value=None)
    client = native.NativeDesktop()
    client.latest_turn = AsyncMock(
        side_effect=AssertionError("No native loading for empty history")
    )
    assert await native.verified_desktop_turn(client, server, "thread", prefer_stored=True) is None


async def test_unverified_history_size_cannot_fall_back_to_native_loading():
    server = ExecutionServer()
    server.require_bounded_desktop_history = AsyncMock(side_effect=AppServerError("unknown size"))
    client = native.NativeDesktop()
    client.latest_turn = AsyncMock(return_value=server.latest)
    with pytest.raises(AppServerError, match="unknown size"):
        await native.verified_desktop_turn(client, server, "thread", prefer_stored=True)
    client.latest_turn.assert_not_awaited()


async def test_native_read_rejection_cannot_prove_delivery_readiness():
    server = ExecutionServer()
    server.verified_stored_turn = AsyncMock(return_value=server.latest)
    client = native.NativeDesktop()
    client.latest_turn = AsyncMock(
        side_effect=NativeOperationRejectedError("Desktop rejected the native operation.")
    )
    with pytest.raises(NativeOperationRejectedError):
        await native.verified_desktop_turn(client, server, "thread")
    server.verified_stored_turn.assert_not_awaited()


async def test_startup_generic_rejection_waits_for_native_readiness():
    client = native.NativeDesktop()
    client.call = AsyncMock(
        side_effect=[
            NativeOperationRejectedError("Codex app tool request failed"),
            {
                "thread": {"id": "thread", "kind": "codex", "hostId": "local"},
                "turns": [{"id": "last", "status": "failed"}],
            },
        ]
    )
    assert (await client.wait_latest_turn("thread"))["id"] == "last"
    assert all(not call.kwargs.get("send") for call in client.call.await_args_list)


async def test_generic_readiness_rejection_expires_without_claim(migrated_db, monkeypatch):
    supervisor, server, source, ticket = setup_native()
    server.verified_stored_turn = AsyncMock(return_value=source)
    supervisor.native.latest_turn = AsyncMock(side_effect=NativeOperationRejectedError("not ready"))
    wait = supervisor.native.wait_latest_turn
    supervisor.native.wait_latest_turn = lambda tid: wait(tid, timeout=0.01)
    with pytest.raises(ConnectionNotReadyError):
        await supervisor.run(ticket)
    supervisor.native.send.assert_not_awaited()
    async with connect() as db:
        assert not await (await db.execute("SELECT * FROM continuation_attempts")).fetchall()


async def test_generic_send_rejection_is_not_replayed():
    client = native.NativeDesktop()
    client.call = AsyncMock(
        side_effect=NativeOperationRejectedError("Codex app tool request failed")
    )
    with pytest.raises(NativeOperationRejectedError):
        await client.send("thread", "last")
    assert client.call.await_count == 1


@pytest.mark.parametrize("old_status", [None, "history_too_large", "uncertain"])
async def test_large_goal_opens_without_native_loading(migrated_db, monkeypatch, old_status):
    from dataclasses import replace

    from codex_account_manager.core.errors import ConversationHistoryTooLargeError
    from codex_account_manager.platform import windows

    supervisor, server, source, ticket = setup_native()
    server.native = GoalInfo("thread", "finish", "active", True, 2000, 100)
    ticket = replace(ticket, goal_signature=goal_signature(server.native))
    server.verified_stored_turn = AsyncMock(return_value=source)
    server.require_bounded_desktop_history = AsyncMock(
        side_effect=ConversationHistoryTooLargeError("too large")
    )
    supervisor.native.wait_latest_turn = AsyncMock(side_effect=AssertionError("No history load"))
    supervisor.native.latest_turn = AsyncMock(side_effect=AssertionError("No history load"))
    if old_status:
        async with connect() as db:
            await db.execute(
                "INSERT INTO continuation_attempts VALUES (?, ?, ?, ?)",
                ("thread", "last", "prior", old_status),
            )
            await db.commit()
    opened = []

    def open_goal(tid):
        opened.append(tid)
        server.verified_stored_turn.return_value = {"id": "new", "status": "inProgress"}
        server.latest = {"id": "new", "status": "inProgress"}
        supervisor.native.thread_status.return_value = "active"

    monkeypatch.setattr(windows, "open_conversation", open_goal)
    await supervisor.run(ticket)
    if supervisor.goal_observer.tasks:
        import asyncio

        await asyncio.wait_for(asyncio.gather(*supervisor.goal_observer.tasks.values()), 2)
    await supervisor.run(ticket)
    assert opened == ([] if old_status == "uncertain" else ["thread"])
    supervisor.native.send.assert_not_awaited()
    if opened:
        assert (await supervisor.tracker.visible())[0].turn_status == "inProgress"


async def test_large_goal_preparation_releases_only_unsent_size_block(migrated_db):
    from codex_account_manager.core.errors import (
        ConversationHistoryTooLargeError,
        DesktopContinuationRequired,
    )

    supervisor, server, source, _ticket = setup_native()
    server.native = GoalInfo("thread", "finish", "usageLimited", True, 2000, 100)
    server.require_headless_compatible = AsyncMock(
        side_effect=DesktopContinuationRequired("Desktop owns this work")
    )
    server.is_desktop_thread = AsyncMock(return_value=True)
    server.verified_stored_turn = AsyncMock(return_value=source)
    server.require_bounded_desktop_history = AsyncMock(
        side_effect=ConversationHistoryTooLargeError("too large")
    )
    async with connect() as db:
        await db.execute(
            "INSERT INTO continuation_attempts VALUES ('thread','last','old','history_too_large')"
        )
        await db.commit()
    ticket = await supervisor.prepare("thread")
    assert ticket is not None and ticket.desktop
    async with connect() as db:
        assert not await (await db.execute("SELECT * FROM continuation_attempts")).fetchall()
    supervisor.native.send.assert_not_awaited()


@pytest.mark.parametrize("old_status", ["history_too_large", "uncertain", "submitted", "sending"])
async def test_newly_bounded_history_releases_only_unsent_size_block(migrated_db, old_status):
    supervisor, _server, _source, ticket = setup_native()
    async with connect() as db:
        await db.execute(
            "INSERT INTO continuation_attempts VALUES ('thread', 'last', 'prior', ?)",
            (old_status,),
        )
        await db.commit()

    async def send(*_args):
        supervisor.native.latest_turn.return_value = {"id": "next", "status": "inProgress"}
        return {"accepted": True}

    supervisor.native.send.side_effect = send
    await supervisor.run(ticket)
    await supervisor.run(ticket)
    if old_status == "history_too_large":
        supervisor.native.send.assert_awaited_once_with("thread", "last")
    else:
        supervisor.native.send.assert_not_awaited()
    async with connect() as db:
        statuses = await (await db.execute("SELECT status FROM continuation_attempts")).fetchall()
    assert statuses == [("submitted" if old_status == "history_too_large" else old_status,)]


async def test_rejected_native_read_without_lifecycle_stays_unknown():
    server = ExecutionServer()
    client = native.NativeDesktop()
    client.latest_turn = AsyncMock(
        side_effect=NativeOperationRejectedError("Desktop rejected the native operation.")
    )
    with pytest.raises(AppServerError, match="Desktop rejected"):
        await native.verified_desktop_turn(client, server, "thread")


async def test_native_identity_mismatch_cannot_use_stored_fallback():
    server = ExecutionServer()
    server.verified_stored_turn = AsyncMock(return_value=server.latest)
    client = native.NativeDesktop()
    client.call = AsyncMock(
        return_value={
            "thread": {"id": "different", "kind": "codex", "hostId": "local"},
            "turns": [{"id": "turn", "status": "completed"}],
        }
    )
    with pytest.raises(AppServerError):
        await native.verified_desktop_turn(client, server, "thread")


async def test_oversized_history_never_reaches_native_loading(migrated_db):
    from codex_account_manager.core.errors import ConversationHistoryTooLargeError

    supervisor, server, _source, ticket = setup_native()
    server.require_bounded_desktop_history = AsyncMock(
        side_effect=ConversationHistoryTooLargeError("too large")
    )
    supervisor.native.wait_latest_turn = AsyncMock()
    await supervisor.run(ticket)
    supervisor.native.wait_latest_turn.assert_not_awaited()
    supervisor.native.send.assert_not_awaited()
    async with connect() as db:
        rows = await (await db.execute("SELECT status FROM continuation_attempts")).fetchall()
    assert rows == [("history_too_large",)]


async def test_goal_resumes_itself_without_duplicate_continuation_message(migrated_db):
    from dataclasses import replace

    supervisor, server, source, ticket = setup_native()
    server.native = GoalInfo("thread", "finish", "usageLimited", True, 2000, 100)
    ticket = replace(ticket, goal_signature=goal_signature(server.native))
    running = {"id": "goal-started", "status": "inProgress"}
    supervisor.native.wait_latest_turn = AsyncMock(return_value=source)
    supervisor.native.latest_turn.side_effect = [source, running]
    await supervisor.run(ticket)
    assert len(server.reactivations) == 1
    supervisor.native.send.assert_not_awaited()
    assert (await supervisor.tracker.visible())[0].turn_status == "inProgress"
    async with connect() as db:
        assert not await (await db.execute("SELECT * FROM continuation_attempts")).fetchall()


async def test_goal_activation_opens_once_without_sending_a_message(migrated_db, monkeypatch):
    from dataclasses import replace

    from codex_account_manager.platform import windows

    supervisor, server, source, ticket = setup_native()
    server.native = GoalInfo("thread", "finish", "active", True, 2000, 100)
    ticket = replace(ticket, goal_signature=goal_signature(server.native))
    supervisor.native.wait_latest_turn = AsyncMock(return_value=source)
    supervisor.native.latest_turn = AsyncMock(return_value=source)
    opened = []

    def open_thread(tid):
        opened.append(tid)
        supervisor.native.latest_turn.return_value = {"id": "resumed-goal", "status": "inProgress"}
        server.latest = {"id": "resumed-goal", "status": "inProgress"}
        supervisor.native.thread_status.return_value = "active"

    monkeypatch.setattr(windows, "open_conversation", open_thread)
    await supervisor.run(ticket)
    import asyncio

    await asyncio.wait_for(asyncio.gather(*supervisor.goal_observer.tasks.values()), 2)
    assert opened == ["thread"]
    supervisor.native.send.assert_not_awaited()
    assert (await supervisor.tracker.visible())[0].turn_status == "inProgress"
    await supervisor.run(ticket)
    assert opened == ["thread"]
    supervisor.native.send.assert_not_awaited()


async def test_opened_goal_does_not_hold_account_lock_while_waiting(migrated_db, monkeypatch):
    import asyncio
    from dataclasses import replace

    from codex_account_manager.continuity import desktop_goal
    from codex_account_manager.core.operation_lock import OperationLock
    from codex_account_manager.platform import windows

    supervisor, server, source, ticket = setup_native()
    server.native = GoalInfo("thread", "finish", "active", True, 2000, 100)
    server.latest = source
    ticket = replace(ticket, goal_signature=goal_signature(server.native))
    opened = []
    monkeypatch.setattr(windows, "open_conversation", lambda tid: opened.append(tid))

    monkeypatch.setattr(desktop_goal, "POLL_SECONDS", 999)
    checked = asyncio.Event()
    probe = supervisor.goal_observer._probe

    async def observe(*args, **kwargs):
        result = await probe(*args, **kwargs)
        checked.set()
        return result

    supervisor.goal_observer._probe = observe
    await supervisor.run(ticket)
    await asyncio.wait_for(checked.wait(), 2)
    with OperationLock(migrated_db.data_dir / "account-operation.lock"):
        assert supervisor.goal_observer.tasks
    await supervisor.stop()


async def test_goal_already_resumed_before_worker_is_observed_without_input(migrated_db):
    from dataclasses import replace

    supervisor, server, source, ticket = setup_native()
    server.native = GoalInfo("thread", "finish", "active", True, 2000, 100)
    ticket = replace(ticket, goal_signature=goal_signature(server.native))
    supervisor.native.wait_latest_turn = AsyncMock(
        return_value={"id": "automatic", "status": "inProgress"}
    )
    await supervisor.run(ticket)
    supervisor.native.send.assert_not_awaited()
    assert (await supervisor.tracker.visible())[0].turn_status == "inProgress"


async def test_continuation_delivery_never_uses_server_fallback_for_native_send(migrated_db):
    supervisor, server, source, ticket = setup_native()
    supervisor.native.wait_latest_turn = AsyncMock(
        side_effect=ConnectionNotReadyError("native unavailable")
    )

    with pytest.raises(ConnectionNotReadyError):
        await supervisor.run(ticket)

    supervisor.native.send.assert_not_awaited()
    assert not server.turn_calls


async def test_native_delivery_stays_in_desktop_and_tracks_next_account_limit(migrated_db):
    supervisor, server, source, ticket = setup_native()
    running = {"id": "native-next", "status": "inProgress"}
    supervisor.native.latest_turn.side_effect = [source, source, running]
    await supervisor.run(ticket)
    supervisor.native.send.assert_awaited_once_with("thread", "last")
    assert not server.resume_calls and not server.turn_calls
    assert (await supervisor.tracker.visible())[0].turn_status == "inProgress"
    await supervisor.tracker.observe(
        account_identity("acc-1").fingerprint,
        "thread",
        {**source, "id": "native-next"},
        GoalInfo("thread", None, None, False),
        limited=True,
    )
    assert (await supervisor.tracker.limited(account_identity("acc-1").fingerprint))[0][
        1
    ] == "native-next"
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
    assert not await supervisor.tracker.limited(account_identity("acc-1").fingerprint)


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
        ticket = ContinuationTicket(
            "thread",
            "last",
            "other",
            ticket.goal_signature,
            True,
            owner_key=account_identity("other").fingerprint,
        )
    elif fault == "new_turn":
        supervisor.native.latest_turn.return_value = {**source, "id": "new"}
    elif fault == "running":
        supervisor.native.latest_turn.return_value = {**source, "status": "inProgress"}
    elif fault == "goal":
        ticket = ContinuationTicket(
            "thread",
            "last",
            "acc-1",
            "changed",
            True,
            owner_key=account_identity("acc-1").fingerprint,
        )
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
@pytest.mark.parametrize("modern", [False, True])
def test_native_transport_bounds_and_closes_all_handles(monkeypatch, fault, modern):
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

    legacy = "codex-browser-use-" + "a" * 36
    current = "codex-app-tools-" + "b" * 36
    opened_paths = []

    def open_pipe(path, *args):
        opened_paths.append(path)
        return handle

    monkeypatch.setattr(
        native,
        "os",
        SimpleNamespace(
            name="nt",
            environ={"CODEX_APP_TOOLS_PIPE_PATH": "\\\\.\\pipe\\" + legacy},
            listdir=lambda _: [legacy, current] if modern else [legacy],
        ),
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
            CreateFile=open_pipe,
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
    assert opened_paths[0] == "\\\\.\\pipe\\" + (current if modern else legacy)


async def test_native_readiness_retries_reads_only():
    client = native.NativeDesktop()
    client.latest_turn = AsyncMock(
        side_effect=[ConnectionNotReadyError("starting"), {"id": "turn"}]
    )
    client.send = AsyncMock()
    assert await client.wait_latest_turn("thread") == {"id": "turn"}
    assert client.latest_turn.await_count == 2
    client.send.assert_not_awaited()


async def test_native_readiness_timeout_does_not_send():
    client = native.NativeDesktop()
    client.latest_turn = AsyncMock(side_effect=ConnectionNotReadyError("starting"))
    client.send = AsyncMock()
    with pytest.raises(AppServerError, match="ready"):
        await client.wait_latest_turn("thread", timeout=0.01)
    client.send.assert_not_awaited()


async def test_protocol_failure_does_not_loop_as_connection_warmup():
    client = native.NativeDesktop()
    client.latest_turn = AsyncMock(side_effect=AppServerError("unsupported schema"))
    with pytest.raises(AppServerError, match="unsupported schema"):
        await client.wait_latest_turn("thread", timeout=0.01)
    assert client.latest_turn.await_count == 1


def test_unknown_desktop_schema_is_not_used_to_authorize_input():
    data = {
        "schemaVersion": 2,
        "thread": {"id": "thread", "kind": "codex", "hostId": "local"},
        "turns": [{"id": "turn", "status": "failed"}],
    }
    with pytest.raises(AppServerError, match="schema"):
        native.read_snapshot(data, "thread")


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
    ticket = ContinuationTicket(
        "thread",
        "last",
        "acc-1",
        goal_signature(original),
        True,
        owner_key=account_identity("acc-1").fingerprint,
    )
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
