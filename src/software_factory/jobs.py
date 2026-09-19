"""Detached controller jobs with a durable launch record and an OS dispatch gate."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import sys
from pathlib import Path
from uuid import uuid4

import aiosqlite

from software_factory.context import FactoryContext, checkpoint_path
from software_factory.detached import launch_detached


async def launch_job(workspace: Path, context: FactoryContext, run_id: str) -> None:
    job_id = "job_" + uuid4().hex
    async with aiosqlite.connect(checkpoint_path(context)) as db:
        await db.execute("INSERT INTO factory_jobs(job_id, run_id) VALUES (?, ?)", (job_id, run_id))
        await db.commit()

    async def register(pid: int, identity: str) -> None:
        async with aiosqlite.connect(checkpoint_path(context)) as db:
            await db.execute(
                "UPDATE factory_jobs SET process_group = ?, identity = ? WHERE job_id = ?",
                (pid, identity, job_id),
            )
            await db.commit()

    await launch_detached(
        [
            sys.executable,
            "-m",
            "software_factory.jobs",
            "--workspace",
            str(workspace),
            "--storage",
            str(context.storage_root),
            "--run",
            run_id,
            "--job",
            job_id,
        ],
        context.storage_root,
        register,
    )


async def run_job(workspace: Path, storage: Path, run_id: str, job_id: str) -> None:
    from software_factory.control import FactoryControl

    control = FactoryControl(workspace, storage_root=storage)
    engine = await control.engine(run_id)
    # A waiting command stays alive until an earlier command releases this lock.
    # It then reconciles the same saved run. No command is lost during job exit.
    import hashlib

    lock = storage / "factory" / ("job-" + hashlib.sha256(run_id.encode()).hexdigest() + ".lock")
    with lock.open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        async with aiosqlite.connect(engine.database_path) as db:
            await db.execute(
                "UPDATE factory_jobs SET state = 'running' WHERE job_id = ?", (job_id,)
            )
            await db.commit()
        try:
            await engine.resume(run_id)
        except Exception as exc:
            async with aiosqlite.connect(engine.database_path) as db:
                await db.execute(
                    "UPDATE factory_jobs SET state = 'recovery_needed', error = ? WHERE job_id = ?",
                    (str(exc), job_id),
                )
                await db.commit()
            raise
        else:
            async with aiosqlite.connect(engine.database_path) as db:
                await db.execute(
                    "UPDATE factory_jobs SET state = 'completed' WHERE job_id = ?", (job_id,)
                )
                await db.commit()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--storage", type=Path, required=True)
    parser.add_argument("--run", required=True)
    parser.add_argument("--job", required=True)
    args = parser.parse_args()
    asyncio.run(run_job(args.workspace, args.storage, args.run, args.job))


if __name__ == "__main__":
    main()
