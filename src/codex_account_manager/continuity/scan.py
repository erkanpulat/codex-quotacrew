"""Session-local incremental discovery; no conversation contents are cached."""

from __future__ import annotations

import time
from dataclasses import dataclass

from codex_account_manager.core.files import resolved_path
from codex_account_manager.domain.models import ThreadInfo

ACTIVE_THREAD_STATES = frozenset({"active", "inProgress", "awaitingUser", "paused"})


def thread_revision(thread: ThreadInfo) -> tuple:
    stamp = None
    if thread.path and thread.history_mode != "paginated":
        try:
            path = resolved_path(thread.path)
            from codex_account_manager.core.paths import paths

            if path.is_relative_to(resolved_path(paths.shared_codex_home / "sessions")):
                stat = path.stat()
                stamp = (stat.st_size, stat.st_mtime_ns)
        except (OSError, ValueError):
            pass
    return thread.updated_at, thread.recency_at, thread.status, stamp


@dataclass
class ScanEntry:
    revision: tuple
    retry_at: float = 0
    failures: int = 0


class ConversationScan:
    def __init__(self) -> None:
        self.entries: dict[str, ScanEntry] = {}

    def select(self, threads: list[ThreadInfo], tracked: list[str]) -> list[str]:
        now = time.monotonic()
        listed = {thread.id: thread for thread in threads}
        self.entries = {
            key: value for key, value in self.entries.items() if key in listed or key in tracked
        }
        active = [t.id for t in threads if t.status in ACTIVE_THREAD_STATES]
        priority = set(tracked) | set(active)
        for thread_id in dict.fromkeys([*tracked, *listed]):
            thread = listed.get(thread_id)
            revision = thread_revision(thread) if thread else (None, None, None, None)
            entry = self.entries.get(thread_id)
            changed = entry is None or entry.revision != revision
            if changed:
                self.entries[thread_id] = ScanEntry(revision)
        recent = sorted(threads, key=lambda t: t.recency_at or t.updated_at or 0, reverse=True)[:20]
        uncached = [t.id for t in recent if self.entries[t.id].revision[-1] is None]
        priority.update(uncached)
        selected = []
        background = sorted(
            (t for t in threads if t.id not in priority),
            key=lambda t: (self.entries[t.id].retry_at, -(t.recency_at or t.updated_at or 0)),
        )
        background_count = 0
        for thread_id in dict.fromkeys([*tracked, *active, *uncached, *(t.id for t in background)]):
            entry = self.entries[thread_id]
            if entry.failures and now < entry.retry_at:
                continue
            if thread_id in priority:
                selected.append(thread_id)
            elif now >= entry.retry_at and background_count < 20:
                selected.append(thread_id)
                background_count += 1
        return selected

    def succeeded(self, thread_id: str) -> None:
        entry = self.entries[thread_id]
        entry.failures = 0
        entry.retry_at = time.monotonic() + 300

    def failed(self, thread_id: str) -> None:
        entry = self.entries[thread_id]
        entry.failures = min(entry.failures + 1, 5)
        entry.retry_at = time.monotonic() + min(30 * 2 ** (entry.failures - 1), 300)
