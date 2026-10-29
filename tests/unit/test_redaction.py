import pytest

from codex_account_manager.core.redaction import redact, redact_text

FAKE_JWT = ".".join(
    ["eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9", "eyJzdWIiOiIxMjM0NTY3ODkwIn0", "abcDEF123456789"]
)


def test_redacts_jwt():
    assert FAKE_JWT not in redact_text(f"token={FAKE_JWT}")


def test_redacts_sk_key():
    key = "sk-" + "a" * 40
    assert key not in redact_text(f"key {key} here")


def test_redacts_bearer():
    out = redact_text("Authorization: Bearer abcdef012345ghijkl")
    assert "abcdef012345ghijkl" not in out


def test_redacts_sensitive_dict_keys():
    data = {
        "access_token": FAKE_JWT,
        "email": "safe@example.test",
        "nested": {"refresh_token": "x" * 50},
    }
    out = redact(data)
    assert out["access_token"] == "***REDACTED***"
    assert out["email"] == "***REDACTED***"
    assert out["nested"]["refresh_token"] == "***REDACTED***"


def test_redact_never_raises_on_weird_input():
    assert redact(object()) is not None
    assert redact_text("") == ""


@pytest.mark.parametrize(
    "key",
    [
        "token",
        "session",
        "auth",
        "auth_json",
        "private_key",
        "userId",
        "chatgpt_user_id",
        "sessionId",
        "activeSessionId",
        "login_owner_id",
    ],
)
@pytest.mark.parametrize(
    "template", ["{key}=sample-value", '"{key}": "sample-value"', "'{key}': 'sample-value'"]
)
def test_short_sensitive_values_are_redacted_in_error_text(key, template):
    text = "Request failed: " + template.format(key=key)
    result = redact_text(text)
    assert "sample-value" not in result
    assert "Request failed:" in result
    assert "***REDACTED***" in result


def test_emails_are_removed_from_error_and_log_text():
    assert "owner@example.test" not in redact_text("Request rejected for owner@example.test")
