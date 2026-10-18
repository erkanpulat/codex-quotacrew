"""Shared test fixtures.

Every test runs against an isolated temporary data directory and DB so tests
never touch the real ``%LOCALAPPDATA%\\CodexAccountManager`` install or ``~/.codex``.

Because production modules do ``from codex_account_manager.core.paths import paths``
(binding the singleton object), we redirect by mutating the singleton's fields
in place — every module that holds a reference then observes the new paths.
"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture(scope="session")
def qt_app():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    return app


@pytest.fixture(autouse=True)
def tmp_paths(tmp_path):
    """Redirect all app paths into a temporary directory (incl. Unicode)."""
    paths_mod = importlib.import_module("codex_account_manager.core.paths")
    singleton = paths_mod.paths

    # Include Unicode + spaces to exercise Windows-style paths.
    root = tmp_path / "Çalışmalar" / "QuotaCrew"
    data = root / "data"
    shared = root / ".codex"
    new_fields = {
        "data_dir": data,
        "db_path": data / "accounts.db",
        "profiles_dir": data / "profiles",
        "logs_dir": data / "logs",
        "backups_dir": data / "backups",
        "shared_codex_home": shared,
    }

    # Save originals and mutate the frozen dataclass in place.
    originals = {k: getattr(singleton, k) for k in new_fields}
    for key, value in new_fields.items():
        object.__setattr__(singleton, key, value)
    singleton.ensure()
    shared.mkdir(parents=True, exist_ok=True)

    yield singleton

    for key, value in originals.items():
        object.__setattr__(singleton, key, value)


@pytest.fixture
async def migrated_db(tmp_paths):
    from codex_account_manager.storage.migrations import migrate

    await migrate()
    return tmp_paths


@pytest.fixture(autouse=True)
def block_live_native_desktop(monkeypatch):
    from codex_account_manager.adapters import native_desktop

    def blocked(*_args, **_kwargs):
        raise RuntimeError("Tests must not connect to a live Desktop.")

    monkeypatch.setattr(native_desktop, "_exchange", blocked)


@pytest.fixture(autouse=True)
def fake_shell_open(monkeypatch):
    import os
    from unittest.mock import Mock

    opened = Mock()
    monkeypatch.setattr(os, "startfile", opened, raising=False)
    return opened


@pytest.fixture(autouse=True)
def block_live_shutdown(monkeypatch):
    from codex_account_manager.platform import power

    def blocked():
        raise AssertionError("Tests must never shut down the computer.")

    monkeypatch.setattr(power, "shutdown_windows", blocked)


@pytest.fixture(autouse=True)
def block_live_ide(monkeypatch):
    from codex_account_manager.adapters import native_ide
    from codex_account_manager.platform import ide_session

    def blocked(*_args, **_kwargs):
        raise AssertionError("Tests must never send to a live IDE.")

    monkeypatch.setattr(native_ide, "_exchange", blocked)
    monkeypatch.setattr(ide_session, "request_close", blocked)
    monkeypatch.setattr(ide_session, "launch_from_explorer", blocked)


@pytest.fixture(autouse=True)
def block_live_updates(monkeypatch):
    from codex_account_manager import updates

    def blocked(*_args, **_kwargs):
        raise AssertionError("Tests must not download or install real updates.")

    monkeypatch.setattr(updates, "_open", blocked)
    monkeypatch.setattr(updates, "launch_from_explorer", blocked)


@pytest.fixture(autouse=True)
def block_live_cli_setup(monkeypatch):
    from codex_account_manager.codex import setup

    def blocked(*_args, **_kwargs):
        raise AssertionError("Tests must not install Codex CLI on the real computer.")

    monkeypatch.setattr(setup.urllib.request, "build_opener", blocked)
