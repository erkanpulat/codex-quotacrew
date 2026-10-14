"""Bridge between Qt's event loop and asyncio coroutines.

A single background event loop runs in a daemon thread. Coroutines are submitted
from the UI thread and their results are marshalled back via Qt signals, keeping
the UI responsive while async core services (App Server calls, DB access) run
off-thread.

Shutdown cancels pending work and drains asynchronous generators before closing
the event loop. Cleanup has a bounded wait so a stuck adapter cannot hang Quit.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from collections.abc import Callable, Coroutine
from typing import Any

from PySide6.QtCore import QObject, Signal, Slot

from codex_account_manager.core.errors import AccountManagerError
from codex_account_manager.core.redaction import redact_text


class AsyncRunner(QObject):
    """Own a background asyncio loop; submit coroutines, get results via callbacks."""

    _result = Signal(object, object)
    _error = Signal(object, object)
    failed = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._closing = False
        self._thread = threading.Thread(target=self._run_loop, name="cx-async", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5)
        self._result.connect(self._deliver_result)
        self._error.connect(self._deliver_error)

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()

    @Slot(object, object)
    def _deliver_result(self, callback: Callable[[Any], object] | None, result: Any) -> None:
        if not self._closing and callback is not None:
            try:
                callback(result)
            except Exception as exc:
                self.failed.emit(redact_text(str(exc)))

    @Slot(object, object)
    def _deliver_error(self, errback: Callable[[Exception], object] | None, exc: Exception) -> None:
        if self._closing or isinstance(exc, concurrent.futures.CancelledError):
            return
        safe = exc if isinstance(exc, AccountManagerError) else RuntimeError(redact_text(str(exc)))
        if errback is not None:
            try:
                errback(safe)
            except Exception as callback_error:
                self.failed.emit(redact_text(str(callback_error)))
        else:
            self.failed.emit(str(safe))

    def submit(
        self,
        coro: Coroutine[Any, Any, Any],
        on_result: Callable[[Any], object] | None = None,
        on_error: Callable[[Exception], object] | None = None,
    ) -> concurrent.futures.Future:
        """Schedule ``coro`` on the background loop; deliver the outcome on the UI thread."""

        def _done(fut: concurrent.futures.Future) -> None:
            try:
                self._result.emit(on_result, fut.result())
            except Exception as exc:  # surfaced to the UI, never crashes the loop
                self._error.emit(on_error, exc)

        if self._closing:
            coro.close()
            raise RuntimeError("Application is shutting down.")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        future.add_done_callback(_done)
        return future

    def shutdown(self, timeout: float = 5.0) -> None:
        """Cancel outstanding work, close the loop cleanly, and join the thread."""
        self._closing = True
        if not self._loop.is_running():
            return

        async def _drain() -> None:
            tasks = [t for t in asyncio.all_tasks(self._loop) if t is not asyncio.current_task()]
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            await self._loop.shutdown_asyncgens()

        try:
            future = asyncio.run_coroutine_threadsafe(_drain(), self._loop)
            future.result(timeout=timeout)
        except Exception:
            pass

        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=timeout)
        if not self._thread.is_alive() and not self._loop.is_closed():
            self._loop.close()
