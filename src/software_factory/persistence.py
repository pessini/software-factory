from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import aiosqlite

from software_factory.events import FactoryEvent


class RunStore:
    """Durable side-effect ledger kept beside LangGraph checkpoints."""

    def __init__(self, path: Path) -> None:
        self.path = path

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.path) as db:
            await db.executescript(
                """
                CREATE TABLE IF NOT EXISTS factory_runs (
                    run_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    workspace TEXT NOT NULL,
                    parent_thread_id TEXT NOT NULL,
                    deadline_at REAL,
                    active_child_thread_id TEXT,
                    active_process_group INTEGER,
                    error TEXT,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS factory_children (
                    run_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    PRIMARY KEY (run_id, role)
                );
                CREATE TABLE IF NOT EXISTS factory_child_intents (
                    run_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    spawn_key TEXT NOT NULL,
                    thread_id TEXT,
                    PRIMARY KEY (run_id, role),
                    UNIQUE (spawn_key)
                );
                CREATE TABLE IF NOT EXISTS factory_events (
                    run_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (run_id, sequence)
                );
                CREATE TABLE IF NOT EXISTS factory_sessions (
                    session_id TEXT PRIMARY KEY,
                    cwd TEXT NOT NULL,
                    run_id TEXT
                );
                """
            )
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute("PRAGMA table_info(factory_runs)")
            columns = {str(row[1]) for row in await cursor.fetchall()}
            if "deadline_at" not in columns:
                await db.execute("ALTER TABLE factory_runs ADD COLUMN deadline_at REAL")
            await db.commit()

    async def create_run(
        self,
        run_id: str,
        workspace: Path,
        parent_thread_id: str,
        *,
        deadline_at: float | None = None,
    ) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """INSERT OR IGNORE INTO factory_runs
                   (run_id, status, workspace, parent_thread_id, deadline_at)
                   VALUES (?, 'pending', ?, ?, ?)""",
                (run_id, str(workspace), parent_thread_id, deadline_at),
            )
            await db.commit()

    async def set_deadline(self, run_id: str, deadline_at: float) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE factory_runs SET deadline_at = ? WHERE run_id = ?",
                (deadline_at, run_id),
            )
            await db.commit()

    async def set_status(self, run_id: str, status: str, error: str | None = None) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """UPDATE factory_runs SET status = ?, error = ?, updated_at = CURRENT_TIMESTAMP
                   WHERE run_id = ?""",
                (status, error, run_id),
            )
            await db.commit()

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM factory_runs WHERE run_id = ?", (run_id,))
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def list_children(self, run_id: str) -> dict[str, str]:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                "SELECT role, thread_id FROM factory_children WHERE run_id = ?", (run_id,)
            )
            return {str(role): str(thread_id) for role, thread_id in await cursor.fetchall()}

    async def child(self, run_id: str, role: str) -> str | None:
        return (await self.list_children(run_id)).get(role)

    async def prepare_child_spawn(self, run_id: str, role: str) -> str:
        spawn_key = f"{run_id}:{role}"
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """INSERT OR IGNORE INTO factory_child_intents
                   (run_id, role, spawn_key) VALUES (?, ?, ?)""",
                (run_id, role, spawn_key),
            )
            cursor = await db.execute(
                """SELECT spawn_key FROM factory_child_intents
                   WHERE run_id = ? AND role = ?""",
                (run_id, role),
            )
            row = await cursor.fetchone()
            await db.commit()
        if row is None:
            raise RuntimeError(f"failed to persist child spawn intent: {run_id}/{role}")
        return str(row[0])

    async def save_child(self, run_id: str, role: str, thread_id: str) -> bool:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT status FROM factory_runs WHERE run_id = ?", (run_id,)
            )
            row = await cursor.fetchone()
            if row is None:
                await db.rollback()
                raise RuntimeError(f"unknown run ID while registering child: {run_id}")
            if str(row[0]) in {"accepted", "blocked", "cancelled", "failed"}:
                await db.rollback()
                return False
            await db.execute(
                """INSERT INTO factory_child_intents (run_id, role, spawn_key, thread_id)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(run_id, role) DO UPDATE SET thread_id = excluded.thread_id""",
                (run_id, role, f"{run_id}:{role}", thread_id),
            )
            await db.execute(
                """INSERT INTO factory_children (run_id, role, thread_id) VALUES (?, ?, ?)
                   ON CONFLICT(run_id, role) DO UPDATE SET thread_id = excluded.thread_id""",
                (run_id, role, thread_id),
            )
            await db.execute(
                "UPDATE factory_runs SET active_child_thread_id = ? WHERE run_id = ?",
                (thread_id, run_id),
            )
            await db.commit()
            return True

    async def clear_active_child(self, run_id: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE factory_runs SET active_child_thread_id = NULL WHERE run_id = ?", (run_id,)
            )
            await db.commit()

    async def set_process_group(self, run_id: str, process_group: int | None) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE factory_runs SET active_process_group = ? WHERE run_id = ?",
                (process_group, run_id),
            )
            await db.commit()

    async def append_event(self, event: FactoryEvent) -> FactoryEvent:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM factory_events WHERE run_id = ?",
                (event.run_id,),
            )
            row = await cursor.fetchone()
            sequence = int(row[0]) if row else 1
            event = event.model_copy(update={"sequence": sequence})
            await db.execute(
                "INSERT INTO factory_events (run_id, sequence, payload) VALUES (?, ?, ?)",
                (event.run_id, sequence, event.model_dump_json()),
            )
            await db.commit()
            return event

    async def events(self, run_id: str) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                "SELECT payload FROM factory_events WHERE run_id = ? ORDER BY sequence", (run_id,)
            )
            return [json.loads(payload) for (payload,) in await cursor.fetchall()]

    async def save_session(self, session_id: str, cwd: Path, run_id: str | None = None) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """INSERT INTO factory_sessions (session_id, cwd, run_id) VALUES (?, ?, ?)
                   ON CONFLICT(session_id) DO UPDATE SET cwd = excluded.cwd,
                     run_id = COALESCE(excluded.run_id, factory_sessions.run_id)""",
                (session_id, str(cwd), run_id),
            )
            await db.commit()

    async def session(self, session_id: str) -> dict[str, str | None] | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT session_id, cwd, run_id FROM factory_sessions WHERE session_id = ?",
                (session_id,),
            )
            row = await cursor.fetchone()
            return dict(row) if row else None
