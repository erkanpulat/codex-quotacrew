from unittest.mock import Mock

import pytest

from codex_account_manager.core import files


@pytest.mark.parametrize("winerror", [5, 32, 33])
@pytest.mark.parametrize("persistent", [False, True])
def test_windows_replace_contention_is_bounded_and_preserves_data(
    tmp_path, monkeypatch, winerror, persistent
):
    target = tmp_path / "atomic" / "credentials"
    files.atomic_write(target, b"old")
    replace = files.os.replace
    error = PermissionError("Synthetic Windows replacement contention")
    error.winerror = winerror
    attempts = Mock(side_effect=[error] * (4 if persistent else 2))

    def contend(source, destination):
        assert target.read_bytes() == b"old"
        if persistent or attempts.call_count < 2:
            attempts()
        return replace(source, destination)

    sleep = Mock()
    monkeypatch.setattr(files.os, "replace", contend)
    monkeypatch.setattr(files.time, "sleep", sleep)
    if persistent:
        with pytest.raises(PermissionError):
            files.atomic_write(target, b"new")
        assert target.read_bytes() == b"old"
        assert attempts.call_count == 4
    else:
        files.atomic_write(target, b"new")
        assert target.read_bytes() == b"new"
        assert attempts.call_count == 2
    assert sum(call.args[0] for call in sleep.call_args_list) <= 0.14
    assert list(target.parent.iterdir()) == [target]
