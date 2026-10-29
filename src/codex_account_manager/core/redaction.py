"""Secret redaction.

Logging and diagnostic callers use these helpers for defence in depth.
Recognized credential fields and token patterns are masked before output;
pattern matching cannot detect every possible secret.

Redaction is deliberately conservative and never raises.
"""

from __future__ import annotations

import re
from typing import Any

_MASK = "***REDACTED***"
_EMAIL_RE = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# Keys whose values are always masked, regardless of content.
_SENSITIVE_KEYS = frozenset(
    {
        "email",
        "access_token",
        "accesstoken",
        "id_token",
        "idtoken",
        "refresh_token",
        "refreshtoken",
        "token",
        "api_key",
        "apikey",
        "openai_api_key",
        "secret",
        "client_secret",
        "password",
        "authorization",
        "auth",
        "auth_json",
        "cookie",
        "session",
        "private_key",
        "user_id",
        "userid",
        "chatgpt_user_id",
        "session_id",
        "sessionid",
        "activesessionid",
        "login_owner_id",
    }
)

# JWT: three base64url segments separated by dots.
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b")
# OpenAI-style keys and long opaque bearer-ish tokens.
_KEY_RE = re.compile(r"\b(sk|pk|rk)-[A-Za-z0-9_-]{16,}\b")
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._-]{12,}\b")
_LONG_SECRET_RE = re.compile(r"\b[A-Za-z0-9_-]{40,}\b")


def redact_text(value: str) -> str:
    """Mask secret-shaped substrings inside a free-form string."""
    if not value:
        return value
    result = re.sub(
        r"(?i)([\"']?(?:access[_-]?token|refresh[_-]?token|id[_-]?token|api[_-]?key|openai_api_key|password|client_secret|authorization|cookie|secret|token|(?:chatgpt_)?user[_-]?id|(?:active)?session[_-]?id|login_owner_id|session|auth_json|auth|private_key)[\"']?\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;}]+)",
        lambda match: match.group(1) + _MASK,
        _BEARER_RE.sub(_MASK, value),
    )
    result = _JWT_RE.sub(_MASK, result)
    result = _KEY_RE.sub(_MASK, result)
    result = _BEARER_RE.sub(_MASK, result)
    result = _LONG_SECRET_RE.sub(_MASK, result)
    result = _EMAIL_RE.sub(_MASK, result)
    return result


def redact(value: Any, _depth: int = 0) -> Any:
    """Recursively redact secrets from arbitrary data.

    - Dict values under sensitive keys are fully masked.
    - Strings are scanned for secret-shaped substrings.
    - Containers are walked (bounded depth to avoid pathological input).
    """
    if _depth > 12:
        return _MASK
    try:
        if isinstance(value, str):
            return redact_text(value)
        if isinstance(value, dict):
            out: dict[Any, Any] = {}
            for key, item in value.items():
                if isinstance(key, str) and key.strip().lower() in _SENSITIVE_KEYS:
                    out[key] = _MASK
                else:
                    out[key] = redact(item, _depth + 1)
            return out
        if isinstance(value, (list, tuple)):
            return type(value)(redact(item, _depth + 1) for item in value)
    except Exception:
        return _MASK
    return value
