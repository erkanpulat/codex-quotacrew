"""Read bounded lifecycle metadata when stored history cannot describe a live turn."""

from __future__ import annotations

import json
import time
from pathlib import Path

from codex_account_manager.core.files import resolved_path

_MAX_TAIL_BYTES = 4 * 1024 * 1024
_FRESH_SECONDS = 120
_TERMINAL_EVENTS = {
    "task_complete": "completed",
    "task_completed": "completed",
    "turn_aborted": "interrupted",
}


def read_turn_activity(rollout: str, codex_home: Path, turn_id: str) -> str:
    """Return a verified lifecycle state, or unknown; never retain message content."""
    path = resolved_path(rollout)
    if path.suffix != ".jsonl" or not path.is_relative_to(resolved_path(codex_home / "sessions")):
        return "unknown"
    try:
        before = path.stat()
        offset = max(0, before.st_size - _MAX_TAIL_BYTES)
        with path.open("rb") as stream:
            stream.seek(offset)
            tail = stream.read(_MAX_TAIL_BYTES)
        after = path.stat()
    except OSError:
        return "unknown"
    if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
        return "unknown"
    if not tail.endswith(b"\n"):
        return "unknown"
    lines = tail.splitlines()
    if offset:
        lines = lines[1:]
    for line in reversed(lines):
        try:
            event = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            return "unknown"
        if not isinstance(event, dict) or event.get("type") != "event_msg":
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        kind = payload.get("type")
        if not isinstance(kind, str) or kind not in {"task_started", *_TERMINAL_EVENTS}:
            continue
        if payload.get("turn_id") != turn_id:
            return "unknown"
        if kind in _TERMINAL_EVENTS:
            return _TERMINAL_EVENTS[kind]
        # Filesystem and wall-clock timestamps may differ slightly on Windows.
        return "inProgress" if -1 <= time.time() - after.st_mtime <= _FRESH_SECONDS else "unknown"
    return "unknown"
