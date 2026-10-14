"""Non-blocking interprocess lock, released by the OS even after a crash."""

from __future__ import annotations

import asyncio
import errno
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from codex_account_manager.core.errors import OperationBusyError, TransactionError


class OperationLock:
    def __init__(self, path: Path):
        self.path = path
        self._fd: int | None = None

    def __enter__(self) -> OperationLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            error_type = (
                OperationBusyError
                if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}
                else TransactionError
            )
            raise error_type(
                "Another operation is in progress. Retry after it completes.", stage="prepare"
            ) from exc
        self._fd = fd
        return self

    def __exit__(self, *exc: object) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    @asynccontextmanager
    async def wait(self, *, timeout: float):
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            try:
                self.__enter__()
                break
            except OperationBusyError:
                if asyncio.get_running_loop().time() >= deadline:
                    raise
                await asyncio.sleep(0.1)
        try:
            yield self
        finally:
            self.__exit__(None, None, None)
