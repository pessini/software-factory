"""Run controller-owned delivery commands with the existing process lifecycle."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from software_factory.persistence import RunStore
from software_factory.processes import finish_process, start_process
from software_factory.runtime import FactoryCancelled


class DeliveryCommandError(RuntimeError):
    pass


class DeliveryCommands:
    def __init__(self, workspace: Path, store: RunStore, run_id: str) -> None:
        self.workspace, self.store, self.run_id = workspace, store, run_id

    async def run(self, argv: list[str], data: bytes | None = None) -> bytes:
        record = await self.store.get_run(self.run_id)
        if record and record.get("delivery_cancel_requested"):
            raise FactoryCancelled("PR delivery cancelled")
        process, identity = await start_process(
            argv,
            self.workspace,
            store=self.store,
            run_id=self.run_id,
            kind="delivery",
            deadline_at=time.time() + 120,
            stdin=asyncio.subprocess.PIPE if data is not None else asyncio.subprocess.DEVNULL,
        )
        communication = asyncio.create_task(process.communicate(data))
        try:
            async with asyncio.timeout(120):
                while not communication.done():
                    await asyncio.wait({communication}, timeout=0.1)
                    record = await self.store.get_run(self.run_id)
                    if record and record.get("delivery_cancel_requested"):
                        raise FactoryCancelled("PR delivery cancelled")
                output, error = await communication
            if process.returncode:
                raise DeliveryCommandError(
                    error.decode(errors="replace").strip() or "delivery command failed"
                )
            return output
        finally:
            try:
                await asyncio.shield(finish_process(process, identity, self.store))
            finally:
                if not communication.done():
                    communication.cancel()
                await asyncio.gather(communication, return_exceptions=True)

    async def git(self, *args: str, data: bytes | None = None, index: Path | None = None) -> bytes:
        argv = ["git", "--literal-pathspecs", "-c", "core.hooksPath=/dev/null", *args]
        if index is not None:
            argv = ["env", f"GIT_INDEX_FILE={index}", *argv]
        return await self.run(argv, data)
