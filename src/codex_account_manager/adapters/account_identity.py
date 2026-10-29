"""Parse documented camelCase App Server identity fields, without auth-file access."""

from codex_account_manager.domain.identity import AccountIdentity, IdentityState


def _identifier(value: object) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= 320
        and not any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)
    )


def account_scope(account_result: dict, rate_result: dict) -> AccountIdentity:
    account = account_result.get("account")
    if not isinstance(account, dict) or account.get("type") != "chatgpt":
        return AccountIdentity()
    routing = account_result.get("workspaceRouting")
    if routing is not None and not isinstance(routing, dict):
        return AccountIdentity()
    values = [rate_result.get("accountId"), (routing or {}).get("chatgptAccountId")]
    if any(value is not None and not _identifier(value) for value in values):
        return AccountIdentity()
    workspaces = {value for value in values if value is not None}
    if len(workspaces) > 1:
        return AccountIdentity(state=IdentityState.CONFLICT)
    if workspaces:
        return AccountIdentity(workspace_id=workspaces.pop(), state=IdentityState.PARTIAL)
    return AccountIdentity()


def session_identity(result: object) -> AccountIdentity:
    """Select the explicitly active session, never the first workspace match."""
    if not isinstance(result, dict):
        return AccountIdentity()
    active_id, sessions = result.get("activeSessionId"), result.get("sessions")
    if not _identifier(active_id) or not isinstance(sessions, list):
        return AccountIdentity()
    if any(
        not isinstance(row, dict)
        or not _identifier(row.get("sessionId"))
        or not isinstance(row.get("isActive"), bool)
        for row in sessions
    ):
        return AccountIdentity()
    if len({row["sessionId"] for row in sessions}) != len(sessions):
        return AccountIdentity(state=IdentityState.CONFLICT)
    selected = [row for row in sessions if row["sessionId"] == active_id]
    active = [row for row in sessions if row["isActive"]]
    if len(selected) != 1 or active != selected:
        return AccountIdentity(state=IdentityState.CONFLICT)
    row = selected[0]
    user_id, workspace_id = row.get("userId"), row.get("selectedWorkspaceAccountId")
    if any(value is not None and not _identifier(value) for value in (user_id, workspace_id)):
        return AccountIdentity()
    state = (
        IdentityState.COMPLETE
        if user_id and workspace_id
        else IdentityState.PARTIAL
        if user_id or workspace_id
        else IdentityState.UNKNOWN
    )
    return AccountIdentity(
        user_id=user_id, workspace_id=workspace_id, session_id=active_id, state=state
    )
