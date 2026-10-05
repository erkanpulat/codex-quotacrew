from unittest.mock import AsyncMock

import pytest

from codex_account_manager.adapters.interfaces import GoalInfo
from codex_account_manager.adapters.native_ide import (
    NativeIDE,
    OwnerProtocol,
    OwnerSnapshot,
    snapshot,
)
from codex_account_manager.adapters.native_ide import (
    _exchange as ide_exchange,
)
from codex_account_manager.continuity.automation import ContinuationSupervisor, ContinuationTicket
from codex_account_manager.continuity.tracking import goal_signature
from codex_account_manager.core.errors import AppServerError, OwnerNotFoundError
from codex_account_manager.storage.repositories import SettingsRepository
from tests.fakes import ExecutionServer


def state_message(**state_changes):
    return {
        "type": "broadcast",
        "method": "thread-stream-state-changed",
        "version": 11,
        "sourceClientId": "owner",
        "params": {
            "hostId": "local",
            "conversationId": "thread",
            "change": {
                "type": "snapshot",
                "revision": 1,
                "conversationState": {
                    "id": "thread",
                    "turns": [{"turnId": "last", "status": "failed"}],
                    "requests": [],
                    "threadRuntimeStatus": {"type": "idle"},
                    "threadGoal": None,
                    **state_changes,
                },
            },
        },
    }


@pytest.mark.parametrize(
    "field,value",
    [("version", 12), ("sourceClientId", "other"), ("params", []), ("type", "request")],
)
def test_wrong_owner_version_and_schema_are_rejected(field, value):
    message = state_message()
    message[field] = value
    with pytest.raises(AppServerError):
        snapshot(message, "thread", "owner")


@pytest.mark.parametrize(
    "changes",
    [
        {"requests": [{}]},
        {"requests": None},
        {"threadRuntimeStatus": {"type": "active"}},
        {"threadRuntimeStatus": None},
        {"unconfirmedTurnSubmissions": [{}]},
    ],
)
def test_pending_approval_unknown_runtime_and_uncertain_sends_block(changes):
    assert snapshot(state_message(**changes), "thread", "owner").blocked


def test_canonical_history_requires_its_latest_boundary():
    history = {
        "kind": "canonical",
        "history": {
            "islands": [{"entries": [{"value": "key"}], "newerBoundary": {"status": "exhausted"}}],
            "entitiesByKey": {"key": {"turnId": "canonical-turn", "status": "inProgress"}},
        },
    }
    result = snapshot(state_message(turnHistory=history), "thread", "owner")
    assert result.turn["id"] == "canonical-turn"
    history["history"]["islands"][0]["newerBoundary"]["status"] = "available"
    with pytest.raises(AppServerError):
        snapshot(state_message(turnHistory=history), "thread", "owner")


class Pipe:
    def __init__(self):
        self.sent = []
        self.incoming = []
        self.owner = "owner"
        self.state = state_message()

    def send(self, message):
        self.sent.append(message)
        method = message["method"]
        if message["type"] == "request":
            result = {"clientId": "test-client"} if method == "initialize" else {}
            if method == "thread-follower-start-turn":
                result = {"result": {"turn": {"id": "next-turn", "status": "inProgress"}}}
            self.incoming.append(
                {
                    "type": "response",
                    "method": method,
                    "requestId": message["requestId"],
                    "resultType": "success",
                    "handledByClientId": self.owner,
                    "result": result,
                }
            )
        elif message["params"]["following"]:
            self.incoming.append(self.state)

    def receive(self):
        return self.incoming.pop(0)


def test_owner_send_inherits_settings_and_targets_exact_owner():
    pipe = Pipe()
    protocol = OwnerProtocol(pipe)
    protocol.initialize()
    initial = protocol.inspect("thread")
    result = protocol.send("thread", initial, "message-id")
    assert result == {"id": "next-turn", "status": "inProgress"}
    send = pipe.sent[-1]
    assert send["targetClientId"] == "owner" and send["version"] == 2
    assert send["params"]["turnStart"]["context"] == {"inheritThreadSettings": True}
    request = send["params"]["turnStart"]["request"]
    assert set(request) == {"threadId", "clientUserMessageId", "input"}
    assert request["threadId"] == "thread" and request["clientUserMessageId"] == "message-id"
    assert "permissions and budget" in request["input"][0]["text"]
    assert request["input"][0]["text_elements"] == []


@pytest.mark.parametrize(
    "response_method,error",
    [
        ("thread-owner-discovery", "no-client-found"),
        (None, "no-client-found"),
        (None, "unavailable"),
        ("thread-owner-discovery", "unavailable"),
        ("unrelated", "no-client-found"),
    ],
)
def test_only_explicit_discovery_miss_is_safe_to_skip(response_method, error):
    pipe = Pipe()
    original = pipe.send

    def reply(message):
        original(message)
        pipe.incoming[-1].update(resultType="error", error=error)
        if response_method is None:
            pipe.incoming[-1].pop("method")
        else:
            pipe.incoming[-1]["method"] = response_method

    pipe.send = reply
    with pytest.raises(AppServerError) as caught:
        OwnerProtocol(pipe).request("thread-owner-discovery", {}, 1)
    assert isinstance(caught.value, OwnerNotFoundError) == (
        response_method in {None, "thread-owner-discovery"} and error == "no-client-found"
    )


@pytest.mark.parametrize("change", ["owner", "turn", "approval", "goal", "running"])
def test_changed_owner_or_work_is_never_sent(change):
    pipe = Pipe()
    protocol = OwnerProtocol(pipe)
    protocol.initialize()
    original = protocol.inspect("thread")
    if change == "owner":
        pipe.owner = "someone-else"
    elif change == "turn":
        pipe.state = state_message(turns=[{"turnId": "new", "status": "failed"}])
    elif change == "approval":
        pipe.state = state_message(requests=[{}])
    elif change == "goal":
        pipe.state = state_message(threadGoal={"objective": "changed"})
    else:
        pipe.state = state_message(turns=[{"turnId": "last", "status": "inProgress"}])
    with pytest.raises(AppServerError):
        protocol.send("thread", original, "message")
    assert not any(m["method"] == "thread-follower-start-turn" for m in pipe.sent)


async def test_ide_is_opt_in(migrated_db):
    supervisor = ContinuationSupervisor()
    assert not await supervisor.ide_enabled()
    await SettingsRepository().set("ide_continue", "true")
    assert await supervisor.ide_enabled()


@pytest.mark.parametrize("blocked", [False, True])
@pytest.mark.parametrize("closed_chat", [False, True])
async def test_ide_refresh_revalidates_new_owner_before_sending(
    migrated_db, tmp_path, blocked, closed_chat
):
    from dataclasses import replace

    from tests.integration.test_limit_continuation import _thread

    await SettingsRepository().set("ide_continue", "true")
    await SettingsRepository().set("ide_refresh", "true")
    server = ExecutionServer()
    server.latest = {
        "id": "last",
        "status": "failed",
        "error": {"codexErrorInfo": "usageLimitExceeded"},
    }
    threads = [replace(_thread("thread", 1), source="vscode", cwd=str(tmp_path))]
    if closed_chat:
        threads.insert(0, replace(threads[0], id="closed"))
    server.list_threads = AsyncMock(return_value=threads)
    server.is_desktop_thread = AsyncMock(return_value=False)
    supervisor = ContinuationSupervisor(factory=lambda: server)
    before = OwnerSnapshot("before", {"id": "last", "status": "failed"}, False, None)
    after = replace(before, owner_id="after")
    running = replace(after, turn={"id": "next", "status": "inProgress"}, blocked=True)
    supervisor.ide.wait_inspect = AsyncMock(side_effect=[before, after])
    supervisor.ide.inspect = AsyncMock(
        side_effect=(
            [OwnerNotFoundError("No local client owns this conversation.")] if closed_chat else []
        )
        + [replace(before, blocked=blocked), after, running]
    )
    supervisor.ide_session.needs_refresh = AsyncMock(return_value=True)
    supervisor.ide_session.refresh = AsyncMock()
    supervisor.ide.send = AsyncMock()
    await supervisor.run(
        ContinuationTicket("thread", "last", "acc-1", goal_signature(server.native), True, "before")
    )
    if blocked:
        supervisor.ide_session.refresh.assert_not_awaited()
        supervisor.ide.send.assert_not_awaited()
        return
    supervisor.ide_session.refresh.assert_awaited_once_with("acc-1", "thread", str(tmp_path))
    assert supervisor.ide.send.await_args.args[1].owner_id == "after"


async def test_presend_account_lock_contention_retains_unsent_ide_ticket(migrated_db):
    import asyncio

    from codex_account_manager.core.operation_lock import OperationLock
    from codex_account_manager.storage.database import connect

    await SettingsRepository().set("ide_continue", "true")
    supervisor = ContinuationSupervisor(factory=ExecutionServer)
    ticket = ContinuationTicket("thread", "last", "acc-1", None, True, "owner")
    await supervisor.save_pending([ticket])
    with OperationLock(migrated_db.data_dir / "account-operation.lock"):
        supervisor.launch_batch([ticket])
        await asyncio.gather(*tuple(supervisor.tasks))
    async with connect() as db:
        assert await (
            await db.execute("SELECT count(*) FROM pending_continuations")
        ).fetchone() == (1,)
        assert await (
            await db.execute("SELECT count(*) FROM continuation_attempts")
        ).fetchone() == (0,)


@pytest.mark.parametrize("ide", [False, True])
async def test_slow_owner_readiness_keeps_unsent_ticket_and_recovers_once(migrated_db, ide):
    import asyncio

    from codex_account_manager.core.errors import ConnectionNotReadyError
    from codex_account_manager.storage.database import connect

    await SettingsRepository().set("ide_continue", "true")
    server = ExecutionServer()
    limited = {"id": "last", "status": "failed", "error": {"codexErrorInfo": "usageLimitExceeded"}}
    server.latest = limited
    supervisor = ContinuationSupervisor(factory=lambda: server)
    ticket = ContinuationTicket(
        "thread", "last", "acc-1", goal_signature(server.native), True, "owner" if ide else None
    )
    await supervisor.save_pending([ticket])
    if ide:
        supervisor.ide.wait_inspect = AsyncMock(side_effect=ConnectionNotReadyError("not ready"))
        supervisor.ide.send = AsyncMock()
    else:
        supervisor.native.wait_latest_turn = AsyncMock(
            side_effect=ConnectionNotReadyError("not ready")
        )
        supervisor.native.send = AsyncMock()
    supervisor.launch_batch([ticket])
    await asyncio.gather(*tuple(supervisor.tasks))
    async with connect() as db:
        assert len(await (await db.execute("SELECT * FROM pending_continuations")).fetchall()) == 1
        assert not await (await db.execute("SELECT * FROM continuation_attempts")).fetchall()
    await supervisor.recover_pending(None)
    async with connect() as db:
        assert len(await (await db.execute("SELECT * FROM pending_continuations")).fetchall()) == 1
    running = {"id": "next", "status": "inProgress"}
    if ide:
        owner = OwnerSnapshot("owner", {"id": "last", "status": "failed"}, False, None)
        supervisor.ide.wait_inspect = AsyncMock(return_value=owner)
        supervisor.ide.inspect = AsyncMock(
            side_effect=[owner, OwnerSnapshot("owner", running, True, None)]
        )
        sender = supervisor.ide.send
    else:
        supervisor.native.wait_latest_turn = AsyncMock(return_value=limited)
        supervisor.native.latest_turn = AsyncMock(side_effect=[limited, running])
        sender = supervisor.native.send
    await supervisor.recover_pending("acc-1")
    await asyncio.gather(*tuple(supervisor.tasks))
    sender.assert_awaited_once()
    await supervisor.recover_pending("acc-1")
    sender.assert_awaited_once()
    async with connect() as db:
        assert not await (await db.execute("SELECT * FROM pending_continuations")).fetchall()


async def test_owner_transport_timeout_is_retryable_before_delivery(monkeypatch):
    from codex_account_manager.core.errors import ConnectionNotReadyError

    client = NativeIDE()
    client.inspect = AsyncMock(side_effect=TimeoutError())
    with pytest.raises(ConnectionNotReadyError):
        await client.wait_inspect("thread", timeout=0.01)


@pytest.mark.parametrize("code", [2, 109, 231, 232, 233])
async def test_windows_channel_reconnect_is_retried_only_for_reads(monkeypatch, code):
    import sys
    import threading

    if sys.platform != "win32":
        pytest.skip("Windows named pipe transport")
    import pywintypes
    import win32file

    from codex_account_manager.adapters import native_ide
    from codex_account_manager.core.errors import ConnectionNotReadyError

    def unavailable(*args):
        raise pywintypes.error(code, "CreateFile", "private OS details")

    monkeypatch.setattr(win32file, "CreateFile", unavailable)
    monkeypatch.setattr(native_ide, "_exchange", ide_exchange)
    with pytest.raises(ConnectionNotReadyError) as caught:
        native_ide._exchange("thread", None, None, threading.Event())
    assert "private" not in str(caught.value)
    client = NativeIDE()
    original = client.inspect
    ready = OwnerSnapshot("owner", {"id": "last", "status": "failed"}, False, None)
    calls = 0

    async def inspect(thread_id):
        nonlocal calls
        calls += 1
        return await original(thread_id) if calls == 1 else ready

    client.inspect = inspect
    assert await client.wait_inspect("thread", timeout=2) == ready
    assert calls == 2
    with pytest.raises(AppServerError) as caught:
        native_ide._exchange("thread", ready, "message", threading.Event())
    assert not isinstance(caught.value, ConnectionNotReadyError)
    assert "private" not in str(caught.value)


async def test_ide_continuation_is_claimed_once_and_tracks_next_turn(migrated_db):
    await SettingsRepository().set("ide_continue", "true")
    server = ExecutionServer()
    supervisor = ContinuationSupervisor(factory=lambda: server)
    limited = {"id": "last", "status": "failed", "error": {"codexErrorInfo": "usageLimitExceeded"}}
    owner = OwnerSnapshot("owner", {"id": "last", "status": "failed"}, False, None)
    running = OwnerSnapshot("owner", {"id": "next-turn", "status": "inProgress"}, True, None)
    server.latest_turn = AsyncMock(return_value=limited)
    supervisor.ide.inspect = AsyncMock(side_effect=[owner, owner, running])
    supervisor.ide.send = AsyncMock(return_value=running.turn)
    ticket = ContinuationTicket(
        "thread",
        "last",
        "acc-1",
        goal_signature(GoalInfo("thread", None, None, False)),
        True,
        "owner",
    )
    await supervisor.run(ticket)
    supervisor.ide.send.assert_awaited_once()
    assert not server.resume_calls and not server.turn_calls
    assert (await supervisor.tracker.visible())[0].turn_status == "inProgress"
    supervisor.ide.inspect.side_effect = None
    supervisor.ide.inspect.return_value = owner
    await supervisor.run(ticket)
    supervisor.ide.send.assert_awaited_once()


async def test_ambiguous_ide_delivery_is_not_retried(migrated_db):
    await SettingsRepository().set("ide_continue", "true")
    server = ExecutionServer()
    server.latest_turn = AsyncMock(
        return_value={
            "id": "last",
            "status": "failed",
            "error": {"codexErrorInfo": "usageLimitExceeded"},
        }
    )
    supervisor = ContinuationSupervisor(factory=lambda: server)
    supervisor.ide.inspect = AsyncMock(
        return_value=OwnerSnapshot("owner", {"id": "last", "status": "failed"}, False, None)
    )
    supervisor.ide.send = AsyncMock(side_effect=TimeoutError())
    ticket = ContinuationTicket(
        "thread",
        "last",
        "acc-1",
        goal_signature(GoalInfo("thread", None, None, False)),
        True,
        "owner",
    )
    await supervisor.run(ticket)
    await supervisor.run(ticket)
    supervisor.ide.send.assert_awaited_once()


async def test_ide_cancellation_signals_transport(monkeypatch):
    import asyncio
    import threading

    from codex_account_manager.adapters import native_ide

    started, stopped = threading.Event(), threading.Event()

    def exchange(_id, _expected, _message, cancel):
        started.set()
        cancel.wait(2)
        stopped.set()

    monkeypatch.setattr(native_ide, "_exchange", exchange)
    task = asyncio.create_task(NativeIDE().inspect("thread"))
    async with asyncio.timeout(2):
        while not started.is_set():
            await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with asyncio.timeout(2):
        while not stopped.is_set():
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("status", ["failed", "completed", "interrupted", "inProgress"])
def test_system_error_only_allows_a_failed_turn_for_further_limit_verification(status):
    result = snapshot(
        state_message(
            threadRuntimeStatus={"type": "systemError"},
            turns=[{"turnId": "last", "status": status}],
        ),
        "thread",
        "owner",
    )
    assert result.blocked is (status != "failed")
    assert snapshot(
        state_message(threadRuntimeStatus={"type": "systemError"}, requests=[{}]),
        "thread",
        "owner",
    ).blocked


@pytest.mark.parametrize("error", ["usageLimitExceeded", "other", None])
async def test_system_error_preparation_requires_same_turn_structured_quota(migrated_db, error):
    from codex_account_manager.core.errors import DesktopContinuationRequired

    await SettingsRepository().set("ide_continue", "true")
    server = ExecutionServer()
    server.require_headless_compatible = AsyncMock(side_effect=DesktopContinuationRequired("local"))
    server.latest_turn = AsyncMock(
        return_value={"id": "last", "status": "failed", "error": {"codexErrorInfo": error}}
    )
    supervisor = ContinuationSupervisor(factory=lambda: server)
    supervisor.ide.inspect = AsyncMock(
        return_value=snapshot(
            state_message(threadRuntimeStatus={"type": "systemError"}), "thread", "owner"
        )
    )
    ticket = await supervisor.prepare("thread")
    assert bool(ticket) is (error == "usageLimitExceeded")
    assert not server.turn_calls


async def test_three_owner_conversations_survive_restart_and_send_once_each(migrated_db):
    import asyncio

    from codex_account_manager.storage.repositories import EventRepository

    await SettingsRepository().set("ide_continue", "true")
    server = ExecutionServer()
    server.latest_turn = AsyncMock(
        return_value={
            "id": "last",
            "status": "failed",
            "error": {"codexErrorInfo": "usageLimitExceeded"},
        }
    )
    supervisor = ContinuationSupervisor(factory=lambda: server)
    sent = set()

    async def inspect(tid):
        return OwnerSnapshot(
            "current-" + tid,
            {
                "id": "next" if tid in sent else "last",
                "status": "inProgress" if tid in sent else "failed",
            },
            tid in sent,
            None,
        )

    async def send(tid, owner, message_id):
        assert owner.owner_id == "current-" + tid
        assert message_id and tid not in sent
        sent.add(tid)
        return {"id": "next", "status": "inProgress"}

    supervisor.ide.inspect = AsyncMock(side_effect=inspect)
    supervisor.ide.send = AsyncMock(side_effect=send)
    tickets = [
        ContinuationTicket(
            tid,
            "last",
            "acc-1",
            goal_signature(GoalInfo(tid, None, None, False)),
            True,
            "before-restart-" + tid,
        )
        for tid in ("desktop-one", "desktop-two", "vscode")
    ]
    supervisor.launch_batch(tickets)
    await asyncio.gather(*tuple(supervisor.tasks))
    assert sent == {"desktop-one", "desktop-two", "vscode"}
    assert not server.turn_calls and not server.resume_calls
    for tid in sent:
        events = await EventRepository().continuation_history(tid)
        assert events[0]["payload"] == {"state": "ide_submitted", "stage": "execution"}


async def test_two_desktop_jobs_continue_while_ide_reconnects_then_recovers(migrated_db):
    import asyncio

    from codex_account_manager.core.errors import ConnectionNotReadyError
    from codex_account_manager.storage.database import connect

    await SettingsRepository().set("ide_continue", "true")
    server = ExecutionServer()
    limited = {"id": "last", "status": "failed", "error": {"codexErrorInfo": "usageLimitExceeded"}}
    server.latest_turn = AsyncMock(return_value=limited)
    supervisor = ContinuationSupervisor(factory=lambda: server)
    sent = []

    async def desktop_turn(tid):
        return {"id": "next", "status": "inProgress"} if tid in sent else limited

    async def send(tid, *_args):
        sent.append(tid)

    async def ide_turn(tid):
        return OwnerSnapshot("owner", await desktop_turn(tid), tid in sent, None)

    supervisor.native.latest_turn = AsyncMock(side_effect=desktop_turn)
    supervisor.native.send = AsyncMock(side_effect=send)
    supervisor.ide.wait_inspect = AsyncMock(side_effect=ConnectionNotReadyError("restarting"))
    supervisor.ide.inspect = AsyncMock(side_effect=ide_turn)
    supervisor.ide.send = AsyncMock(side_effect=send)
    tickets = [
        ContinuationTicket(
            tid,
            "last",
            "acc-1",
            goal_signature(GoalInfo(tid, None, None, False)),
            True,
            "owner" if tid == "ide" else None,
        )
        for tid in ("desktop-one", "ide", "desktop-two")
    ]
    await supervisor.save_pending(tickets)
    supervisor.launch_batch(tickets)
    await asyncio.gather(*tuple(supervisor.tasks))
    assert sent == ["desktop-one", "desktop-two"]
    async with connect() as db:
        rows = await (await db.execute("SELECT thread_id FROM pending_continuations")).fetchall()
        assert rows == [("ide",)]
    supervisor.ide.wait_inspect.side_effect = ide_turn
    await supervisor.recover_pending("acc-1")
    await asyncio.gather(*tuple(supervisor.tasks))
    assert sent == ["desktop-one", "desktop-two", "ide"]
    await supervisor.recover_pending("acc-1")
    assert len(sent) == 3
    assert {row.thread_id for row in await supervisor.tracker.visible()} == set(sent)


async def test_owner_change_during_post_restart_verification_blocks_send(migrated_db):
    from codex_account_manager.storage.repositories import EventRepository

    await SettingsRepository().set("ide_continue", "true")
    server = ExecutionServer()
    server.latest_turn = AsyncMock(
        return_value={
            "id": "last",
            "status": "failed",
            "error": {"codexErrorInfo": "usageLimitExceeded"},
        }
    )
    supervisor = ContinuationSupervisor(factory=lambda: server)
    supervisor.ide.inspect = AsyncMock(
        side_effect=[
            OwnerSnapshot(owner, {"id": "last", "status": "failed"}, False, None)
            for owner in ("after-restart", "changed-again")
        ]
    )
    supervisor.ide.send = AsyncMock()
    await supervisor.run(
        ContinuationTicket(
            "thread",
            "last",
            "acc-1",
            goal_signature(GoalInfo("thread", None, None, False)),
            True,
            "original",
        )
    )
    supervisor.ide.send.assert_not_awaited()
    events = await EventRepository().continuation_history("thread")
    assert events[0]["payload"] == {"state": "needs_user", "stage": "verification"}


async def test_batch_continues_after_one_failed_worker_without_logging_raw_error(migrated_db):
    import asyncio

    from codex_account_manager.storage.repositories import EventRepository

    supervisor = ContinuationSupervisor()

    async def run(ticket):
        if ticket.thread_id == "one":
            raise RuntimeError("private-conversation-text")

    supervisor.run = AsyncMock(side_effect=run)
    tickets = [ContinuationTicket(tid, "last") for tid in ("one", "two", "three")]
    supervisor.launch_batch(tickets)
    await asyncio.gather(*tuple(supervisor.tasks))
    assert supervisor.run.await_count == 3
    events = await EventRepository().continuation_history("one")
    assert events[0]["payload"] == {"state": "needs_user", "stage": "connection"}
    assert "private-conversation-text" not in str(events)


async def test_submitted_but_failed_next_turn_requires_attention_without_resending(migrated_db):
    from codex_account_manager.storage.repositories import EventRepository

    await SettingsRepository().set("ide_continue", "true")
    server = ExecutionServer()
    original = OwnerSnapshot("owner", {"id": "last", "status": "failed"}, False, None)
    failed = OwnerSnapshot("owner", {"id": "next", "status": "failed"}, False, None)
    server.latest_turn = AsyncMock(
        side_effect=[
            {**original.turn, "error": {"codexErrorInfo": "usageLimitExceeded"}},
            {**original.turn, "error": {"codexErrorInfo": "usageLimitExceeded"}},
            {**failed.turn, "error": {"codexErrorInfo": "other"}},
        ]
    )
    supervisor = ContinuationSupervisor(factory=lambda: server)
    supervisor.ide.inspect = AsyncMock(side_effect=[original, original, failed])
    supervisor.ide.send = AsyncMock(return_value=failed.turn)
    await supervisor.run(
        ContinuationTicket(
            "thread",
            "last",
            "acc-1",
            goal_signature(GoalInfo("thread", None, None, False)),
            True,
            "owner",
        )
    )
    supervisor.ide.send.assert_awaited_once()
    events = await EventRepository().continuation_history("thread")
    assert events[0]["payload"] == {"state": "needs_user", "stage": "observation"}
