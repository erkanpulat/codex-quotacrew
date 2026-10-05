"""Codex JSON-RPC adapter with bounded requests and deterministic subprocess cleanup."""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

from codex_account_manager import __version__
from codex_account_manager.adapters.credential_store import ProfileAuthSession
from codex_account_manager.adapters.interfaces import CapabilitySet, GoalInfo, ServerInfo
from codex_account_manager.adapters.subscription import subscription_period
from codex_account_manager.codex.runtime import codex_command, profile_environment
from codex_account_manager.core.child_process import (
    ChildProcessLifetime,
    finish_cleanup,
    stop_owned_process,
)
from codex_account_manager.core.errors import (
    AppServerError,
    DesktopContinuationRequired,
    SignedOutError,
    SignInRequiredError,
)
from codex_account_manager.core.logging import get_logger
from codex_account_manager.core.protection import CredentialProtectionError
from codex_account_manager.core.redaction import redact_text
from codex_account_manager.domain.models import AccountSnapshot, ThreadInfo

log = get_logger(__name__)

_DEFAULT_TIMEOUT = 30.0
CONTINUATION_POLL_SECONDS = 30.0
# Methods we probe for capability detection (read-only, cheap).
_PROBE_METHODS = ("account/read", "account/rateLimits/read", "thread/list")


class CodexAppServer:
    """Concrete :class:`AppServerAdapter` backed by a real Codex process."""

    def __init__(
        self,
        codex_home: str | Path,
        *,
        timeout: float = _DEFAULT_TIMEOUT,
        experimental: bool = False,
        refresh_on_unauthorized: bool = False,
    ):
        self.codex_home = str(codex_home)
        self.timeout = timeout
        self.experimental = experimental
        self.refresh_on_unauthorized = refresh_on_unauthorized
        self._turn_events: asyncio.Queue[dict] | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._request_id = 0
        self._capabilities: CapabilitySet | None = None
        self._closing = False
        self._auth_session: ProfileAuthSession | None = None
        self._child_lifetime = ChildProcessLifetime()

    async def start(self) -> ServerInfo:
        if self._proc is not None:
            raise AppServerError("App Server is already running")
        self._closing = False
        creationflags = 0
        if sys.platform == "win32":
            creationflags = subprocess.CREATE_NO_WINDOW
        self._auth_session = ProfileAuthSession(self.codex_home)
        self._auth_session.__enter__()
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *codex_command("app-server", "--listen", "stdio://"),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                limit=16 * 1024 * 1024,
                env=profile_environment(self.codex_home),
                creationflags=creationflags,
            )
            self._child_lifetime.attach(self._proc.pid)
        except BaseException as exc:
            if self._proc is not None:
                await finish_cleanup(self._finish_process_close(self._proc))
            else:
                self._child_lifetime.close()
                self._close_auth_session()
            if isinstance(exc, OSError):
                raise AppServerError(
                    f"Could not start Codex App Server: {redact_text(str(exc))}"
                ) from exc
            raise

        self._reader_task = asyncio.create_task(self._read_loop())

        try:
            result = await self._request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "codex_account_manager",
                        "title": "QuotaCrew",
                        "version": __version__,
                    },
                    "capabilities": {"experimentalApi": self.experimental},
                },
            )
            await self._notify("initialized")
            return ServerInfo(
                user_agent=result.get("userAgent"),
                codex_home=result.get("codexHome"),
                platform_os=result.get("platformOs"),
            )
        except BaseException:
            await self.aclose()
            raise

    async def aclose(self) -> None:
        if self._closing:
            return
        self._closing = True
        proc = self._proc
        if proc is None:
            self._close_auth_session()
            return

        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(AppServerError("App Server closing"))
        self._pending.clear()

        # Stop the reader before touching the process so it cannot race on a
        # closing transport.
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
            self._reader_task = None

        try:
            if proc.stdin is not None and not proc.stdin.is_closing():
                proc.stdin.close()
        except Exception:
            pass

        try:
            if proc.returncode is None:
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except TimeoutError:
                    proc.terminate()
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=3)
                    except TimeoutError:
                        proc.kill()
                        await asyncio.wait_for(proc.wait(), timeout=3)
            else:
                await asyncio.wait_for(proc.wait(), timeout=3)
        finally:
            await finish_cleanup(self._finish_process_close(proc))

    async def _finish_process_close(self, proc) -> None:
        try:
            await stop_owned_process(proc, self._child_lifetime)
        except Exception:
            self._closing = False
            raise CredentialProtectionError(
                "Codex did not stop. Sign-in storage remains locked; restart QuotaCrew after Codex exits."
            ) from None
        finally:
            transport = getattr(proc, "_transport", None)
            if transport is not None:
                try:
                    transport.close()
                except Exception:
                    pass
        self._proc = None
        self._close_auth_session()

    def _close_auth_session(self) -> None:
        session = self._auth_session
        self._auth_session = None
        if session is not None:
            session.__exit__(None, None, None)

    async def __aenter__(self) -> CodexAppServer:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def _read_loop(self) -> None:
        if self._proc is None or self._proc.stdout is None:
            raise AppServerError("App Server stdout is unavailable")
        stdout = self._proc.stdout
        try:
            await self._read_messages(stdout)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("App Server reader stopped unexpectedly.")
        finally:
            if not self._closing:
                if self._turn_events is not None:
                    if self._turn_events.full():
                        self._turn_events.get_nowait()
                    self._turn_events.put_nowait({"closed": True})
                for fut in self._pending.values():
                    if not fut.done():
                        fut.set_exception(AppServerError("Codex App Server closed unexpectedly"))
                self._pending.clear()

    async def _read_messages(self, stdout: asyncio.StreamReader) -> None:
        while True:
            try:
                line = await stdout.readline()
            except Exception:
                break
            if not line:
                break
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue  # tolerate non-JSON banner lines
            if not isinstance(message, dict):
                continue
            msg_id = message.get("id")
            if "method" in message:
                if self._turn_events is not None:
                    params = message.get("params") or {}
                    if not isinstance(params, dict):
                        params = {}
                    if isinstance(msg_id, (int, str)) or message["method"] == "turn/completed":
                        # Only terminal events are retained; streamed content is never buffered.
                        if self._turn_events.full():
                            self._turn_events.get_nowait()
                        self._turn_events.put_nowait(
                            {
                                "method": message["method"],
                                "request": isinstance(msg_id, (int, str)),
                                "threadId": params.get("threadId"),
                                "turn": self._turn_summary(params.get("turn")),
                            }
                        )
                if isinstance(msg_id, (int, str)):
                    await self._send(
                        {
                            "id": msg_id,
                            "error": {
                                "code": -32601,
                                "message": "Interactive requests are not supported by this monitoring client.",
                            },
                        }
                    )
                continue
            if not isinstance(msg_id, (int, str)):
                continue
            if msg_id is not None and msg_id in self._pending:
                fut = self._pending.pop(msg_id)
                if not fut.done():
                    if "error" in message:
                        fut.set_exception(AppServerError(redact_text(str(message["error"]))))
                    elif isinstance(message.get("result", {}), dict):
                        fut.set_result(message.get("result") or {})
                    else:
                        fut.set_exception(AppServerError("Invalid App Server response"))

    async def _send(self, message: dict) -> None:
        if not self._proc or not self._proc.stdin:
            raise AppServerError("App Server is not running")
        data = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
        self._proc.stdin.write(data)
        try:
            async with asyncio.timeout(self.timeout):
                await self._proc.stdin.drain()
        except TimeoutError as exc:
            raise AppServerError("App Server write timed out.") from exc

    async def _request(self, method: str, params: dict | None = None) -> dict:
        self._request_id += 1
        req_id = self._request_id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        message = {"method": method, "id": req_id}
        if params is not None:
            message["params"] = params
        try:
            async with asyncio.timeout(self.timeout):
                await self._send(message)
                return await fut
        except TimeoutError as exc:
            self._pending.pop(req_id, None)
            raise AppServerError(f"App Server request timed out: {method}") from exc
        finally:
            self._pending.pop(req_id, None)
            if not fut.done():
                fut.cancel()
            elif not fut.cancelled():
                fut.exception()

    async def _notify(self, method: str, params: dict | None = None) -> None:
        message: dict = {"method": method}
        if params is not None:
            message["params"] = params
        await self._send(message)

    async def capabilities(self) -> CapabilitySet:
        if self._capabilities is not None:
            return self._capabilities
        available: set[str] = set()
        for method in _PROBE_METHODS:
            try:
                params = {"refreshToken": False} if method == "account/read" else {}
                await self._request(method, params)
                available.add(method)
            except AppServerError:
                pass
        self._capabilities = CapabilitySet(methods=frozenset(available))
        return self._capabilities

    @staticmethod
    def _reset_credits(value):
        from pydantic import ValidationError

        from codex_account_manager.domain.models import ResetCredits

        if not isinstance(value, dict):
            return None
        rows = value.get("credits")
        try:
            return ResetCredits.model_validate(
                {
                    "available_count": value.get("availableCount"),
                    "credits": None
                    if rows is None
                    else [
                        {
                            "status": row.get("status"),
                            "reset_type": row.get("resetType"),
                            "granted_at": row.get("grantedAt"),
                            "expires_at": row.get("expiresAt"),
                            "title": row.get("title"),
                            "description": row.get("description"),
                        }
                        for row in rows
                    ],
                }
            )
        except (ValidationError, TypeError, AttributeError):
            return None

    async def read_account(self) -> AccountSnapshot:
        try:
            account_result = await self._request("account/read", {"refreshToken": False})
        except AppServerError as exc:
            if _requires_sign_in(exc):
                raise SignInRequiredError(
                    "Codex rejected this sign-in. Sign in again in Accounts."
                ) from exc
            raise
        if "account" not in account_result:
            raise AppServerError("Codex returned an invalid account response.")
        if account_result["account"] is None:
            raise SignedOutError("Codex has no signed-in account. Sign in again in Accounts.")
        if not isinstance(account_result["account"], dict):
            raise AppServerError("Codex returned an invalid account response.")
        try:
            rate_result = await self._request("account/rateLimits/read")
        except AppServerError as exc:
            if _requires_sign_in(exc):
                raise SignInRequiredError(
                    "Codex rejected this sign-in. Sign in again in Accounts."
                ) from exc
            if not self.refresh_on_unauthorized or not re.search(r"\b401\b", str(exc)):
                raise
            # Let Codex own OAuth rotation; retry only once, never for network/429 errors.
            try:
                account_result = await self._request("account/read", {"refreshToken": True})
                if account_result.get("account") is None:
                    raise SignInRequiredError(
                        "Codex has no signed-in account. Sign in again in Accounts."
                    )
                rate_result = await self._request("account/rateLimits/read")
            except AppServerError as retry_error:
                if _requires_sign_in(retry_error) or re.search(r"\b401\b", str(retry_error)):
                    raise SignInRequiredError(
                        "Codex rejected this sign-in. Sign in again in Accounts."
                    ) from retry_error
                raise

        account = account_result.get("account") or {}
        routing = account_result.get("workspaceRouting") or {}
        limits = (
            (rate_result.get("rateLimitsByLimitId") or {}).get("codex")
            or rate_result.get("rateLimits")
            or {}
        )
        primary = limits.get("primary") or {}
        secondary = limits.get("secondary") or {}
        credits = limits.get("credits") or {}

        account_id = (
            rate_result.get("accountId")
            or routing.get("chatgptAccountId")
            or account.get("accountId")
            or account.get("id")
        )

        plan = account.get("planType") or limits.get("planType")
        period = subscription_period(self.codex_home, account_id, plan, account.get("email"))
        return AccountSnapshot(
            account_id=account_id,
            account_type=account.get("type"),
            email=_display_email(account.get("email")),
            plan_type=plan,
            subscription_until=period[0] if period else None,
            subscription_checked_at=period[1] if period else None,
            ordinary_usage_allowed=rate_result.get("ordinaryUsageAllowed"),
            primary_used_percent=primary.get("usedPercent"),
            primary_resets_at=primary.get("resetsAt"),
            primary_window_minutes=primary.get("windowDurationMins"),
            secondary_used_percent=secondary.get("usedPercent"),
            secondary_resets_at=secondary.get("resetsAt"),
            secondary_window_minutes=secondary.get("windowDurationMins"),
            rate_limit_reached_type=limits.get("rateLimitReachedType"),
            has_credits=credits.get("hasCredits"),
            reset_credits=self._reset_credits(rate_result.get("rateLimitResetCredits")),
        )

    async def list_threads(
        self,
        cursor: str | None = None,
        *,
        max_items: int | None = None,
        include_subagents: bool = True,
    ) -> list[ThreadInfo]:
        if max_items is not None and max_items < 1:
            raise ValueError("max_items must be positive")
        items: list[dict] = []
        seen: set[str] = set()
        for _ in range(100):
            params: dict = {
                "limit": min(100, max_items) if max_items is not None else 100,
                "sortKey": "updated_at",
                "sortDirection": "desc",
                "modelProviders": [],
                "sourceKinds": [
                    "cli",
                    "vscode",
                    "exec",
                    "appServer",
                    "unknown",
                ]
                + (
                    [
                        "subAgent",
                        "subAgentReview",
                        "subAgentCompact",
                        "subAgentThreadSpawn",
                        "subAgentOther",
                    ]
                    if include_subagents
                    else []
                ),
            }
            if cursor:
                params["cursor"] = cursor
            if max_items is not None:
                params["useStateDbOnly"] = True
            result = await self._request("thread/list", params)
            data = result.get("data")
            cursor = result.get("nextCursor")
            if not isinstance(data, list) or any(
                not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]
                for item in data
            ):
                raise AppServerError("App Server returned an invalid conversation list")
            if cursor is not None and not isinstance(cursor, str):
                raise AppServerError("App Server returned an invalid pagination cursor")
            items.extend(data)
            if max_items is not None and len(items) >= max_items:
                items = items[:max_items]
                break
            if not cursor:
                break
            if cursor in seen:
                raise AppServerError("App Server returned a repeated pagination cursor")
            seen.add(cursor)
        else:
            raise AppServerError("Thread list exceeds the 10,000 conversation safety limit")
        threads: list[ThreadInfo] = []
        for item in items:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            status = item.get("status") or {}
            threads.append(
                ThreadInfo(
                    id=item["id"],
                    preview=item.get("preview"),
                    title=item.get("name"),
                    source=(
                        item.get("source")
                        if isinstance(item.get("source"), str)
                        else "subAgent"
                        if isinstance(item.get("source"), dict)
                        else "unknown"
                    ),
                    project_id=item.get("projectId"),
                    model_provider=item.get("modelProvider"),
                    cwd=item.get("cwd"),
                    path=item.get("path"),
                    model=item.get("model"),
                    reasoning_effort=item.get("reasoningEffort"),
                    created_at=item.get("createdAt"),
                    updated_at=item.get("updatedAt"),
                    recency_at=item.get("recencyAt"),
                    status=status.get("type") if isinstance(status, dict) else None,
                )
            )
        return threads

    async def thread_source(self, thread_id: str) -> str:
        result = await self._request("thread/read", {"threadId": thread_id, "includeTurns": False})
        thread = result.get("thread")
        if not isinstance(thread, dict) or thread.get("id") != thread_id:
            raise AppServerError("Conversation identity could not be verified.")
        source = thread.get("source")
        return source if isinstance(source, str) else "unknown"

    async def require_headless_compatible(self, thread_id: str) -> None:
        source = await self.thread_source(thread_id)
        if source == "vscode":
            raise DesktopContinuationRequired(
                "This conversation requires its original Desktop or IDE runtime. A separate App Server cannot preserve Desktop tools."
            )
        if source not in {"cli", "exec", "appServer"}:
            raise AppServerError(
                "Automatic continuation is not supported for this conversation source."
            )

    async def is_desktop_thread(self, thread_id: str) -> bool:
        result = await self._request("thread/read", {"threadId": thread_id, "includeTurns": False})
        thread = result.get("thread")
        if not isinstance(thread, dict) or thread.get("id") != thread_id:
            raise AppServerError("Conversation identity could not be verified.")
        return thread.get("source") == "vscode" and thread.get("originator") == "Codex Desktop"

    async def resume_thread(self, thread_id: str) -> dict:
        await self.require_headless_compatible(thread_id)
        return await self._request("thread/resume", {"threadId": thread_id})

    async def get_goal(self, thread_id: str) -> GoalInfo | None:
        try:
            result = await self._request("thread/goal/get", {"threadId": thread_id})
        except AppServerError:
            return None
        if not isinstance(result, dict) or "goal" not in result:
            return None
        goal = result["goal"]
        if goal is None:
            return GoalInfo(thread_id=thread_id, objective=None, status=None, present=False)
        if not isinstance(goal, dict) or not isinstance(goal.get("objective"), str):
            return None
        return GoalInfo(
            thread_id=thread_id,
            objective=goal.get("objective") or goal.get("text"),
            status=goal.get("status"),
            present=True,
            token_budget=goal.get("tokenBudget"),
            tokens_used=goal.get("tokensUsed", 0),
        )

    async def set_goal(self, thread_id: str, objective: str) -> GoalInfo:
        result = await self._request(
            "thread/goal/set", {"threadId": thread_id, "objective": objective}
        )
        goal = result.get("goal") if isinstance(result, dict) else None
        if not isinstance(goal, dict) or goal.get("objective") != objective:
            raise AppServerError("Codex did not confirm the requested goal; inspect it in Codex.")
        return GoalInfo(
            thread_id=thread_id,
            objective=goal.get("objective") or objective,
            status=goal.get("status"),
            present=True,
        )

    @staticmethod
    def _turn_summary(turn: object) -> dict:
        if not isinstance(turn, dict):
            return {}
        error = turn.get("error")
        return {
            "id": turn.get("id"),
            "status": turn.get("status"),
            "error": {"codexErrorInfo": error.get("codexErrorInfo")}
            if isinstance(error, dict)
            else None,
        }

    async def latest_turn(self, thread_id: str) -> dict | None:
        result = await self._request(
            "thread/turns/list",
            {"threadId": thread_id, "limit": 1, "sortDirection": "desc", "itemsView": "notLoaded"},
        )
        data = result.get("data")
        if not isinstance(data, list) or len(data) > 1:
            raise AppServerError("Could not verify the latest conversation turn.")
        if not data:
            return None
        turn = data[0]
        if not isinstance(turn, dict) or not isinstance(turn.get("id"), str):
            raise AppServerError("Could not verify the latest conversation turn.")
        return self._turn_summary(turn)

    async def observed_turn(self, thread_id: str) -> dict | None:
        turn = await self.latest_turn(thread_id)
        if not turn or turn.get("status") != "interrupted":
            return turn
        result = await self._request("thread/read", {"threadId": thread_id, "includeTurns": False})
        thread = result.get("thread") or {}
        if thread.get("id") != thread_id or not isinstance(thread.get("path"), str):
            raise AppServerError("Could not verify the live conversation state.")
        from codex_account_manager.codex.turn_activity import read_turn_activity

        state = await asyncio.to_thread(
            read_turn_activity, thread["path"], Path(self.codex_home), turn["id"]
        )
        if state == "unknown":
            raise AppServerError("Could not verify the live conversation state.")
        return {**turn, "status": state}

    async def resume_for_continuation(self, thread_id: str) -> None:
        await self.require_headless_compatible(thread_id)
        result = await self._request("thread/resume", {"threadId": thread_id, "excludeTurns": True})
        thread = result.get("thread") or {}
        if thread.get("id") != thread_id or (thread.get("status") or {}).get("type") != "idle":
            raise AppServerError("Conversation is not idle; automatic continuation was skipped.")

    async def reactivate_usage_limited_goal(self, thread_id: str, previous: GoalInfo) -> None:
        # Update status only: never recreate the objective or reset its token budget.
        await self._request("thread/goal/set", {"threadId": thread_id, "status": "active"})
        current = await self.get_goal(thread_id)
        if (
            current is None
            or current.status != "active"
            or current.objective != previous.objective
            or current.token_budget != previous.token_budget
            or current.tokens_used < previous.tokens_used
        ):
            raise AppServerError("Could not verify the goal after restoring its active status.")

    async def run_continuation_turn(
        self,
        thread_id: str,
        message_id: str,
        *,
        on_started: Callable[[str], Awaitable[None]] | None = None,
    ) -> dict:
        if self._turn_events is not None:
            raise AppServerError("A continuation is already running on this connection.")
        events: asyncio.Queue[dict] = asyncio.Queue(maxsize=16)
        self._turn_events = events
        turn_id = None
        finished = False
        try:
            current = await self._request("thread/read", {"threadId": thread_id})
            thread = current.get("thread") or {}
            if thread.get("id") != thread_id or (thread.get("status") or {}).get("type") != "idle":
                raise AppServerError("Conversation is already running; no continuation was sent.")
            result = await self._request(
                "turn/start",
                {
                    "threadId": thread_id,
                    "clientUserMessageId": message_id,
                    "input": [
                        {
                            "type": "text",
                            "text": (
                                "Continue the previously requested unfinished work from its current state. "
                                "Follow the existing goal, user instructions, permissions and budget. "
                                "Do not redo completed work. If the work is complete or needs user input, "
                                "report that and stop."
                            ),
                        }
                    ],
                },
            )
            turn = self._turn_summary(result.get("turn"))
            turn_id = turn.get("id")
            if not isinstance(turn_id, str) or not turn_id:
                raise AppServerError("Codex did not confirm the continuation turn.")
            if on_started is not None:
                await on_started(turn_id)
            if turn.get("status") in {"completed", "failed", "interrupted"}:
                finished = True
                return turn
            while True:
                try:
                    event = await asyncio.wait_for(events.get(), timeout=CONTINUATION_POLL_SECONDS)
                except TimeoutError:
                    current_turn = await self.latest_turn(thread_id)
                    if not current_turn or current_turn.get("id") != turn_id:
                        raise AppServerError(
                            "The owned continuation turn could not be verified."
                        ) from None
                    if current_turn.get("status") == "inProgress":
                        continue
                    event = {"threadId": thread_id, "turn": current_turn}
                if event.get("closed"):
                    raise AppServerError(
                        "Continuation connection closed; inspect the conversation before retrying."
                    )
                if event.get("request"):
                    raise AppServerError(
                        "Continuation needs approval or user input. Open the conversation in Codex."
                    )
                completed = event.get("turn") or {}
                if event.get("threadId") == thread_id and completed.get("id") == turn_id:
                    if completed.get("status") not in {"completed", "failed", "interrupted"}:
                        raise AppServerError("Codex returned an unknown continuation status.")
                    finished = True
                    return completed
        finally:
            if turn_id and not finished:
                try:
                    await self._request(
                        "turn/interrupt", {"threadId": thread_id, "turnId": turn_id}
                    )
                except Exception:
                    pass
            self._turn_events = None


def _requires_sign_in(error: Exception) -> bool:
    return isinstance(error, SignInRequiredError) or any(
        code in str(error).casefold()
        for code in (
            "refresh_token_expired",
            "refresh_token_revoked",
            "refresh_token_reused",
            "invalid_grant",
        )
    )


def _display_email(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 320 or "@" not in value:
        return None
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    return value
