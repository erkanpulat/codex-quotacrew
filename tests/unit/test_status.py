import pytest

from codex_account_manager.codex.status import normalize_turn_status


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("inProgress", "inProgress"),
        ("in_progress", "inProgress"),
        ("in-progress", "inProgress"),
        ("running", "inProgress"),
        ("completed", "completed"),
        ("complete", "completed"),
        ("error", "failed"),
        ("failed", "failed"),
    ],
)
def test_codex_turn_status_variants_are_stable(raw, expected):
    assert normalize_turn_status(raw) == expected


def test_unknown_turn_status_is_preserved_for_safe_handling():
    assert normalize_turn_status("awaitingApproval") == "awaitingApproval"
    assert normalize_turn_status(None) is None
