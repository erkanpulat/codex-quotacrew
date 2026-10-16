"""Local application state and the shared Codex conversation home."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from platformdirs import user_data_dir

from codex_account_manager.core.files import restrict_access

APP_NAME = "CodexAccountManager"


@dataclass(frozen=True)
class Paths:
    """Resolved, stable filesystem locations used across the app."""

    data_dir: Path
    db_path: Path
    profiles_dir: Path
    logs_dir: Path
    backups_dir: Path
    shared_codex_home: Path

    def ensure(self) -> Paths:
        """Create the writable directories we own. Never touches shared home."""
        for directory in (
            self.data_dir,
            self.profiles_dir,
            self.logs_dir,
            self.backups_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
            restrict_access(directory)
        for profile in self.profiles_dir.iterdir():
            if profile.is_dir():
                restrict_access(profile)
        for suffix in ("", "-wal", "-shm", "-journal"):
            database_file = self.db_path.with_name(self.db_path.name + suffix)
            if database_file.exists() or database_file.is_symlink():
                restrict_access(database_file)
        return self


@lru_cache(maxsize=1)
def _resolve() -> Paths:
    data_dir = Path(user_data_dir(APP_NAME, appauthor=False)).absolute()
    database = data_dir / "accounts.db"
    return Paths(
        data_dir=data_dir,
        db_path=database,
        profiles_dir=data_dir / "profiles",
        logs_dir=data_dir / "logs",
        backups_dir=data_dir / "backups",
        shared_codex_home=(Path.home() / ".codex").resolve(),
    )


#: Process-wide singleton with the resolved paths.
paths: Paths = _resolve()
