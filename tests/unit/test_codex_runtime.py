import asyncio
import json
import os
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import psutil
import pytest

from codex_account_manager.codex import runtime
from codex_account_manager.codex.runtime import codex_command
from codex_account_manager.core.errors import AccountManagerError


@pytest.mark.skipif(sys.platform != "win32", reason="Windows command processor")
@pytest.mark.parametrize("suffix", [".cmd", ".bat"])
async def test_batch_wrapper_preserves_arguments_with_unicode_and_spaces(tmp_path, suffix):
    folder = tmp_path / "Çalışmalar with spaces"
    folder.mkdir()
    wrapper = folder / ("fake codex" + suffix)
    wrapper.write_text(
        '@echo off\n"%CODEX_TEST_PYTHON%" -c "import json,sys;print(json.dumps(sys.argv[1:]))" %*\n',
        encoding="utf-8",
    )
    arguments = ["login", "--example", "iki sözcük", "", "stdio://"]
    command = codex_command(*arguments, executable=str(wrapper))
    env = {**os.environ, "CODEX_TEST_PYTHON": sys.executable}
    # Verify argument handling, allowing for cold Windows process startup.
    # Product deadlines are tested separately; this is not a latency benchmark.
    result = subprocess.run(command, capture_output=True, text=True, timeout=60, env=env)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == arguments
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW,
        env=env,
    )
    communication = asyncio.create_task(process.communicate())
    try:
        stdout, stderr = await asyncio.wait_for(asyncio.shield(communication), timeout=60)
    finally:
        if process.returncode is None:
            try:
                children = psutil.Process(process.pid).children(recursive=True)
            except psutil.NoSuchProcess:
                children = []
            for child in children:
                try:
                    child.kill()
                except psutil.NoSuchProcess:
                    pass
            if process.returncode is None:
                process.kill()
        await communication
        await process.wait()
    assert process.returncode == 0, stderr
    assert json.loads(stdout) == arguments


@pytest.mark.parametrize("unsafe", ['"', "%", "!", "&", "|", "<", ">", "^", "(", ")", "\r", "\n"])
def test_batch_wrapper_rejects_shell_syntax(unsafe):
    with pytest.raises(ValueError, match="Unsafe shell characters"):
        codex_command("login", unsafe, executable="codex.cmd")
    with pytest.raises(ValueError, match="Unsafe shell characters"):
        codex_command("login", executable=f"unsafe{unsafe}codex.cmd")


def test_native_executable_keeps_arguments_literal():
    assert codex_command("a&b", executable="codex.exe") == ["codex.exe", "a&b"]


@pytest.mark.parametrize("packaged", [True, False])
def test_sandbox_elevation_is_blocked_only_for_packaged_installations(monkeypatch, packaged):
    monkeypatch.setattr(runtime.package, "is_packaged", lambda: packaged)
    find = Mock(return_value="codex.exe")
    run = Mock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(runtime, "find_codex", find)
    monkeypatch.setattr(runtime.subprocess, "run", run)
    if packaged:
        with pytest.raises(AccountManagerError, match="unavailable in the Microsoft Store edition"):
            runtime.run_sandbox_setup_elevated("synthetic-profile")
        find.assert_not_called()
        run.assert_not_called()
    else:
        assert runtime.run_sandbox_setup_elevated("synthetic-profile") == 0
        assert "-Verb RunAs" in run.call_args.args[0][-1]


def test_store_sandbox_command_reports_a_readable_error(monkeypatch, capsys):
    import typer

    from codex_account_manager.cli import main as cli

    monkeypatch.setattr(runtime.package, "is_packaged", lambda: True)
    monkeypatch.setattr(cli, "_run", lambda value: value)
    monkeypatch.setattr(
        cli._accounts.profiles, "get_by_alias", lambda _: SimpleNamespace(codex_home="synthetic")
    )
    with pytest.raises(typer.Exit) as result:
        cli.sandbox_setup("synthetic")
    assert result.value.exit_code == 1
    assert "Microsoft Store edition" in " ".join(capsys.readouterr().out.split())
