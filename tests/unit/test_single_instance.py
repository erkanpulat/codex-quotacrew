from codex_account_manager.core.single_instance import SingleInstance


def test_second_instance_is_blocked(tmp_paths):
    name = "Local\\CodexAccountManagerTest"
    first = SingleInstance(name)
    assert first.acquire() is True
    second = SingleInstance(name)
    assert second.acquire() is False
    first.release()
    # After release, a new instance can acquire.
    third = SingleInstance(name)
    assert third.acquire() is True
    third.release()


def test_isolated_data_home_does_not_collide_with_daily_app(tmp_paths, monkeypatch):
    monkeypatch.delenv("WIN_PD_OVERRIDE_LOCAL_APPDATA", raising=False)
    daily = SingleInstance().name
    assert daily == "Local\\CodexAccountManagerSingleton"
    monkeypatch.setenv("WIN_PD_OVERRIDE_LOCAL_APPDATA", str(tmp_paths.data_dir))
    assert SingleInstance().name != daily
    first = SingleInstance()
    second = SingleInstance()
    assert first.name == second.name
    assert SingleInstance("Local\\ExplicitName").name == "Local\\ExplicitName"
    assert first.acquire()
    try:
        assert not second.acquire()
    finally:
        first.release()


def test_activation_message_keeps_isolated_windows_separate(tmp_paths, monkeypatch):
    import sys

    if sys.platform != "win32":
        return
    monkeypatch.delenv("WIN_PD_OVERRIDE_LOCAL_APPDATA", raising=False)
    daily = SingleInstance().activation_message()
    monkeypatch.setenv("WIN_PD_OVERRIDE_LOCAL_APPDATA", str(tmp_paths.data_dir))
    isolated = SingleInstance().activation_message()
    assert daily and isolated and daily != isolated
    assert isolated == SingleInstance().activation_message()
