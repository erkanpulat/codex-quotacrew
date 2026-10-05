"""Optional display metadata from the current local sign-in, never authorization."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path


def subscription_period(
    home: str, account_id: str | None, plan: str | None, email: str | None
) -> tuple[datetime, datetime] | None:
    if (
        not isinstance(account_id, str)
        or not account_id
        or not isinstance(plan, str)
        or not plan
        or plan.lower() == "free"
        or not isinstance(email, str)
        or not email
    ):
        return None
    try:
        with (Path(home) / "auth.json").open("rb") as stream:
            raw = stream.read(262145)
        if len(raw) > 262144:
            return None
        auth = json.loads(raw)
        token = auth["tokens"]["id_token"]
        parts = token.split(".")
        if len(parts) != 3:
            return None
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
        details = claims["https://api.openai.com/auth"]
        if (
            details.get("chatgpt_account_id") != account_id
            or auth["tokens"].get("account_id") != account_id
            or details.get("chatgpt_plan_type") != plan
            or claims.get("email", "").casefold() != email.casefold()
        ):
            return None
        until = datetime.fromisoformat(details["chatgpt_subscription_active_until"])
        checked = datetime.fromisoformat(details["chatgpt_subscription_last_checked"])
        now = datetime.now(UTC)
        if (
            until.tzinfo is None
            or checked.tzinfo is None
            or until <= now
            or checked > now + timedelta(minutes=5)
            or checked >= until
        ):
            return None
        return until, checked
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None
