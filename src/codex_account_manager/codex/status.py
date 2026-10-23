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


def normalize_thread_source(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and ("subagent" in value or "subAgent" in value):
        subagent = value.get("subagent", value.get("subAgent"))
        if isinstance(subagent, dict):
            if subagent.get("other") == "guardian":
                return "subAgentGuardian"
            if isinstance(subagent.get("thread_spawn", subagent.get("threadSpawn")), dict):
                return "subAgentThreadSpawn"
        return "subAgent"
    return "unknown"
