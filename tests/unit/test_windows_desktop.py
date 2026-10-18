"""Packaged Desktop detection must not confuse the Codex CLI with the GUI."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from codex_account_manager.adapters import desktop
from codex_account_manager.platform import windows
from codex_account_manager.platform.windows import _process_has_exited
from codex_account_manager.platform.windows import is_desktop_installed as native_desktop_installed


@pytest.fixture(autouse=True)
def assume_processes_are_alive(monkeypatch):
    monkeypatch.setattr(windows, "_process_has_exited", lambda _pid: False)
    monkeypatch.setattr(windows, "is_desktop_installed", lambda: True)


@pytest.mark.parametrize(
    "count,code,expected", [(0, 0, False), (1, 122, True), (2, 122, True), (0, 5, None)]
)
def test_desktop_registration_checks_current_user_without_launching(
    count, code, expected, monkeypatch
):
    from unittest.mock import Mock

    monkeypatch.setattr(windows, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(windows, "_desktop_processes", lambda: [])

    def query(family, count_pointer, names, _length_pointer, buffer):
        assert family == "OpenAI.Codex_2p2nqsd0c76g0"
        assert names is None and buffer is None
        count_pointer._obj.value = count
        return code

    function = Mock(side_effect=query)
    monkeypatch.setattr(
        windows.ctypes,
        "WinDLL",
        lambda *_args, **_kwargs: SimpleNamespace(GetPackagesByPackageFamily=function),
        raising=False,
    )
    launch = Mock()
    monkeypatch.setattr(windows.subprocess, "Popen", launch)
    if expected is None:
        with pytest.raises(OSError, match="could not be checked"):
            native_desktop_installed()
    else:
        assert native_desktop_installed() is expected
    launch.assert_not_called()


def test_running_packaged_desktop_is_installation_evidence(monkeypatch):
    monkeypatch.setattr(windows, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(windows, "_desktop_processes", lambda: [object()])
    assert native_desktop_installed()


async def test_missing_desktop_stops_switch_before_credentials_or_recovery_change(
    tmp_paths, monkeypatch
):
    from unittest.mock import AsyncMock, Mock

    from codex_account_manager.adapters.credential_store import FileCredentialStore
    from codex_account_manager.auth.transaction import AuthTransaction
    from codex_account_manager.core.errors import DesktopLaunchError
    from codex_account_manager.domain.models import Profile

    monkeypatch.setattr(windows, "is_desktop_installed", lambda: False)
    store = FileCredentialStore(shared_home=tmp_paths.shared_codex_home)
    store.write_active_atomic(b"original-shared-auth")
    home = tmp_paths.profiles_dir / "target"
    home.mkdir(parents=True)
    (home / "auth.json").write_bytes(b"target-auth")
    config = store.shared_home / "config.toml"
    config.write_bytes(b"original-config")
    target = Profile(
        id="target", alias="Second", codex_home=str(home), bound_account_id="account-2"
    )
    verify, stop, launch = AsyncMock(), Mock(), Mock()
    monkeypatch.setattr(windows, "stop_desktop", stop)
    monkeypatch.setattr(windows, "launch_desktop", launch)
    tx = AuthTransaction(
        credential_store=store,
        desktop=desktop.WindowsDesktopLauncher(),
        verify_account=verify,
        launch_desktop=True,
    )
    with pytest.raises(DesktopLaunchError, match="not installed"):
        await tx.switch(target)
    assert store.read_active() == b"original-shared-auth"
    assert config.read_bytes() == b"original-config"
    assert (home / "auth.json").read_bytes() == b"target-auth"
    assert not tx.recovery_path.exists()
    assert not (tmp_paths.data_dir / "switch.journal.json").exists()
    verify.assert_not_awaited()
    stop.assert_not_called()
    launch.assert_not_called()


def test_launch_missing_desktop_never_opens_an_unrelated_explorer_window(monkeypatch):
    from unittest.mock import Mock

    from codex_account_manager.core.errors import DesktopLaunchError

    monkeypatch.setattr(windows, "is_desktop_installed", lambda: False)
    launch = Mock()
    monkeypatch.setattr(windows.subprocess, "Popen", launch)
    with pytest.raises(DesktopLaunchError, match="not installed"):
        windows.launch_desktop()
    launch.assert_not_called()


@pytest.mark.parametrize("verify_matches", [True, False])
async def test_cli_and_ide_activation_without_desktop_stays_transactional(
    tmp_paths, monkeypatch, verify_matches
):
    from unittest.mock import Mock

    from codex_account_manager.adapters.credential_store import FileCredentialStore
    from codex_account_manager.auth.transaction import AuthTransaction
    from codex_account_manager.core.errors import TransactionError
    from codex_account_manager.domain.models import Profile

    monkeypatch.setattr(windows, "is_desktop_installed", lambda: False)
    monkeypatch.setattr(windows, "is_desktop_running", lambda: False)
    launch, stop = Mock(), Mock()
    monkeypatch.setattr(windows, "launch_desktop", launch)
    monkeypatch.setattr(windows, "stop_desktop", stop)
    store = FileCredentialStore(shared_home=tmp_paths.shared_codex_home)
    store.write_active_atomic(b"original")
    home = tmp_paths.profiles_dir / "target"
    home.mkdir(parents=True)
    (home / "auth.json").write_bytes(b"target")
    target = Profile(
        id="target", alias="IDE account", codex_home=str(home), bound_account_id="acc2"
    )

    async def verify(_home):
        return "acc2" if verify_matches else "wrong"

    tx = AuthTransaction(
        credential_store=store, desktop=desktop.WindowsDesktopLauncher(), verify_account=verify
    )
    if verify_matches:
        assert (await tx.switch(target)).success
        assert store.read_active() == b"target"
        stop.assert_not_called()
    else:
        with pytest.raises(TransactionError) as error:
            await tx.switch(target)
        assert error.value.rolled_back
        assert store.read_active() == b"original"
    launch.assert_not_called()
    assert not tx.recovery_path.exists()


class Process:
    def __init__(self, name: str, exe: str):
        self.info = {"name": name, "exe": exe}
        self.terminated = False
        self.pid = id(self)
        self.parent_pid = 0

    def ppid(self) -> int:
        return self.parent_pid

    def terminate(self) -> None:
        self.terminated = True


PACKAGED = (
    r"C:\Program Files\WindowsApps\OpenAI.Codex_26.924.2738.0_x64__2p2nqsd0c76g0\app\ChatGPT.exe"
)


def test_detects_installed_desktop_main_process_without_matching_cli(monkeypatch):
    app = Process("ChatGPT.exe", PACKAGED)
    old_app = Process("Codex.exe", PACKAGED.replace("ChatGPT.exe", "Codex.exe"))
    cli = Process("codex.exe", r"C:\Users\test\AppData\Local\OpenAI\Codex\bin\codex.exe")
    other_chat = Process(
        "ChatGPT.exe", r"C:\Program Files\WindowsApps\OpenAI.ChatGPT_1\app\ChatGPT.exe"
    )
    helper = Process(
        "codex-windows-sandbox-service.exe",
        PACKAGED.replace(r"app\ChatGPT.exe", r"app\resources\codex-windows-sandbox-service.exe"),
    )
    monkeypatch.setattr(
        windows.psutil, "process_iter", lambda _attrs: [app, old_app, cli, other_chat, helper]
    )
    assert windows._desktop_processes() == [app, old_app]
    assert windows.is_desktop_running()
    snapshots = iter(([app, old_app], []))
    monkeypatch.setattr(windows, "_desktop_processes", lambda: next(snapshots))
    windows.stop_desktop()
    assert app.terminated and old_app.terminated
    assert not cli.terminated and not other_chat.terminated and not helper.terminated


@pytest.mark.asyncio
async def test_readiness_never_accepts_standalone_app_server_without_desktop(monkeypatch):
    launcher = desktop.WindowsDesktopLauncher()
    monkeypatch.setattr(launcher, "is_running", lambda: False)
    assert not await launcher.wait_ready(timeout=0.01)


@pytest.mark.asyncio
async def test_readiness_accepts_detected_desktop(monkeypatch):
    launcher = desktop.WindowsDesktopLauncher()
    observed = iter((False, True))
    monkeypatch.setattr(launcher, "is_running", lambda: next(observed))
    assert await launcher.wait_ready(timeout=2)


@pytest.mark.asyncio
async def test_switch_commits_when_verified_account_and_packaged_process_are_ready(
    tmp_paths, monkeypatch
):
    from codex_account_manager.adapters.credential_store import FileCredentialStore
    from codex_account_manager.auth.transaction import AuthTransaction
    from codex_account_manager.domain.models import Profile

    home = tmp_paths.profiles_dir / "target"
    home.mkdir(parents=True)
    (home / "auth.json").write_bytes(b"target-credentials")
    target = Profile(id="target", alias="Other", codex_home=str(home), bound_account_id="account-2")
    store = FileCredentialStore(shared_home=tmp_paths.shared_codex_home)
    store.write_active_atomic(b"current-credentials")
    running = False

    def stop():
        nonlocal running
        running = False

    def launch():
        nonlocal running
        running = True

    monkeypatch.setattr(windows, "stop_desktop", stop)
    monkeypatch.setattr(windows, "is_desktop_installed", lambda: True)
    monkeypatch.setattr(windows, "launch_desktop", launch)
    monkeypatch.setattr(windows, "is_desktop_running", lambda: running)

    async def verify(_home):
        return "account-2"

    transaction = AuthTransaction(
        credential_store=store,
        desktop=desktop.WindowsDesktopLauncher(),
        verify_account=verify,
    )
    result = await transaction.switch(target)
    assert result.success
    assert running
    assert store.read_active() == b"target-credentials"


def test_shutdown_rechecks_replacement_children(monkeypatch):
    parent = Process("ChatGPT.exe", PACKAGED)
    replacement = Process("ChatGPT.exe", PACKAGED)
    snapshots = iter(([parent], [replacement], []))
    monkeypatch.setattr(windows, "_desktop_processes", lambda: next(snapshots))
    monkeypatch.setattr(windows.time, "sleep", lambda _: None)
    windows.stop_desktop()
    assert parent.terminated and replacement.terminated


def test_shutdown_refuses_to_terminate_its_own_desktop_ancestor(monkeypatch):
    parent = Process("ChatGPT.exe", PACKAGED)
    monkeypatch.setattr(windows, "_desktop_processes", lambda: [parent])
    monkeypatch.setattr(
        windows.psutil, "Process", lambda: SimpleNamespace(parents=lambda: [parent])
    )
    with pytest.raises(RuntimeError, match="Start menu shortcut"):
        windows.stop_desktop()
    assert not parent.terminated


def test_shutdown_timeout_still_aborts(monkeypatch):
    process = Process("ChatGPT.exe", PACKAGED)
    monkeypatch.setattr(windows, "_desktop_processes", lambda: [process])
    clock = iter((0.0, 31.0))
    monkeypatch.setattr(windows.time, "monotonic", lambda: next(clock))
    with pytest.raises(RuntimeError, match="could not be stopped"):
        windows.stop_desktop()


@pytest.mark.parametrize(
    "thread_id",
    [
        "new",
        "NEW",
        "../settings",
        "thread?prompt=run",
        "https://example.test",
        "",
        "a\nb",
        "a" * 129,
    ],
)
def test_conversation_deep_link_rejects_route_and_query_injection(thread_id):
    with pytest.raises(ValueError):
        windows.conversation_url(thread_id)


def test_conversation_deep_link_opens_existing_chat_without_sending_input():
    assert windows.conversation_url("019abc-task-123") == "codex://threads/019abc-task-123"


def test_shutdown_stops_parent_before_restarting_children(monkeypatch):
    parent = Process("ChatGPT.exe", PACKAGED)
    child = Process("ChatGPT.exe", PACKAGED)
    child.parent_pid = parent.pid
    order = []
    parent.terminate = lambda: order.append("parent")
    child.terminate = lambda: order.append("child")
    snapshots = iter(([child, parent], []))
    monkeypatch.setattr(windows, "_desktop_processes", lambda: next(snapshots))
    monkeypatch.setattr(windows.time, "sleep", lambda _: None)
    windows.stop_desktop()
    assert order == ["parent", "child"]


def test_detection_ignores_exited_process_with_retained_pid(monkeypatch):
    exited = Process("ChatGPT.exe", PACKAGED)
    running = Process("ChatGPT.exe", PACKAGED)
    monkeypatch.setattr(windows.psutil, "process_iter", lambda _attrs: [exited, running])
    monkeypatch.setattr(windows, "_process_has_exited", lambda pid: pid == exited.pid)
    assert windows._desktop_processes() == [running]


def test_exited_process_does_not_satisfy_desktop_readiness(monkeypatch):
    exited = Process("ChatGPT.exe", PACKAGED)
    monkeypatch.setattr(windows.psutil, "process_iter", lambda _attrs: [exited])
    monkeypatch.setattr(windows, "_process_has_exited", lambda _pid: True)
    assert not windows.is_desktop_running()


def test_shutdown_accepts_signaled_process_still_in_process_table(monkeypatch):
    process = Process("ChatGPT.exe", PACKAGED)
    monkeypatch.setattr(windows.psutil, "process_iter", lambda _attrs: [process])
    monkeypatch.setattr(windows, "_process_has_exited", lambda _pid: process.terminated)
    monkeypatch.setattr(windows.time, "sleep", lambda _: None)
    windows.stop_desktop()
    assert process.terminated


@pytest.mark.parametrize("wait_result, expected", [(0, True), (258, False), (128, None)])
def test_native_process_signal_and_handle_cleanup(monkeypatch, wait_result, expected):
    closed = []
    handle = SimpleNamespace(Close=lambda: closed.append(True))
    monkeypatch.setitem(sys.modules, "pywintypes", SimpleNamespace(error=OSError))
    monkeypatch.setitem(sys.modules, "winerror", SimpleNamespace(ERROR_INVALID_PARAMETER=87))
    monkeypatch.setitem(
        sys.modules,
        "win32con",
        SimpleNamespace(SYNCHRONIZE=0x100000, WAIT_OBJECT_0=0, WAIT_TIMEOUT=258),
    )

    def opened(access, inherit, pid):
        assert (access, inherit, pid) == (0x100000, False, 123)
        return handle

    monkeypatch.setitem(sys.modules, "win32api", SimpleNamespace(OpenProcess=opened))
    monkeypatch.setitem(
        sys.modules,
        "win32event",
        SimpleNamespace(WaitForSingleObject=lambda _handle, timeout: wait_result),
    )
    if expected is None:
        with pytest.raises(RuntimeError, match="verify Desktop"):
            _process_has_exited(123)
    else:
        assert _process_has_exited(123) is expected
    assert closed == [True]


@pytest.mark.parametrize("error_code, expected", [(87, True), (5, False)])
def test_native_process_open_failure_keeps_access_denied_process_alive(
    monkeypatch, error_code, expected
):
    class WindowsError(OSError):
        winerror = error_code

    def opened(*_args):
        raise WindowsError()

    monkeypatch.setitem(sys.modules, "pywintypes", SimpleNamespace(error=WindowsError))
    monkeypatch.setitem(
        sys.modules,
        "winerror",
        SimpleNamespace(ERROR_INVALID_PARAMETER=87, ERROR_ACCESS_DENIED=5),
    )
    monkeypatch.setitem(sys.modules, "win32api", SimpleNamespace(OpenProcess=opened))
    monkeypatch.setitem(sys.modules, "win32con", SimpleNamespace(SYNCHRONIZE=0x100000))
    monkeypatch.setitem(sys.modules, "win32event", SimpleNamespace())
    assert _process_has_exited(123) is expected


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process handles")
def test_real_process_signal_with_retained_handle():
    import subprocess

    import win32api
    import win32con

    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    handle = win32api.OpenProcess(win32con.SYNCHRONIZE, False, child.pid)
    try:
        assert not _process_has_exited(child.pid)
        child.terminate()
        child.wait(timeout=10)
        assert _process_has_exited(child.pid)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
        handle.Close()
