"""Account ownership, separate from workspace routing and quota permission."""

import json
from dataclasses import dataclass, field
from enum import StrEnum
from hashlib import sha256


class IdentityState(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    UNKNOWN = "unknown"
    CONFLICT = "conflict"


@dataclass(frozen=True)
class AccountIdentity:
    provider: str = "chatgpt"
    user_id: str | None = field(default=None, repr=False)
    workspace_id: str | None = field(default=None, repr=False)
    session_id: str | None = field(default=None, repr=False)
    state: IdentityState = IdentityState.UNKNOWN

    @property
    def owner_key(self) -> tuple[str, str, str] | None:
        if self.state is IdentityState.COMPLETE and self.user_id and self.workspace_id:
            return (self.provider, self.user_id, self.workspace_id)
        return None

    def matches(self, other: "AccountIdentity | None") -> bool:
        return (
            self.owner_key is not None
            and isinstance(other, AccountIdentity)
            and self.owner_key == other.owner_key
        )

    @property
    def fingerprint(self) -> str | None:
        if self.owner_key is None:
            return None
        encoded = json.dumps(self.owner_key, ensure_ascii=True, separators=(",", ":"))
        return "owner-v1:" + sha256(encoded.encode()).hexdigest()


def reconcile_identity(
    scope: AccountIdentity, before: AccountIdentity, after: AccountIdentity
) -> AccountIdentity:
    """Require a stable session around the account/quota read; never infer permission."""
    if IdentityState.CONFLICT in (scope.state, before.state, after.state):
        return AccountIdentity(state=IdentityState.CONFLICT)
    if before.state is IdentityState.UNKNOWN or after.state is IdentityState.UNKNOWN:
        return AccountIdentity()
    if before != after:
        return AccountIdentity(state=IdentityState.CONFLICT)
    if scope.workspace_id is None or after.workspace_id is None:
        return AccountIdentity(state=IdentityState.PARTIAL)
    if scope.provider != after.provider or scope.workspace_id != after.workspace_id:
        return AccountIdentity(state=IdentityState.CONFLICT)
    return after
