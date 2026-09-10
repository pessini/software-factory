from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
from typing import IO, Any

from platformdirs import user_state_path

from software_factory.persistence import RunStore
from software_factory.state import FINAL_STATUSES


class OwnershipError(RuntimeError):
    pass


class RunOwnership:
    """OS locks exclude live executors; the saved claim protects orphaned work."""

    def __init__(self, workspace: Path, run_id: str, database: Path) -> None:
        root = Path(os.environ.get("FACTORY_OWNERSHIP_DIR", user_state_path("software-factory")))
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        workspace_key = hashlib.sha256(os.fsencode(workspace.resolve())).hexdigest()
        run_key = hashlib.sha256(run_id.encode()).hexdigest()
        self.paths = [root / f"run-{run_key}.lock", root / f"workspace-{workspace_key}.lock"]
        self.claim_path = root / f"workspace-{workspace_key}.json"
        self.claim = {
            "run_id": run_id,
            "database": str(database.resolve()),
            "workspace": str(workspace.resolve()),
        }
        self.handles: list[IO[str]] = []

    async def acquire(self) -> None:
        try:
            for path in self.paths:
                handle = path.open("a+")
                self.handles.append(handle)
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise OwnershipError("the run or workspace already has an executor") from exc
            if self.claim_path.exists():
                previous = json.loads(self.claim_path.read_text())
                if previous != self.claim:
                    await self._check_previous(previous)
            temporary = self.claim_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self.claim))
            with temporary.open("r+") as handle:
                os.fsync(handle.fileno())
            temporary.replace(self.claim_path)
        except BaseException:
            self.close()
            raise

    async def _check_previous(self, previous: dict[str, Any]) -> None:
        path = Path(previous["database"])
        if not path.is_file():
            raise OwnershipError("previous workspace owner cannot be inspected; recovery needed")
        store = RunStore(path)
        await store.initialize()
        record = await store.get_run(previous["run_id"])
        if (
            record is None
            or record["control_version"] != 2
            or record["status"] not in FINAL_STATUSES
            or record["active_child_thread_id"]
            or record["active_process_group"]
            or await store.processes(previous["run_id"])
            or any(
                op["state"] in {"dispatching", "dispatched"}
                for op in await store.operations(previous["run_id"])
            )
        ):
            raise OwnershipError(f"workspace is reserved for recovery of {previous['run_id']}")

    def release_claim(self) -> None:
        if self.claim_path.exists() and json.loads(self.claim_path.read_text()) == self.claim:
            self.claim_path.unlink()

    def close(self) -> None:
        for handle in reversed(self.handles):
            handle.close()
        self.handles.clear()
