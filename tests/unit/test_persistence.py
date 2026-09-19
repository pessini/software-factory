import asyncio
import json
from pathlib import Path

import aiosqlite
import pytest

from software_factory.bb import BBContext
from software_factory.engine import FactoryEngine
from software_factory.events import FactoryEvent
from software_factory.persistence import RunStore
from tests.conftest import FakeBB


@pytest.mark.asyncio
async def test_initialize_migrates_existing_run_table(tmp_path: Path) -> None:
    database = tmp_path / "factory.sqlite"
    async with aiosqlite.connect(database) as db:
        await db.execute(
            """CREATE TABLE factory_runs (
                run_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                workspace TEXT NOT NULL,
                parent_thread_id TEXT NOT NULL,
                active_child_thread_id TEXT,
                active_process_group INTEGER,
                error TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        await db.execute(
            "INSERT INTO factory_runs (run_id, status, workspace, parent_thread_id) VALUES ('old-run', 'running', ?, 'parent')",
            (str(tmp_path),),
        )
        await db.commit()

    store = RunStore(database)
    await store.initialize()

    async with aiosqlite.connect(database) as db:
        cursor = await db.execute("PRAGMA table_info(factory_runs)")
        columns = {str(row[1]) for row in await cursor.fetchall()}
    assert "deadline_at" in columns
    record = await store.get_run("old-run")
    assert record is not None and record["control_version"] == 1
    bb = FakeBB(tmp_path, {})
    engine = FactoryEngine(
        workspace=tmp_path,
        context=BBContext("parent", "project", "environment", tmp_path / "storage"),
        execution=bb,
        database_path=database,
    )
    with pytest.raises(ValueError, match="legacy run lacks Phase 1 evidence"):
        await engine.resume("old-run")
    assert not bb.spawn_counts


@pytest.mark.parametrize("first", ["cancellation", "acceptance", "simultaneous"])
async def test_independent_writers_cannot_replace_an_accepted_stop_or_final_result(
    tmp_path: Path, first: str
) -> None:
    store = RunStore(tmp_path / "factory.sqlite")
    await store.initialize()
    await store.create_run("race", tmp_path, "parent")
    await store.set_status("race", "running")
    other = RunStore(store.path)
    result = {"run_id": "race", "changed_files": ["answer.txt"], "test_attempt_count": 1}
    if first == "cancellation":
        await other.request_stop("race")
        await store.set_status("race", "ready_for_review", result=result)
    elif first == "acceptance":
        await store.set_status("race", "ready_for_review", result=result)
        await other.request_stop("race")
    else:
        await asyncio.gather(
            other.request_stop("race"), store.set_status("race", "ready_for_review", result=result)
        )
    record = await store.get_run("race")
    assert record is not None
    if record["status"] == "stopping":
        assert first != "acceptance"
        assert record["stop_target"] == "cancelled"
        await store.set_status("race", "ready_for_review")
        final = await other.set_status("race", "cancelled")
        assert final["status"] == "cancelled"
    else:
        assert first != "cancellation"
        assert record["status"] == "ready_for_review" and record["error"] is None
        assert json.loads(record["result_json"])["changed_files"] == ["answer.txt"]
    events = await store.events("race")
    assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))


async def test_concurrent_events_keep_unique_ordered_sequences(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "events.sqlite")
    await store.initialize()
    await store.create_run("events", tmp_path, "parent")
    await asyncio.gather(
        *(
            RunStore(store.path).append_event(
                FactoryEvent(run_id="events", node="test", status="info", message=f"event {index}")
            )
            for index in range(12)
        )
    )
    events = await store.events("events")
    assert [event["sequence"] for event in events] == list(range(1, 13))
    assert {event["message"] for event in events} == {f"event {index}" for index in range(12)}


async def test_concurrent_read_failures_cannot_reset_the_saved_retry_budget(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "retries.sqlite")
    await store.initialize()
    await store.create_run("retries", tmp_path, "parent")
    operation = await store.prepare_operation("retries", "plan", 1, "create", "request")
    claimed = await asyncio.gather(
        *(RunStore(store.path).claim_retry(operation["operation_id"], 2) for _ in range(8))
    )
    assert sorted(count for count in claimed if count is not None) == [1, 2]
    assert (await store.operations("retries"))[0]["retry_count"] == 2
    assert await RunStore(store.path).claim_retry(operation["operation_id"], 2) is None
