from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import aiosqlite

from software_factory.events import FactoryEvent
from software_factory.state import FINAL_STATUSES, TRANSITIONS


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
                    control_version INTEGER NOT NULL DEFAULT 2,
                    stop_target TEXT,
                    result_json TEXT,
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
                CREATE TABLE IF NOT EXISTS factory_operations (
                    operation_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    step TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    role TEXT,
                    request TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'prepared',
                    thread_id TEXT,
                    execution_id TEXT,
                    result_json TEXT,
                    deadline_at REAL NOT NULL,
                    repeat_rule TEXT NOT NULL DEFAULT 'never',
                    dispatch_count INTEGER NOT NULL DEFAULT 0,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    UNIQUE (run_id, step, attempt, kind)
                );
                CREATE TABLE IF NOT EXISTS factory_processes (
                    process_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    process_group INTEGER NOT NULL,
                    identity TEXT NOT NULL
                );
                """
            )
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute("PRAGMA table_info(factory_runs)")
            columns = {str(row[1]) for row in await cursor.fetchall()}
            if "deadline_at" not in columns:
                await db.execute("ALTER TABLE factory_runs ADD COLUMN deadline_at REAL")
            for name, definition in (
                ("control_version", "INTEGER NOT NULL DEFAULT 1"),
                ("stop_target", "TEXT"),
                ("result_json", "TEXT"),
            ):
                if name not in columns:
                    await db.execute(f"ALTER TABLE factory_runs ADD COLUMN {name} {definition}")
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
                """INSERT INTO factory_runs
                   (run_id, status, workspace, parent_thread_id, deadline_at, control_version)
                   VALUES (?, 'pending', ?, ?, ?, 2)""",
                (run_id, str(workspace.resolve()), parent_thread_id, deadline_at),
            )
            await db.commit()

    async def set_deadline(self, run_id: str, deadline_at: float) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE factory_runs SET deadline_at = ? WHERE run_id = ?",
                (deadline_at, run_id),
            )
            await db.commit()

    async def set_status(
        self,
        run_id: str,
        status: str,
        error: str | None = None,
        *,
        result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Compare and change state under the same write transaction as its event."""
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute("SELECT * FROM factory_runs WHERE run_id = ?", (run_id,))
            row = await cursor.fetchone()
            if row is None:
                raise ValueError(f"unknown run ID: {run_id}")
            record = dict(row)
            current = str(record["status"])
            if current in FINAL_STATUSES or (
                record["stop_target"] and status in {"accepted", "running", "waiting_for_input"}
            ):
                return record
            if status != current and status not in TRANSITIONS[current]:
                raise ValueError(f"invalid run state change: {current} -> {status}")
            if record["stop_target"] and status in FINAL_STATUSES:
                status = str(record["stop_target"])
                error = record["error"]
            if status in FINAL_STATUSES:
                cursor = await db.execute(
                    "SELECT 1 FROM factory_processes WHERE run_id = ? LIMIT 1", (run_id,)
                )
                active_process = await cursor.fetchone()
                cursor = await db.execute(
                    "SELECT 1 FROM factory_operations WHERE run_id = ? AND state IN ('dispatching', 'dispatched') LIMIT 1",
                    (run_id,),
                )
                active_operation = await cursor.fetchone()
                if (
                    record["active_child_thread_id"]
                    or record["active_process_group"]
                    or active_process
                    or active_operation
                ):
                    raise ValueError("cannot finish while owned work is active or uncertain")
            await db.execute(
                """UPDATE factory_runs SET status = ?, error = ?, updated_at = CURRENT_TIMESTAMP
                   WHERE run_id = ?""",
                (status, error, run_id),
            )
            if result is not None:
                payload = {**result, "status": status, "error": error, "final_report_path": ""}
                await db.execute(
                    "UPDATE factory_runs SET result_json = ? WHERE run_id = ?",
                    (json.dumps(payload), run_id),
                )
            if status != current:
                event = FactoryEvent(
                    run_id=run_id,
                    node="state",
                    status="info",
                    message=f"run state: {status}",
                    metadata={"previous_status": current, "status": status, "error": error},
                )
                await self._append_event(db, event)
            await db.commit()
            return {**record, "status": status, "error": error}

    async def request_stop(
        self, run_id: str, target: str = "cancelled", error: str = "cancelled by user"
    ) -> dict[str, Any]:
        if target not in {"cancelled", "failed", "blocked"}:
            raise ValueError(f"invalid stop outcome: {target}")
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute("SELECT * FROM factory_runs WHERE run_id = ?", (run_id,))
            row = await cursor.fetchone()
            if row is None:
                raise ValueError(f"unknown run ID: {run_id}")
            record = dict(row)
            if record["status"] in FINAL_STATUSES:
                return record
            if record["stop_target"] == "cancelled":
                target, error = "cancelled", str(record["error"])
            await db.execute(
                "UPDATE factory_runs SET status = 'stopping', stop_target = ?, error = ? WHERE run_id = ?",
                (target, error, run_id),
            )
            if record["status"] != "stopping" or record["stop_target"] != target:
                await self._append_event(
                    db,
                    FactoryEvent(
                        run_id=run_id,
                        node="state",
                        status="info",
                        message=f"stop requested: {target}",
                        metadata={"status": "stopping", "stop_target": target},
                    ),
                )
            await db.commit()
            return {**record, "status": "stopping", "stop_target": target, "error": error}

    async def save_result(self, run_id: str, result: dict[str, Any]) -> dict[str, Any]:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT status, error FROM factory_runs WHERE run_id = ?", (run_id,)
            )
            row = await cursor.fetchone()
            if row is None:
                raise ValueError(f"unknown run ID: {run_id}")
            result = {**result, "status": row[0], "error": row[1]}
            await db.execute(
                "UPDATE factory_runs SET result_json = ? WHERE run_id = ?",
                (json.dumps(result), run_id),
            )
            await db.commit()
            return result

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
                "SELECT status, stop_target FROM factory_runs WHERE run_id = ?", (run_id,)
            )
            row = await cursor.fetchone()
            if row is None:
                await db.rollback()
                raise RuntimeError(f"unknown run ID while registering child: {run_id}")
            if str(row[0]) in FINAL_STATUSES | {"stopping"} or row[1]:
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
            await db.execute("BEGIN IMMEDIATE")
            event = await self._append_event(db, event)
            await db.commit()
            return event

    async def _append_event(self, db: aiosqlite.Connection, event: FactoryEvent) -> FactoryEvent:
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
        return event

    async def prepare_operation(
        self,
        run_id: str,
        step: str,
        attempt: int,
        kind: str,
        request: str,
        *,
        role: str | None = None,
        timeout_seconds: float = 900,
        repeat_rule: str = "never",
    ) -> dict[str, Any]:
        operation_id = f"{run_id}:{step}:{attempt}:{kind}"
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT deadline_at FROM factory_runs WHERE run_id = ?", (run_id,)
            )
            run = await cursor.fetchone()
            if run is None:
                raise ValueError(f"unknown run ID: {run_id}")
            deadline = min(time.time() + timeout_seconds, run[0] or float("inf"))
            await db.execute(
                """INSERT OR IGNORE INTO factory_operations
                   (operation_id, run_id, step, attempt, kind, role, request, deadline_at, repeat_rule)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (operation_id, run_id, step, attempt, kind, role, request, deadline, repeat_rule),
            )
            cursor = await db.execute(
                "SELECT * FROM factory_operations WHERE operation_id = ?", (operation_id,)
            )
            row = await cursor.fetchone()
            assert row is not None
            if row["request"] != request or row["repeat_rule"] != repeat_rule:
                raise ValueError(f"saved operation differs from requested work: {operation_id}")
            await db.commit()
            return dict(row)

    async def operations(self, run_id: str) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM factory_operations WHERE run_id = ? ORDER BY rowid", (run_id,)
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def update_operation(self, operation_id: str, **values: Any) -> None:
        allowed = {
            "state",
            "thread_id",
            "execution_id",
            "result_json",
            "dispatch_count",
        }
        if not values or not values.keys() <= allowed:
            raise ValueError("invalid operation update")
        async with aiosqlite.connect(self.path) as db:
            fields = ", ".join(f"{key} = ?" for key in values)
            await db.execute(
                f"UPDATE factory_operations SET {fields} WHERE operation_id = ?",
                (*values.values(), operation_id),
            )
            await db.commit()

    async def claim_retry(self, operation_id: str, limit: int) -> int | None:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """UPDATE factory_operations SET retry_count = retry_count + 1
                   WHERE operation_id = ? AND retry_count < ? RETURNING retry_count""",
                (operation_id, limit),
            )
            row = await cursor.fetchone()
            await cursor.close()
            await db.commit()
            return int(row[0]) if row is not None else None

    async def register_process(
        self, run_id: str, process_id: str, kind: str, group: int, identity: str
    ) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO factory_processes VALUES (?, ?, ?, ?, ?)",
                (process_id, run_id, kind, group, identity),
            )
            await db.commit()

    async def processes(self, run_id: str) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM factory_processes WHERE run_id = ?", (run_id,))
            return [dict(row) for row in await cursor.fetchall()]

    async def clear_process(self, process_id: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("DELETE FROM factory_processes WHERE process_id = ?", (process_id,))
            await db.commit()

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
