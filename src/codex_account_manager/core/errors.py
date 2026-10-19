"""Typed exception hierarchy for QuotaCrew.

Exceptions never carry secret material. Messages are safe to log and to show in
the UI. Anything credential-shaped must be redacted before it reaches an
exception message (see :mod:`codex_account_manager.core.redaction`).
"""

from __future__ import annotations


class AccountManagerError(Exception):
    """Base class for all QuotaCrew errors."""

    def __init__(self, message: str):
        from codex_account_manager.core.redaction import redact_text

        super().__init__(redact_text(message))


class CodexNotFoundError(AccountManagerError):
    """The Codex CLI could not be located on PATH."""


class AppServerError(AccountManagerError):
    """The Codex App Server failed to start, respond, or shut down cleanly."""


class SignInRequiredError(AppServerError):
    """Codex explicitly reported a missing or permanently rejected sign-in."""


class WorkspaceRoutingUnauthorizedError(AppServerError):
    """The local workspace router rejected discovery without proving sign-in expiry."""


class SignedOutError(SignInRequiredError):
    """Codex successfully read its authentication state and reported no account."""


class DesktopContinuationRequired(AppServerError):
    """The conversation must retain its Desktop-owned tools and runtime."""


class NativeOperationRejectedError(AppServerError):
    """Desktop rejected an operation before returning a conversation snapshot."""


class ConnectionNotReadyError(AppServerError):
    """A read-only owner check timed out before any continuation was sent."""


class OwnerNotFoundError(AppServerError):
    """The local router explicitly reported that no client owns this thread."""


class LocalResponseTooLargeError(AppServerError):
    """A local response exceeded the bounded frame or aggregate read size."""


class ConversationHistoryTooLargeError(AppServerError):
    """Automatic Desktop loading is unsafe for this conversation's history size."""


class DesktopLaunchError(AccountManagerError):
    """Codex Desktop could not be started or did not become ready in time."""


class HandoffDeferredError(AccountManagerError):
    """A quota handoff must wait for work to reach a verified safe state."""


class AccountRecoveryRequired(AccountManagerError):
    """Stored sign-ins exist but their account catalogue is missing."""


class ProfileNotFoundError(AccountManagerError):
    """No profile matches the requested alias or id."""


class AccountMismatchError(AccountManagerError):
    """The active account does not match the profile's bound account."""


class TransactionError(AccountManagerError):
    """An auth switch transaction failed. Rollback state is described here."""

    def __init__(self, message: str, *, stage: str | None = None, rolled_back: bool = False):
        super().__init__(message)
        self.stage = stage
        self.rolled_back = rolled_back


class OperationBusyError(TransactionError):
    """A shared resource is temporarily held by another operation."""
