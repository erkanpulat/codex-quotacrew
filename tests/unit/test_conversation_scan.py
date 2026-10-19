from dataclasses import replace

from codex_account_manager.continuity import scan
from tests.integration.test_limit_continuation import _thread


def test_discovery_is_bounded_fair_and_does_not_rescan_unchanged_history(monkeypatch):
    monkeypatch.setattr(scan.time, "monotonic", lambda: 1000)
    monkeypatch.setattr(scan, "thread_revision", lambda t: (t.updated_at, (1, 1)))
    scanner = scan.ConversationScan()
    threads = [_thread(str(i), i) for i in range(45)]
    seen = []
    for expected in (20, 20, 5):
        selected = scanner.select(threads, [])
        assert len(selected) == expected
        assert not set(selected) & set(seen)
        seen.extend(selected)
        for tid in selected:
            scanner.succeeded(tid)
    assert len(seen) == 45
    assert scanner.select(threads, []) == []
    threads[0] = replace(threads[0], updated_at=9999)
    assert scanner.select(threads, []) == ["0"]


def test_failed_conversation_backs_off_without_delaying_tracked_sibling(monkeypatch):
    now = [1000]
    monkeypatch.setattr(scan.time, "monotonic", lambda: now[0])
    scanner = scan.ConversationScan()
    threads = [_thread("bad", 1), _thread("good", 2)]
    assert scanner.select(threads, ["bad", "good"]) == ["bad", "good"]
    scanner.failed("bad")
    scanner.succeeded("good")
    now[0] += 1
    assert scanner.select(threads, ["bad", "good"]) == ["good"]
    now[0] += 30
    assert scanner.select(threads, ["bad", "good"]) == ["bad", "good"]
    scanner.failed("bad")
    now[0] += 31
    assert scanner.select(threads, ["bad", "good"]) == ["good"]
    assert scanner.select([], []) == []
    assert not scanner.entries


def test_appended_local_history_invalidates_unchanged_thread_metadata(migrated_db):
    history = migrated_db.shared_codex_home / "sessions" / "test.jsonl"
    history.parent.mkdir(exist_ok=True)
    history.write_bytes(b"{}\n")
    thread = replace(_thread("thread", 1), path=str(history))
    scanner = scan.ConversationScan()
    assert scanner.select([thread], []) == ["thread"]
    scanner.succeeded("thread")
    assert scanner.select([thread], []) == []
    with history.open("ab") as stream:
        stream.write(b"{}\n")
    assert scanner.select([thread], []) == ["thread"]
