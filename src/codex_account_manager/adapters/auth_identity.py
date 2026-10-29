"""Bounded local identity hints; decoded claims never authorize an operation."""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path

from codex_account_manager.adapters.account_identity import _identifier
from codex_account_manager.domain.identity import AccountIdentity, IdentityState

MAX_AUTH_BYTES = 262144


@dataclass(frozen=True)
class AuthEvidence:
    identity: AccountIdentity = field(default_factory=AccountIdentity)
    login_owner: str | None = field(default=None, repr=False)
    revision: tuple[int, int, int, str] | None = field(default=None, repr=False)


def _claims(value: object) -> dict:
    if not isinstance(value, str) or len(value) > MAX_AUTH_BYTES:
        raise ValueError
    parts = value.split(".")
    if len(parts) != 3 or not all(parts):
        raise ValueError
    payload = base64.b64decode(parts[1] + "=" * (-len(parts[1]) % 4), altchars=b"-_", validate=True)
    claims = json.loads(payload)
    if not isinstance(claims, dict):
        raise ValueError
    auth = claims.get("https://api.openai.com/auth", {})
    if not isinstance(auth, dict):
        raise ValueError
    return auth


def auth_evidence(data: bytes) -> AuthEvidence:
    if len(data) > MAX_AUTH_BYTES:
        return AuthEvidence()
    try:
        document = json.loads(data)
        if not isinstance(document, dict) or document.get("auth_mode") not in (None, "chatgpt"):
            return AuthEvidence()
        tokens = document.get("tokens")
        if not isinstance(tokens, dict):
            return AuthEvidence()
        workspace = tokens.get("account_id")
        login_owner = tokens.get("login_owner_id")
        if not _identifier(workspace) or (login_owner is not None and not _identifier(login_owner)):
            return AuthEvidence()
        claims = [_claims(tokens["id_token"])]
        access = tokens.get("access_token")
        if not isinstance(access, str) or not access:
            return AuthEvidence()
        if "." in access:
            claims.append(_claims(access))
        users: set[str] = set()
        workspaces = {workspace}
        scoped_user = False
        for claim in claims:
            user_fields = [claim.get("chatgpt_user_id"), claim.get("user_id")]
            claimed_workspace = claim.get("chatgpt_account_id")
            if any(
                value is not None and not _identifier(value)
                for value in [*user_fields, claimed_workspace]
            ):
                return AuthEvidence()
            users.update(value for value in user_fields if value is not None)
            if claimed_workspace:
                workspaces.add(claimed_workspace)
                scoped_user |= any(user_fields)
        if len(users) > 1 or len(workspaces) > 1:
            return AuthEvidence(identity=AccountIdentity(state=IdentityState.CONFLICT))
        user = next(iter(users), None)
        return AuthEvidence(
            identity=AccountIdentity(
                user_id=user,
                workspace_id=workspace,
                state=IdentityState.COMPLETE if user and scoped_user else IdentityState.PARTIAL,
            ),
            login_owner=login_owner,
        )
    except (ValueError, TypeError, KeyError, RecursionError):
        return AuthEvidence()


def read_auth_evidence(home: str | Path) -> AuthEvidence:
    path = Path(home) / "auth.json"
    try:
        if path.is_symlink() or path.resolve().parent != path.parent.resolve():
            return AuthEvidence()
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            data = stream.read(MAX_AUTH_BYTES + 1)
            after = os.fstat(stream.fileno())
        current = path.stat()
        revisions = {
            (stat.st_ino, stat.st_mtime_ns, stat.st_size) for stat in (before, after, current)
        }
        if len(revisions) != 1:
            return AuthEvidence(identity=AccountIdentity(state=IdentityState.CONFLICT))
        evidence = auth_evidence(data)
        return AuthEvidence(
            identity=evidence.identity,
            login_owner=evidence.login_owner,
            revision=(*revisions.pop(), sha256(data).hexdigest()),
        )
    except OSError:
        return AuthEvidence()


def stable_auth_identity(
    startup: AuthEvidence | None, before: AuthEvidence, after: AuthEvidence
) -> AccountIdentity:
    evidence = [before, after] if startup is None else [startup, before, after]
    if any(item.identity.state is IdentityState.CONFLICT for item in evidence):
        return AccountIdentity(state=IdentityState.CONFLICT)
    if any(item.identity.owner_key is None for item in evidence):
        return AccountIdentity()
    if (
        len({item.identity.owner_key for item in evidence}) != 1
        or len({item.login_owner for item in evidence}) != 1
    ):
        return AccountIdentity(state=IdentityState.CONFLICT)
    # Replacing identical bytes can conceal an intervening account switch.
    for earlier, later in zip(evidence, evidence[1:], strict=False):
        if (
            earlier.revision
            and later.revision
            and earlier.revision[-1] == later.revision[-1]
            and earlier.revision != later.revision
        ):
            return AccountIdentity(state=IdentityState.CONFLICT)
    return after.identity
