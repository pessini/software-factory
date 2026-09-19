"""Factory-owned protected workers; no application task is needed for execution."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
from pathlib import Path
from uuid import uuid4

from software_factory.config import AgentConfig, ModelOption
from software_factory.context import RunContext, checkpoint_path
from software_factory.detached import launch_detached
from software_factory.execution import ExecutionError, ExecutionResult, RecoveryNeeded, WorkerState
from software_factory.ownership import OwnershipError, session_lease
from software_factory.persistence import RunStore
from software_factory.processes import group_alive, stop_group
from software_factory.worker import worker_identity
from software_factory.worker_container import (
    WORKER_IMAGE,
    ContainerProvider,
    container_command,
    inspect_container,
    stop_container,
)
from software_factory.worker_records import WorkerReceipt, WorkerRecords, request_key


class ProtectedWorkerManager:
    def __init__(self, workspace: Path, context: RunContext) -> None:
        self.workspace, self.context = workspace.resolve(), context
        self.store = RunStore(checkpoint_path(context))

    def bind_run(self, store: RunStore, run_id: str) -> None:
        if store.path.resolve() != self.store.path.resolve():
            raise ValueError("worker storage differs from the run storage")

    async def validate_workspace(self, workspace: Path) -> None:
        if workspace.resolve() != self.workspace:
            raise ExecutionError("worker belongs to a different workspace", "configuration")
        if self.context.storage_root.resolve().is_relative_to(self.workspace):
            raise ExecutionError(
                "controller storage must be outside the workspace", "configuration"
            )

    async def check_protection(self, controller_root: Path) -> None:
        await self.validate_workspace(self.workspace)
        if controller_root.resolve().is_relative_to(self.workspace):
            raise ExecutionError(
                "controller evidence must be outside the workspace", "configuration"
            )
        if not shutil.which("container"):
            raise ExecutionError(
                "Install and start Apple Container for protected execution", "configuration"
            )
        await container_command("image", "inspect", WORKER_IMAGE)

    async def model_catalog(self) -> list[ModelOption]:
        root = self.context.storage_root / "factory" / "catalog" / uuid4().hex
        root.mkdir(parents=True, mode=0o700)
        provider = ContainerProvider(
            "factory-models-" + uuid4().hex[:24],
            "models:" + uuid4().hex,
            self.workspace,
            root,
            root / "provider",
            [],
            read_only=True,
        )
        created = False

        def before_create() -> None:
            nonlocal created
            created = True

        try:
            await provider.start(before_create=before_create)
            catalog: list[ModelOption] = []
            cursor = None
            while True:
                response = await provider.request("model/list", {"cursor": cursor, "limit": 100})
                for model in response["data"]:
                    catalog.append(
                        ModelOption(
                            provider="codex",
                            model=model["model"],
                            name=model["displayName"],
                            efforts=[
                                item["reasoningEffort"]
                                for item in model["supportedReasoningEfforts"]
                            ],
                            default_effort=model["defaultReasoningEffort"],
                            is_default=model["isDefault"],
                            permission_modes=["auto", "accept-edits"],
                        )
                    )
                cursor = response.get("nextCursor")
                if not cursor:
                    break
            if not catalog:
                raise ExecutionError("Native Codex returned no model choices", "configuration")
            return catalog
        finally:
            if created:
                await asyncio.shield(provider.close())

    def _records(self, worker_id: str) -> WorkerRecords:
        if re.fullmatch(r"worker_[0-9a-f]{32}", worker_id) is None:
            raise ValueError("invalid Factory worker ID")
        return WorkerRecords(self.context.storage_root / "factory" / "workers" / worker_id)

    async def _authorize(self, prompt: str) -> tuple[str, str, str]:
        marker = re.search(r"\n\nFactory operation: ([^\n]+)\n$", prompt)
        if marker is None:
            raise ValueError("dispatch requires a saved Factory operation")
        operation_id = marker[1]
        run_id = operation_id.split(":")[0]
        run = await self.store.get_run(run_id)
        if (
            run is None
            or run["parent_thread_id"] != self.context.session_id
            or Path(run["workspace"]).resolve() != self.workspace
            or run["stop_target"]
        ):
            raise ValueError("worker request does not match an authorized run")
        operation = next(
            (
                op
                for op in await self.store.operations(run_id)
                if op["operation_id"] == operation_id
            ),
            None,
        )
        if (
            operation is None
            or operation["state"] != "dispatching"
            or operation["request"] != prompt[: marker.start()]
        ):
            raise ValueError("worker request does not match the saved operation")
        return run_id, operation_id, worker_identity(run_id, str(operation["role"]))

    async def start_worker(self, role: str, prompt: str, agent: AgentConfig, spawn_key: str) -> str:
        run_id, operation_id, worker_id = await self._authorize(prompt)
        if worker_id != "worker_" + request_key(spawn_key)[:32]:
            raise ValueError("worker identity differs from the saved operation")
        await self._launch(run_id, operation_id, worker_id)
        return worker_id

    async def continue_worker(self, worker_id: str, message: str) -> str | None:
        run_id, operation_id, expected = await self._authorize(message)
        if expected != worker_id:
            raise ValueError("continuation belongs to another worker")
        await self._launch(run_id, operation_id, worker_id)
        return None

    async def _launch(self, run_id: str, operation_id: str, worker_id: str) -> None:
        records = self._records(worker_id)
        records.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = records.root / (request_key(operation_id) + ".launch")
        # This marker precedes process creation. Uncertain creation never gets a second dispatch.
        with path.open("x") as handle:
            json.dump(
                {"run_id": run_id, "operation_id": operation_id, "worker_id": worker_id}, handle
            )
            handle.flush()
            os.fsync(handle.fileno())
        directory = os.open(records.root, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

        async def register(pid: int, identity: str) -> None:
            await self.store.register_process(
                run_id, uuid4().hex, "worker:" + worker_id, pid, identity
            )
            run = await self.store.get_run(run_id)
            if run is None or run["stop_target"] or records.stop_requested:
                raise RecoveryNeeded("run stopped before worker dispatch")

        await launch_detached(
            [
                sys.executable,
                "-m",
                "software_factory.worker_job",
                "--workspace",
                str(self.workspace),
                "--storage",
                str(self.context.storage_root),
                "--session",
                self.context.session_id,
                "--worker",
                worker_id,
                "--operation",
                operation_id,
            ],
            self.context.storage_root,
            register,
        )

    async def find_worker(self, role: str, spawn_key: str) -> str | None:
        worker_id = "worker_" + request_key(spawn_key)[:32]
        return worker_id if any(self._records(worker_id).root.glob("*.launch")) else None

    @staticmethod
    def _execution(receipt: WorkerReceipt) -> ExecutionResult:
        if receipt.execution_id is None:
            raise RecoveryNeeded(receipt.error or "provider dispatch has no confirmed execution ID")
        status = receipt.status
        if status == "prepared" or (status == "completed" and not receipt.stopped):
            status = "running"
        return ExecutionResult(
            receipt.execution_id,
            status,
            receipt.output,
            receipt.reported,
            receipt.interaction_id,
            receipt.usage,
            dict(receipt.connection_evidence) if receipt.connection_evidence else None,
        )

    async def reconcile_operation(
        self, worker_id: str, operation_id: str
    ) -> ExecutionResult | None:
        records = self._records(worker_id)
        while True:
            receipt = records.read(operation_id)
            if receipt is not None and (
                receipt.execution_id or receipt.status in {"failed", "stopped"}
            ):
                return self._execution(receipt)
            processes = [
                p
                for p in await self.store.processes(operation_id.split(":")[0])
                if p["kind"] == "worker:" + worker_id
            ]
            if not any([await group_alive(int(p["process_group"])) for p in processes]):
                raise RecoveryNeeded(
                    "worker dispatch is uncertain; the request will not be repeated"
                )
            await asyncio.sleep(0.1)

    async def execution(self, worker_id: str, execution_id: str) -> ExecutionResult:
        matches = [
            receipt
            for receipt in self._records(worker_id).all()
            if receipt.execution_id == execution_id
        ]
        if len(matches) != 1:
            raise RecoveryNeeded("the exact worker execution cannot be established")
        return self._execution(matches[0])

    async def output(self, worker_id: str) -> str:
        raise RecoveryNeeded("read the exact execution instead of the latest worker output")

    async def wait(self, worker_id: str) -> None:
        while True:
            if (await self.inspect_worker(worker_id)).stopped:
                return
            await asyncio.sleep(0.1)

    async def inspect_worker(self, worker_id: str) -> WorkerState:
        records = self._records(worker_id)
        receipts = records.all()
        stopped = bool(receipts) or records.stop_confirmed
        for receipt in receipts:
            stopped = stopped and receipt.stopped
            if receipt.container_creation_requested:
                container = await inspect_container(receipt.container_id, receipt.operation_id)
                stopped = (
                    stopped and container is not None and container["status"]["state"] == "stopped"
                )
            for process in await self.store.processes(receipt.operation_id.split(":")[0]):
                if process["kind"] == "worker:" + worker_id:
                    if await group_alive(int(process["process_group"])):
                        stopped = False
                    else:
                        await self.store.clear_process(str(process["process_id"]))
        return WorkerState("stopped" if stopped else "running", stopped)

    async def stop(self, worker_id: str) -> None:
        records = self._records(worker_id)
        records.request_stop()
        # Stop provider containers before terminating their supervising processes.
        for receipt in records.all():
            if receipt.container_creation_requested and not await stop_container(
                receipt.container_id, receipt.operation_id
            ):
                raise RecoveryNeeded("provider container exit cannot be confirmed")
        launches = [json.loads(path.read_text()) for path in records.root.glob("*.launch")]
        for run_id in {launch["run_id"] for launch in launches}:
            for process in await self.store.processes(run_id):
                if process["kind"] == "worker:" + worker_id:
                    await stop_group(int(process["process_group"]), str(process["identity"]))
                    await self.store.clear_process(str(process["process_id"]))
        try:
            with session_lease(records.root / "worker.lock", worker_id):
                for receipt in records.all():
                    if receipt.container_creation_requested and not await stop_container(
                        receipt.container_id, receipt.operation_id
                    ):
                        raise RecoveryNeeded("container creation remains uncertain")
                    receipt.stopped = True
                    if receipt.status != "completed":
                        receipt.status, receipt.error = "stopped", "Worker stopped by controller"
                    records.save(receipt)
                records.confirm_stop()
        except OwnershipError as exc:
            raise RecoveryNeeded("worker exit is not confirmed") from exc
        if not (await self.inspect_worker(worker_id)).stopped:
            raise RecoveryNeeded("worker dispatch or exit remains uncertain")
