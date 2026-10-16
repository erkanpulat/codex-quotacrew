"""Normalize Codex lifecycle values across client and App Server versions."""

_TURN_STATUS = {
    "in_progress": "inProgress",
    "in-progress": "inProgress",
    "running": "inProgress",
    "complete": "completed",
    "success": "completed",
    "error": "failed",
    "failure": "failed",
}


def normalize_turn_status(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return _TURN_STATUS.get(value, value)
