"""Persist observed turn identities and goal metadata, never conversation text."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from hashlib import sha256
from typing import ClassVar

from codex_account_manager.adapters.interfaces import GoalInfo
from codex_account_manager.codex.status import normalize_turn_status
from codex_account_manager.storage.database import connect


def goal_signature(goal: GoalInfo) -> str:
    return json.dumps(
        {
            "present": goal.present,
            "objective_hash": sha256((goal.objective or "").encode()).hexdigest(),
            "budget": goal.token_budget,
        },
        sort_keys=True,
    )


@dataclass(frozen=True)
class ObservedWork:
    thread_id: str
    account_hash: str
    turn_status: str
    goal_status: str | None
    goal_present: bool
    limited: bool
    observed_at: float
    verified: bool = True
    unavailable_reason: str | None = None
    continuation_pending: bool = False
    continuation_status: str | None = None
    turn_id: str | None = None


class WorkTracker:
    generation: ClassVar[int] = 0

    async def reset(self) -> None:
        from codex_account_manager.core.events import bus
        from codex_account_manager.storage.repositories import SettingsRepository

        await SettingsRepository().set("monitor_enabled", "false")
        WorkTracker.generation += 1
        bus.publish("continuation.stop")
        bus.publish("monitor.settings_changed")
        await self.forget()
        bus.publish("work.observed")

    async def visible(
        self, owner_key: str | None = None, *, verified_only: bool = False
    ) -> list[ObservedWork]:
        from codex_account_manager.continuity.desktop_goal import OBSERVATION_SECONDS
        from codex_account_manager.monitoring.settings import poll_interval
        from codex_account_manager.storage.repositories import SettingsRepository

        freshness = max(180, poll_interval(await SettingsRepository().get("poll_seconds")) + 30)
        now = time.time()
        async with connect() as db:
            rows = await (
                await db.execute(
                    """SELECT thread_id, account_hash, turn_status, goal_fingerprint,
                goal_status, limited, observed_at, verified, unavailable_reason,
                EXISTS(
                    SELECT 1 FROM pending_continuations AS pending
                    WHERE pending.thread_id=observed_work.thread_id
                      AND pending.turn_id=observed_work.turn_id
                      AND pending.owner_key=observed_work.account_hash
                      AND pending.created_at > ? AND pending.created_at <= ?
                      AND (pending.created_at > ? OR NOT EXISTS (
                          SELECT 1 FROM continuation_attempts AS activation
                          WHERE activation.thread_id=pending.thread_id
                            AND activation.source_turn_id=pending.turn_id
                            AND activation.status IN ('goal_opened','goal_reopened')
                      ))
                ),
                (
                    SELECT attempts.status FROM continuation_attempts AS attempts
                    WHERE attempts.thread_id=observed_work.thread_id
                      AND attempts.source_turn_id=observed_work.turn_id
                ), turn_id
                FROM observed_work
                    WHERE account_hash <> '' AND observed_at >= ?
                      AND (? = 0 OR (verified=1 AND observed_at BETWEEN ? AND ?))
                  AND (? IS NULL OR account_hash = ?)
                ORDER BY observed_at DESC LIMIT 50""",
                    (
                        now - 600,
                        now,
                        now - OBSERVATION_SECONDS,
                        now - 86400,
                        int(verified_only),
                        now - freshness,
                        now,
                        owner_key,
                        owner_key,
                    ),
                )
            ).fetchall()
        return [
            ObservedWork(
                thread_id=row[0],
                account_hash=row[1],
                turn_status=row[2],
                goal_status=row[4],
                goal_present=bool(json.loads(row[3])["present"]),
                limited=bool(row[5]),
                observed_at=row[6],
                verified=bool(row[7]) and 0 <= now - row[6] <= freshness,
                unavailable_reason=row[8],
                continuation_pending=bool(row[9]),
                continuation_status=row[10],
                turn_id=row[11],
            )
            for row in rows
        ]

    async def forget(self, thread_ids: list[str] | None = None) -> None:
        async with connect() as db:
            if thread_ids is None:
                await db.execute("DELETE FROM observed_work")
                await db.execute("DELETE FROM pending_continuations")
                await db.execute("DELETE FROM conversation_cooldowns")
            else:
                await db.executemany(
                    "DELETE FROM observed_work WHERE thread_id=?", [(tid,) for tid in thread_ids]
                )
                await db.executemany(
                    "DELETE FROM pending_continuations WHERE thread_id=?",
                    [(tid,) for tid in thread_ids],
                )
                await db.executemany(
                    "DELETE FROM conversation_cooldowns WHERE thread_id=?",
                    [(tid,) for tid in thread_ids],
                )
            await db.commit()

    async def reconcile(self, listed_ids: set[str]) -> None:
        async with connect() as db:
            rows = await (await db.execute("SELECT thread_id FROM observed_work")).fetchall()
            await db.executemany(
                "DELETE FROM observed_work WHERE thread_id=?",
                [(row[0],) for row in rows if row[0] not in listed_ids],
            )
            if listed_ids:
                cooldown_rows = await (
                    await db.execute("SELECT thread_id FROM conversation_cooldowns")
                ).fetchall()
                await db.executemany(
                    "DELETE FROM conversation_cooldowns WHERE thread_id=?",
                    [(row[0],) for row in cooldown_rows if row[0] not in listed_ids],
                )
            else:
                await db.execute("DELETE FROM conversation_cooldowns")
            await db.commit()

    async def unverified(self, thread_ids: list[str] | None = None) -> None:
        async with connect() as db:
            if thread_ids is None:
                await db.execute("UPDATE observed_work SET verified=0")
            else:
                await db.executemany(
                    "UPDATE observed_work SET verified=0 WHERE thread_id=?",
                    [(tid,) for tid in thread_ids],
                )
            await db.commit()

    async def cooling_down_ids(self, now: float | None = None) -> set[str]:
        now = time.time() if now is None else now
        async with connect() as db:
            cooldown_rows = await (
                await db.execute(
                    "SELECT thread_id FROM conversation_cooldowns WHERE retry_after > ?",
                    (now,),
                )
            ).fetchall()
            observed_rows = await (
                await db.execute(
                    """SELECT thread_id FROM observed_work
                    WHERE unavailable_reason IS NOT NULL AND unavailable_until > ?""",
                    (now,),
                )
            ).fetchall()
        return {row[0] for row in (*cooldown_rows, *observed_rows)}

    async def mark_unavailable(
        self, thread_id: str, reason: str, *, cooldown_seconds: float = 600
    ) -> None:
        async with connect() as db:
            now = time.time()
            await db.execute(
                """INSERT INTO conversation_cooldowns
                (thread_id, reason, retry_after, updated_at) VALUES (?, ?, ?, ?)
                ON CONFLICT(thread_id) DO UPDATE SET reason=excluded.reason,
                retry_after=excluded.retry_after, updated_at=excluded.updated_at""",
                (thread_id, reason, now + cooldown_seconds, now),
            )
            await db.execute(
                """UPDATE observed_work SET verified=0, unavailable_reason=?,
                unavailable_until=?, observed_at=? WHERE thread_id=?""",
                (reason, now + cooldown_seconds, now, thread_id),
            )
            await db.commit()

    async def ids(self, owner_key: str | None = None) -> list[str]:
        async with connect() as db:
            await db.execute(
                "DELETE FROM observed_work WHERE observed_at<?", (time.time() - 86400,)
            )
            await db.commit()
            rows = await (
                await db.execute(
                    "SELECT thread_id FROM observed_work WHERE (? IS NULL OR account_hash=?) ORDER BY observed_at DESC",
                    (owner_key, owner_key),
                )
            ).fetchall()
        return [row[0] for row in rows]

    async def observe(
        self,
        owner_key: str,
        thread_id: str,
        turn: dict | None,
        goal: GoalInfo | None,
        *,
        limited: bool = False,
        new_turn: bool = False,
        generation: int | None = None,
    ) -> None:
        async with connect() as db:
            row = await (
                await db.execute(
                    "SELECT account_hash, turn_id, turn_status, goal_fingerprint FROM observed_work WHERE thread_id=?",
                    (thread_id,),
                )
            ).fetchone()
            if generation is not None and generation != WorkTracker.generation:
                return
            source = owner_key
            status = normalize_turn_status(turn.get("status")) if turn else None
            turn_id = turn.get("id") if turn else None
            signature = goal_signature(goal) if goal is not None else None
            if row and not row[0].startswith("owner-v1:") and status != "inProgress":
                await db.execute(
                    "UPDATE observed_work SET verified=0 WHERE thread_id=?", (thread_id,)
                )
                await db.commit()
                return
            if row and row[0] != source:
                if status != "inProgress" and limited and signature == row[3]:
                    source = row[0]
                    new_turn = new_turn or row[1] != turn_id
                elif status != "inProgress":
                    # Retire completed work from the previous account.
                    if (
                        status == "completed"
                        and not limited
                        and goal is not None
                        and (not goal.present or goal.status in {"complete", "completed"})
                    ):
                        await db.execute(
                            "DELETE FROM observed_work WHERE thread_id=?", (thread_id,)
                        )
                        await db.commit()
                    else:
                        await db.execute(
                            "UPDATE observed_work SET verified=0 WHERE thread_id=?", (thread_id,)
                        )
                        await db.commit()
                    return
                else:
                    row = None
            await db.execute("DELETE FROM conversation_cooldowns WHERE thread_id=?", (thread_id,))
            if (
                row
                and row[2] == "awaitingDesktop"
                and row[1] == turn_id
                and row[3] == signature
                and limited
            ):
                await db.execute(
                    """UPDATE observed_work SET observed_at=?, verified=1,
                    unavailable_reason=NULL, unavailable_until=NULL WHERE thread_id=?""",
                    (time.time(), thread_id),
                )
                await db.commit()
                return
            new_turn = new_turn or bool(
                row and row[1] != turn_id and row[2] == "inProgress" and row[3] == signature
            )
            transition = bool(
                limited
                and row
                and row[1] == turn_id
                and row[2] in {"inProgress", "failed", "completed"}
                and row[3] == signature
            )
            if (status == "inProgress" or (new_turn and limited)) and turn_id and signature:
                await db.execute(
                    """INSERT INTO observed_work (
                    thread_id, account_hash, turn_id, turn_status, goal_fingerprint,
                    goal_status, limited, observed_at, verified, unavailable_reason,
                    unavailable_until
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, NULL, NULL)
                    ON CONFLICT(thread_id) DO UPDATE SET
                    account_hash=excluded.account_hash,
                    turn_id=excluded.turn_id, turn_status=excluded.turn_status,
                    goal_fingerprint=excluded.goal_fingerprint, limited=excluded.limited,
                    goal_status=excluded.goal_status,
                    observed_at=excluded.observed_at, verified=1,
                    unavailable_reason=NULL, unavailable_until=NULL""",
                    (
                        thread_id,
                        source,
                        turn_id,
                        status,
                        signature,
                        goal.status if goal else None,
                        int(new_turn and limited),
                        time.time(),
                    ),
                )
            elif transition:
                await db.execute(
                    """UPDATE observed_work SET turn_status=?, goal_status=?, limited=1,
                    observed_at=?, verified=1, unavailable_reason=NULL,
                    unavailable_until=NULL
                    WHERE thread_id=?""",
                    (status, goal.status if goal else None, time.time(), thread_id),
                )
            else:
                await db.execute("DELETE FROM observed_work WHERE thread_id=?", (thread_id,))
            await db.commit()

    async def unfinished(self) -> list[tuple[str, str, str]]:
        async with connect() as db:
            rows = await (
                await db.execute(
                    """SELECT thread_id, turn_id, goal_fingerprint FROM observed_work
                WHERE account_hash <> '' AND observed_at >= ? ORDER BY observed_at DESC""",
                    (time.time() - 86400,),
                )
            ).fetchall()
        return [(row[0], row[1], row[2]) for row in rows]

    async def await_desktop(
        self,
        source_account: str | None,
        target_account: str,
        thread_ids: list[str],
        *,
        turn_ids: dict[str, str] | None = None,
    ) -> None:
        if not thread_ids:
            return
        async with connect() as db:
            await db.executemany(
                """UPDATE observed_work SET account_hash=?, turn_status='awaitingDesktop',
                turn_id=COALESCE(?, turn_id), limited=0, observed_at=?
                WHERE (? IS NULL OR (account_hash=? AND limited=1)) AND thread_id=?""",
                [
                    (
                        target_account,
                        (turn_ids or {}).get(thread_id),
                        time.time(),
                        source_account if source_account is not None else None,
                        source_account if source_account is not None else None,
                        thread_id,
                    )
                    for thread_id in thread_ids
                ],
            )
            await db.commit()

    async def clear(self, owner_key: str, thread_ids: list[str]) -> None:
        if not thread_ids:
            return
        async with connect() as db:
            await db.executemany(
                "DELETE FROM observed_work WHERE account_hash=? AND thread_id=?",
                [(owner_key, thread_id) for thread_id in thread_ids],
            )
            await db.commit()

    async def limited(self, owner_key: str) -> list[tuple[str, str, str]]:
        async with connect() as db:
            rows = await (
                await db.execute(
                    """SELECT thread_id, turn_id, goal_fingerprint FROM observed_work
                WHERE account_hash=? AND limited=1 AND verified=1 AND observed_at>=?
                ORDER BY observed_at DESC""",
                    (owner_key, time.time() - 3600),
                )
            ).fetchall()
        return [(row[0], row[1], row[2]) for row in rows]
