from pathlib import Path

import aiosqlite
import pytest

from software_factory.persistence import RunStore


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
        await db.commit()

    store = RunStore(database)
    await store.initialize()

    async with aiosqlite.connect(database) as db:
        cursor = await db.execute("PRAGMA table_info(factory_runs)")
        columns = {str(row[1]) for row in await cursor.fetchall()}
    assert "deadline_at" in columns
