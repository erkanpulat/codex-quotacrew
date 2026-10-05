import base64
import json
from datetime import UTC, datetime, timedelta

import pytest

from codex_account_manager.adapters.subscription import subscription_period


@pytest.mark.parametrize(
    "case",
    [
        "valid",
        "wrong_account",
        "wrong_plan",
        "wrong_email",
        "expired_period",
        "future_check",
        "missing",
        "malformed",
        "naive",
    ],
)
def test_subscription_metadata_is_optional_and_bound_to_live_identity(tmp_path, case):
    now = datetime.now(UTC)
    until = now + timedelta(days=10)
    details = {
        "chatgpt_account_id": "test-account",
        "chatgpt_plan_type": "plus",
        "chatgpt_subscription_active_until": until.isoformat(),
        "chatgpt_subscription_last_checked": now.isoformat(),
    }
    if case == "wrong_account":
        details["chatgpt_account_id"] = "other-account"
    if case == "wrong_plan":
        details["chatgpt_plan_type"] = "free"
    if case == "expired_period":
        details["chatgpt_subscription_active_until"] = (now - timedelta(days=1)).isoformat()
    if case == "future_check":
        details["chatgpt_subscription_last_checked"] = (now + timedelta(days=1)).isoformat()
    if case == "missing":
        del details["chatgpt_subscription_active_until"]
    if case == "naive":
        details["chatgpt_subscription_active_until"] = until.replace(tzinfo=None).isoformat()
    claims = {"email": "test@example.invalid", "https://api.openai.com/auth": details}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    auth = {"tokens": {"account_id": "test-account", "id_token": f"header.{payload}.signature"}}
    (tmp_path / "auth.json").write_text("{" if case == "malformed" else json.dumps(auth))
    result = subscription_period(
        str(tmp_path),
        "test-account",
        "plus",
        "other@example.invalid" if case == "wrong_email" else "test@example.invalid",
    )
    assert result == ((until, now) if case == "valid" else None)
