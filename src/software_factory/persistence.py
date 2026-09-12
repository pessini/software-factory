from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import aiosqlite

from software_factory.events import FactoryEvent
from software_factory.interactions import Interaction
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
                CREATE TABLE IF NOT EXISTS factory_review_feedback (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS factory_sessions (
                    session_id TEXT PRIMARY KEY,
                    cwd TEXT NOT NULL,
                    run_id TEXT,
                    settings_json TEXT
                );
                CREATE TABLE IF NOT EXISTS factory_active_sessions (
                    cwd TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS factory_session_history (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    entry_key TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    UNIQUE (session_id, entry_key)
                );
                CREATE TABLE IF NOT EXISTS factory_session_runs (
                    session_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    PRIMARY KEY (session_id, run_id)
                );
                CREATE TABLE IF NOT EXISTS factory_interactions (
                    interaction_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    operation_id TEXT,
                    questions_json TEXT NOT NULL,
                    answers_json TEXT,
                    state TEXT NOT NULL DEFAULT 'waiting',
                    created_at REAL NOT NULL,
                    answered_at REAL
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
                ("wait_started_at", "REAL"),
                ("overall_expires_at", "REAL"),
            ):
                if name not in columns:
                    await db.execute(f"ALTER TABLE factory_runs ADD COLUMN {name} {definition}")
            cursor = await db.execute("PRAGMA table_info(factory_sessions)")
            session_columns = {str(row[1]) for row in await cursor.fetchall()}
            for column in ("settings_json", "config_options_json"):
                if column not in session_columns:
                    await db.execute(f"ALTER TABLE factory_sessions ADD COLUMN {column} TEXT")
            await db.execute(
                """INSERT OR IGNORE INTO factory_session_runs (session_id, run_id)
                   SELECT session_id, run_id FROM factory_sessions WHERE run_id IS NOT NULL"""
            )
            await db.commit()

    async def add_review_feedback(self, run_id: str, payload: dict[str, Any]) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO factory_review_feedback (run_id, payload) VALUES (?, ?)",
                (run_id, json.dumps({**payload, "created_at": time.time()})),
            )
            await db.commit()

    async def review_feedback(self, run_id: str) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                "SELECT sequence, payload FROM factory_review_feedback WHERE run_id = ? ORDER BY sequence",
                (run_id,),
            )
            return [{"sequence": row[0], **json.loads(row[1])} for row in await cursor.fetchall()]

    async def create_run(
        self,
        run_id: str,
        workspace: Path,
        parent_thread_id: str,
        *,
        deadline_at: float | None = None,
        overall_expires_at: float | None = None,
    ) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """INSERT INTO factory_runs
                   (run_id, status, workspace, parent_thread_id, deadline_at, overall_expires_at, control_version)
                   VALUES (?, 'pending', ?, ?, ?, ?, 2)""",
                (
                    run_id,
                    str(workspace.resolve()),
                    parent_thread_id,
                    deadline_at,
                    overall_expires_at,
                ),
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
                record["stop_target"]
                and status in {"accepted", "ready_for_review", "running", "waiting_for_input"}
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
            await db.execute(
                "UPDATE factory_interactions SET state = 'cancelled' WHERE run_id = ? AND state IN ('waiting', 'answered')",
                (run_id,),
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

    async def interactions(self, run_id: str) -> list[Interaction]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM factory_interactions WHERE run_id = ? ORDER BY rowid", (run_id,)
            )
            return [Interaction.from_record(dict(row)) for row in await cursor.fetchall()]

    async def interaction(self, run_id: str, interaction_id: str) -> Interaction | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM factory_interactions WHERE run_id = ? AND interaction_id = ?",
                (run_id, interaction_id),
            )
            row = await cursor.fetchone()
            return Interaction.from_record(dict(row)) if row else None

    async def save_interaction(self, interaction: Interaction) -> Interaction:
        """Save a question and the start of its waiting interval together."""
        questions_json = json.dumps([q.model_dump() for q in interaction.questions])
        if not interaction.questions or len({q.id for q in interaction.questions}) != len(
            interaction.questions
        ):
            raise ValueError("an interaction requires questions with unique IDs")
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT * FROM factory_interactions WHERE interaction_id = ?",
                (interaction.interaction_id,),
            )
            previous = await cursor.fetchone()
            if previous is not None:
                saved = Interaction.from_record(dict(previous))
                if (saved.run_id, saved.source, saved.operation_id, saved.questions) != (
                    interaction.run_id,
                    interaction.source,
                    interaction.operation_id,
                    interaction.questions,
                ):
                    raise ValueError("saved interaction differs from this question")
                return saved
            cursor = await db.execute(
                "SELECT * FROM factory_runs WHERE run_id = ?", (interaction.run_id,)
            )
            run = await cursor.fetchone()
            if run is None or run["stop_target"] or run["status"] in FINAL_STATUSES:
                raise ValueError("cannot ask a question for a stopped or unknown run")
            cursor = await db.execute(
                "SELECT 1 FROM factory_interactions WHERE run_id = ? AND state = 'waiting'",
                (interaction.run_id,),
            )
            if await cursor.fetchone():
                raise ValueError("answer the existing interaction before asking another")
            now = time.time()
            await db.execute(
                """INSERT INTO factory_interactions
                   (interaction_id, run_id, source, operation_id, questions_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    interaction.interaction_id,
                    interaction.run_id,
                    interaction.source,
                    interaction.operation_id,
                    questions_json,
                    now,
                ),
            )
            await db.execute(
                "UPDATE factory_runs SET wait_started_at = COALESCE(wait_started_at, ?) WHERE run_id = ?",
                (now, interaction.run_id),
            )
            if run["status"] != "waiting_for_input":
                await db.execute(
                    "UPDATE factory_runs SET status = 'waiting_for_input', error = NULL WHERE run_id = ?",
                    (interaction.run_id,),
                )
                await self._append_event(
                    db,
                    FactoryEvent(
                        run_id=interaction.run_id,
                        node="state",
                        status="info",
                        message="run state: waiting_for_input",
                        metadata={"previous_status": run["status"], "status": "waiting_for_input"},
                    ),
                )
            await self._append_event(
                db,
                FactoryEvent(
                    run_id=interaction.run_id,
                    node="input",
                    status="waiting",
                    message=interaction.display(),
                    metadata={"interaction_id": interaction.interaction_id},
                ),
            )
            await db.commit()
            return interaction

    async def answer_interaction(
        self, run_id: str, interaction_id: str, answers: dict[str, str]
    ) -> bool:
        """Accept one immutable answer; duplicate submissions leave the clocks alone."""
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT * FROM factory_interactions WHERE run_id = ? AND interaction_id = ?",
                (run_id, interaction_id),
            )
            row = await cursor.fetchone()
            if row is None:
                raise ValueError("unknown interaction for this run")
            interaction = Interaction.from_record(dict(row))
            interaction.validate_answers(answers)
            if interaction.answers is not None:
                if interaction.answers != answers:
                    raise ValueError("this interaction already has a different saved answer")
                return False
            cursor = await db.execute("SELECT * FROM factory_runs WHERE run_id = ?", (run_id,))
            run = await cursor.fetchone()
            assert run is not None
            if (
                run["stop_target"]
                or run["status"] in FINAL_STATUSES
                or interaction.state != "waiting"
            ):
                raise ValueError("this interaction is no longer waiting for an answer")
            now = time.time()
            if run["overall_expires_at"] and now >= float(run["overall_expires_at"]):
                raise TimeoutError("overall run expiry exceeded")
            if run["deadline_at"] and float(run["wait_started_at"]) >= float(run["deadline_at"]):
                raise TimeoutError("active work timeout exceeded")
            interval = max(0, now - float(run["wait_started_at"] or now))
            await db.execute(
                """UPDATE factory_runs SET deadline_at = deadline_at + ?, wait_started_at = NULL
                   WHERE run_id = ?""",
                (interval, run_id),
            )
            await db.execute(
                """UPDATE factory_operations SET deadline_at = deadline_at + ?
                   WHERE run_id = ? AND state NOT IN ('completed', 'stopped')""",
                (interval, run_id),
            )
            await db.execute(
                """UPDATE factory_interactions SET answers_json = ?, state = 'answered', answered_at = ?
                   WHERE interaction_id = ?""",
                (json.dumps(answers), now, interaction_id),
            )
            await self._append_event(
                db,
                FactoryEvent(
                    run_id=run_id,
                    node="input",
                    status="completed",
                    message="User answer saved",
                    metadata={"interaction_id": interaction_id},
                ),
            )
            await db.commit()
            return True

    async def deliver_interaction(self, run_id: str, interaction_id: str) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """UPDATE factory_interactions SET state = 'delivered'
                   WHERE run_id = ? AND interaction_id = ? AND state = 'answered'
                   AND EXISTS (SELECT 1 FROM factory_runs WHERE run_id = ? AND stop_target IS NULL
                               AND status NOT IN ('accepted', 'ready_for_review', 'blocked', 'failed', 'cancelled'))
                   RETURNING interaction_id""",
                (run_id, interaction_id, run_id),
            )
            row = await cursor.fetchone()
            await cursor.close()
            await db.commit()
            return row is not None

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
                "SELECT * FROM factory_sessions WHERE session_id = ?",
                (session_id,),
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def save_session_settings(self, session_id: str, settings_json: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT run_id FROM factory_sessions WHERE session_id = ?", (session_id,)
            )
            session = await cursor.fetchone()
            if session is None:
                raise ValueError(f"unknown ACP session: {session_id}")
            if session[0]:
                cursor = await db.execute(
                    "SELECT status FROM factory_runs WHERE run_id = ?", (session[0],)
                )
                run = await cursor.fetchone()
                if run is None or run[0] not in FINAL_STATUSES:
                    raise ValueError(
                        "settings cannot change while a run is active; finish or cancel it first"
                    )
            await db.execute(
                "UPDATE factory_sessions SET settings_json = ?, config_options_json = NULL WHERE session_id = ?",
                (settings_json, session_id),
            )
            await db.commit()

    async def save_session_options(
        self,
        session_id: str,
        settings_json: str | None,
        options_json: str,
    ) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """UPDATE factory_sessions SET config_options_json = ?
                   WHERE session_id = ? AND settings_json IS ?""",
                (options_json, session_id, settings_json),
            )
            await db.commit()

    async def latest_session(self, cwd: Path) -> dict[str, str | None] | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """SELECT * FROM factory_sessions WHERE cwd = ?
                   ORDER BY session_id = (SELECT session_id FROM factory_active_sessions WHERE cwd = factory_sessions.cwd) DESC,
                            rowid DESC LIMIT 1""",
                (str(cwd.resolve()),),
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def activate_session(self, session_id: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """INSERT INTO factory_active_sessions (cwd, session_id)
                   SELECT cwd, session_id FROM factory_sessions WHERE session_id = ?
                   ON CONFLICT(cwd) DO UPDATE SET session_id = excluded.session_id""",
                (session_id,),
            )
            await db.commit()

    async def claim_session_run(
        self, session_id: str, run_id: str, expected_settings: str | None
    ) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT run_id, settings_json FROM factory_sessions WHERE session_id = ?",
                (session_id,),
            )
            row = await cursor.fetchone()
            if row is None or row[1] != expected_settings:
                raise ValueError("session settings changed; reload before starting")
            if row[0]:
                cursor = await db.execute(
                    "SELECT status FROM factory_runs WHERE run_id = ?", (row[0],)
                )
                active = await cursor.fetchone()
                if active is None or active[0] not in FINAL_STATUSES:
                    raise ValueError("this session already has an active run; resume or cancel it")
            await db.execute(
                "UPDATE factory_sessions SET run_id = ? WHERE session_id = ?", (run_id, session_id)
            )
            await db.execute(
                "INSERT INTO factory_session_runs (session_id, run_id) VALUES (?, ?)",
                (session_id, run_id),
            )
            await db.commit()

    async def session_runs(self, session_id: str) -> list[str]:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                "SELECT run_id FROM factory_session_runs WHERE session_id = ? ORDER BY rowid",
                (session_id,),
            )
            return [str(row[0]) for row in await cursor.fetchall()]

    async def append_session_update(
        self, session_id: str, entry_key: str, update: dict[str, Any]
    ) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """INSERT OR IGNORE INTO factory_session_history (session_id, entry_key, payload)
                   VALUES (?, ?, ?)""",
                (session_id, entry_key, json.dumps(update)),
            )
            await db.commit()
            return cursor.rowcount == 1

    async def session_history(self, session_id: str) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                "SELECT payload FROM factory_session_history WHERE session_id = ? ORDER BY sequence",
                (session_id,),
            )
            return [json.loads(row[0]) for row in await cursor.fetchall()]

    async def release_unstarted_session_run(self, session_id: str, run_id: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """UPDATE factory_sessions SET run_id = (
                       SELECT history.run_id FROM factory_session_runs AS history
                       JOIN factory_runs AS runs ON runs.run_id = history.run_id
                       WHERE history.session_id = factory_sessions.session_id
                       ORDER BY history.rowid DESC LIMIT 1
                   ) WHERE session_id = ? AND run_id = ?
                   AND NOT EXISTS (SELECT 1 FROM factory_runs WHERE run_id = ?)
                   AND NOT EXISTS (SELECT 1 FROM factory_operations WHERE run_id = ?)
                   AND NOT EXISTS (SELECT 1 FROM factory_processes WHERE run_id = ?)
                   AND NOT EXISTS (SELECT 1 FROM factory_children WHERE run_id = ?)""",
                (session_id, run_id, run_id, run_id, run_id, run_id),
            )
            await db.commit()
