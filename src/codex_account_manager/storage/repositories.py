"""Repositories: the only place that reads/writes the SQLite database.

Each repository maps rows to domain models. Credentials are never stored here.
Repositories are async and use short-lived connections via
:func:`codex_account_manager.storage.database.connect`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from codex_account_manager.core.redaction import redact, redact_text
from codex_account_manager.domain.identity import AccountIdentity
from codex_account_manager.domain.models import (
    GoalCheckpoint,
    HandoffRecord,
    Profile,
    ThreadRecord,
)
from codex_account_manager.domain.states import GoalState, HandoffReason
from codex_account_manager.storage.database import connect


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class ProfileRepository:
    async def list(self) -> list[Profile]:
        async with connect() as db:
            cursor = await db.execute(
                """
                SELECT p.id, p.alias, p.codex_home, pa.account_id, p.created_at, pa.user_id
                FROM profiles p
                LEFT JOIN profile_accounts pa ON pa.profile_id = p.id
                ORDER BY p.created_at
                """
            )
            rows = await cursor.fetchall()
        return [self._row(r) for r in rows]

    async def get_by_alias(self, alias: str) -> Profile | None:
        async with connect() as db:
            cursor = await db.execute(
                """
                SELECT p.id, p.alias, p.codex_home, pa.account_id, p.created_at, pa.user_id
                FROM profiles p
                LEFT JOIN profile_accounts pa ON pa.profile_id = p.id
                WHERE p.alias = ?
                """,
                (alias,),
            )
            row = await cursor.fetchone()
        return self._row(row) if row else None

    async def create(self, profile: Profile) -> None:
        async with connect() as db:
            await db.execute(
                "INSERT INTO profiles (id, alias, codex_home, created_at) VALUES (?, ?, ?, ?)",
                (profile.id, profile.alias, profile.codex_home, profile.created_at.isoformat()),
            )
            await db.commit()

    async def rename(self, profile_id: str, new_alias: str) -> None:
        async with connect() as db:
            await db.execute("UPDATE profiles SET alias = ? WHERE id = ?", (new_alias, profile_id))
            await db.commit()

    async def delete(self, profile_id: str) -> None:
        async with connect() as db:
            await db.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
            await db.commit()

    async def bind_account(
        self, profile_id: str, account_id: str, *, user_id: str | None = None
    ) -> None:
        async with connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            if user_id is None:
                previous = await (
                    await db.execute(
                        "SELECT user_id FROM profile_accounts WHERE profile_id=?", (profile_id,)
                    )
                ).fetchone()
                if previous and previous[0] is not None:
                    raise ValueError(
                        "A verified user binding cannot be replaced by a workspace hint."
                    )
            await db.execute(
                """
                INSERT INTO profile_accounts (profile_id, account_id, bound_at, user_id)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(profile_id) DO UPDATE SET
                    account_id = excluded.account_id, bound_at = excluded.bound_at,
                    user_id = excluded.user_id
                """,
                (profile_id, account_id, _now_iso(), user_id),
            )
            await db.commit()

    @staticmethod
    def _row(row) -> Profile:
        return Profile(
            id=row[0],
            alias=row[1],
            codex_home=row[2],
            bound_account_id=row[3],
            created_at=datetime.fromisoformat(row[4]),
            bound_user_id=row[5],
        )

    async def upgrade_legacy_identity(self, profile: Profile, identity: AccountIdentity) -> bool:
        if not identity.owner_key or identity.workspace_id != profile.bound_account_id:
            return False
        async with connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            other = await (
                await db.execute(
                    "SELECT 1 FROM profile_accounts WHERE account_id=? AND profile_id<>?",
                    (profile.bound_account_id, profile.id),
                )
            ).fetchone()
            if other:
                return False
            cursor = await db.execute(
                "UPDATE profile_accounts SET user_id=?, bound_at=? "
                "WHERE profile_id=? AND account_id=? AND user_id IS NULL",
                (identity.user_id, _now_iso(), profile.id, profile.bound_account_id),
            )
            await db.commit()
            return cursor.rowcount == 1


class ThreadRepository:
    _upsert = """
        INSERT INTO threads (id, profile_id, workspace, cwd, preview, last_turn_id,
            last_seen_at, created_at, updated_at, title, source, project_id, model_provider, is_listed)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
        ON CONFLICT(id) DO UPDATE SET
            workspace=excluded.workspace, cwd=excluded.cwd, preview=excluded.preview,
            last_seen_at=excluded.last_seen_at, updated_at=excluded.updated_at,
            title=excluded.title, source=excluded.source, project_id=excluded.project_id,
            model_provider=excluded.model_provider, is_listed=1
    """

    @staticmethod
    def _values(record: ThreadRecord) -> tuple:
        return (
            record.id,
            record.profile_id,
            record.workspace,
            record.cwd,
            record.preview,
            record.last_turn_id,
            record.last_seen_at.isoformat(),
            record.created_at.isoformat(),
            record.updated_at.isoformat(),
            record.title,
            record.source,
            record.project_id,
            record.model_provider,
        )

    @staticmethod
    def _row(row) -> ThreadRecord:
        return ThreadRecord(
            id=row[0],
            profile_id=row[1],
            workspace=row[2],
            cwd=row[3],
            preview=row[4],
            last_turn_id=row[5],
            last_seen_at=datetime.fromisoformat(row[6]),
            created_at=datetime.fromisoformat(row[7]),
            updated_at=datetime.fromisoformat(row[8]),
            title=row[9],
            source=row[10],
            project_id=row[11],
            model_provider=row[12],
        )

    async def upsert(self, record: ThreadRecord) -> None:
        async with connect() as db:
            await db.execute(self._upsert, self._values(record))
            await db.commit()

    async def replace_listed(self, records: list[ThreadRecord]) -> None:
        """Publish one complete snapshot; failed syncs leave the previous list intact."""
        async with connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute("UPDATE threads SET is_listed=0")
            await db.executemany(self._upsert, [self._values(record) for record in records])
            await db.commit()

    async def get(self, thread_id: str) -> ThreadRecord | None:
        async with connect() as db:
            cursor = await db.execute(
                "SELECT id, profile_id, workspace, cwd, preview, last_turn_id, last_seen_at, created_at, updated_at, title, source, project_id, model_provider FROM threads WHERE id=?",
                (thread_id,),
            )
            row = await cursor.fetchone()
        return self._row(row) if row else None

    async def list(self, limit: int | None = None) -> list[ThreadRecord]:
        sql = "SELECT id, profile_id, workspace, cwd, preview, last_turn_id, last_seen_at, created_at, updated_at, title, source, project_id, model_provider FROM threads WHERE is_listed=1 ORDER BY updated_at DESC, id ASC"
        args = () if limit is None else (limit,)
        if limit is not None:
            sql += " LIMIT ?"
        async with connect() as db:
            cursor = await db.execute(sql, args)
            rows = await cursor.fetchall()
        return [self._row(row) for row in rows]


class GoalRepository:
    async def upsert(self, goal: GoalCheckpoint) -> None:
        async with connect() as db:
            await db.execute(
                """
                INSERT INTO goals
                    (id, thread_id, objective, local_status, native_goal_status,
                     native_goal_present, workspace, profile_id, revision, user_cleared,
                     created_at, last_seen_at, last_handoff_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    objective = excluded.objective,
                    local_status = excluded.local_status,
                    native_goal_status = excluded.native_goal_status,
                    native_goal_present = excluded.native_goal_present,
                    workspace = excluded.workspace,
                    profile_id = excluded.profile_id,
                    revision = excluded.revision,
                    user_cleared = excluded.user_cleared,
                    last_seen_at = excluded.last_seen_at,
                    last_handoff_at = excluded.last_handoff_at
                """,
                (
                    goal.id,
                    goal.thread_id,
                    goal.objective,
                    goal.local_status.value,
                    goal.native_goal_status,
                    None if goal.native_goal_present is None else int(goal.native_goal_present),
                    goal.workspace,
                    goal.profile_id,
                    goal.revision,
                    int(goal.user_cleared),
                    goal.created_at.isoformat(),
                    goal.last_seen_at.isoformat(),
                    goal.last_handoff_at.isoformat() if goal.last_handoff_at else None,
                ),
            )
            await db.commit()

    async def get_for_thread(self, thread_id: str) -> GoalCheckpoint | None:
        async with connect() as db:
            cursor = await db.execute(
                """
                SELECT id, thread_id, objective, local_status, native_goal_status,
                       native_goal_present, workspace, profile_id, revision, user_cleared,
                       created_at, last_seen_at, last_handoff_at
                FROM goals WHERE thread_id = ? ORDER BY revision DESC LIMIT 1
                """,
                (thread_id,),
            )
            row = await cursor.fetchone()
        return self._row(row) if row else None

    async def list_active(self) -> list[GoalCheckpoint]:
        async with connect() as db:
            cursor = await db.execute(
                """
                SELECT id, thread_id, objective, local_status, native_goal_status,
                       native_goal_present, workspace, profile_id, revision, user_cleared,
                       created_at, last_seen_at, last_handoff_at
                FROM goals ORDER BY last_seen_at DESC
                """
            )
            rows = await cursor.fetchall()
        return [self._row(r) for r in rows]

    @staticmethod
    def _row(row) -> GoalCheckpoint:
        return GoalCheckpoint(
            id=row[0],
            thread_id=row[1],
            objective=row[2],
            local_status=GoalState(row[3]),
            native_goal_status=row[4],
            native_goal_present=None if row[5] is None else bool(row[5]),
            workspace=row[6],
            profile_id=row[7],
            revision=row[8],
            user_cleared=bool(row[9]),
            created_at=datetime.fromisoformat(row[10]),
            last_seen_at=datetime.fromisoformat(row[11]),
            last_handoff_at=datetime.fromisoformat(row[12]) if row[12] else None,
        )


class HandoffRepository:
    async def save(self, handoff: HandoffRecord) -> None:
        async with connect() as db:
            await db.execute(
                """
                INSERT INTO handoffs
                    (id, thread_id, from_profile_id, to_profile_id, reason, started_at,
                     finished_at, success, rolled_back, final_stage, detail)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    finished_at = excluded.finished_at,
                    success = excluded.success,
                    rolled_back = excluded.rolled_back,
                    final_stage = excluded.final_stage,
                    detail = excluded.detail
                """,
                (
                    handoff.id,
                    handoff.thread_id,
                    handoff.from_profile_id,
                    handoff.to_profile_id,
                    handoff.reason.value,
                    handoff.started_at.isoformat(),
                    handoff.finished_at.isoformat() if handoff.finished_at else None,
                    None if handoff.success is None else int(handoff.success),
                    int(handoff.rolled_back),
                    handoff.final_stage,
                    redact_text(handoff.detail) if handoff.detail else None,
                ),
            )
            await db.commit()

    async def recent(self, limit: int = 50) -> list[HandoffRecord]:
        async with connect() as db:
            cursor = await db.execute(
                """
                SELECT id, thread_id, from_profile_id, to_profile_id, reason, started_at,
                       finished_at, success, rolled_back, final_stage, detail
                FROM handoffs ORDER BY started_at DESC LIMIT ?
                """,
                (limit,),
            )
            rows = await cursor.fetchall()
        return [
            HandoffRecord(
                id=r[0],
                thread_id=r[1],
                from_profile_id=r[2],
                to_profile_id=r[3],
                reason=HandoffReason(r[4]),
                started_at=datetime.fromisoformat(r[5]),
                finished_at=datetime.fromisoformat(r[6]) if r[6] else None,
                success=None if r[7] is None else bool(r[7]),
                rolled_back=bool(r[8]),
                final_stage=r[9],
                detail=r[10],
            )
            for r in rows
        ]


class EventRepository:
    HISTORY_LIMIT = 10_000

    async def continuation_history(self, thread_id: str) -> list[dict]:
        async with connect() as db:
            rows = await (
                await db.execute(
                    """SELECT payload, created_at FROM events
                    WHERE topic='continuation.status' AND thread_id=?
                    ORDER BY id DESC LIMIT 20""",
                    (thread_id,),
                )
            ).fetchall()
        return [{"payload": json.loads(payload), "at": at} for payload, at in rows]

    async def append(
        self,
        topic: str,
        *,
        thread_id: str | None = None,
        profile_id: str | None = None,
        payload: dict | None = None,
    ) -> None:
        async with connect() as db:
            await db.execute(
                """
                INSERT INTO events (topic, thread_id, profile_id, payload, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    topic,
                    thread_id,
                    profile_id,
                    json.dumps(redact(payload or {}), ensure_ascii=False),
                    _now_iso(),
                ),
            )
            await db.execute(
                """DELETE FROM events WHERE id <= (
                    SELECT id FROM events ORDER BY id DESC LIMIT 1 OFFSET ?
                )""",
                (self.HISTORY_LIMIT,),
            )
            await db.commit()

    async def timeline(self, thread_id: str, limit: int = 200) -> list[dict]:
        async with connect() as db:
            cursor = await db.execute(
                """
                SELECT topic, payload, created_at FROM events
                WHERE thread_id = ? ORDER BY id ASC LIMIT ?
                """,
                (thread_id, limit),
            )
            rows = await cursor.fetchall()
        out: list[dict] = []
        for topic, payload, created_at in rows:
            try:
                data = json.loads(payload) if payload else {}
            except json.JSONDecodeError:
                data = {}
            out.append({"topic": topic, "payload": data, "at": created_at})
        return out


class SettingsRepository:
    async def get(self, key: str, default: str | None = None) -> str | None:
        async with connect() as db:
            cursor = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
            row = await cursor.fetchone()
        return row[0] if row else default

    async def set(self, key: str, value: str) -> None:
        await self.set_many({key: value})

    async def set_many(self, values: dict[str, str]) -> None:
        async with connect() as db:
            await db.executemany(
                """
                INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                [(key, value, _now_iso()) for key, value in values.items()],
            )
            await db.commit()

    async def all(self) -> dict[str, str]:
        async with connect() as db:
            cursor = await db.execute("SELECT key, value FROM settings")
            rows = await cursor.fetchall()
        return {k: v for k, v in rows}
