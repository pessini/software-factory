"""BB owns the child; a local isolated worker owns its provider execution."""

from __future__ import annotations

import asyncio
import re
import shutil
from pathlib import Path
from urllib.parse import quote

from software_factory.bb.client import (
    BBClient,
    BBError,
    BBExecution,
    BBThreadState,
    RecoveryNeeded,
    _child_title,
)
from software_factory.bb.local_api import read_bb, thread_storage
from software_factory.config import AgentConfig, ModelOption
from software_factory.ownership import OwnershipError, session_lease
from software_factory.worker_container import (
    WORKER_IMAGE,
    container_command,
    inspect_container,
    stop_container,
)
from software_factory.worker_records import WorkerMessage, WorkerReceipt, WorkerRecords

WORKER_PROVIDER = "acp-software-factory-worker"


class ProtectedBBClient(BBClient):
    async def model_catalog(self) -> list[ModelOption]:
        catalog = [option for option in await super().model_catalog() if option.provider == "codex"]
        if not catalog:
            raise BBError(
                "No supported Codex models are available in this BB environment", "configuration"
            )
        return catalog

    async def check_protection(self, controller_root: Path) -> None:
        if not shutil.which("container"):
            raise BBError(
                "Install and start Apple Container to use the protected worker", "configuration"
            )
        if controller_root.resolve().is_relative_to(self.workspace.resolve()):
            raise BBError(
                "Controller evidence must be outside the writable workspace", "configuration"
            )
        await container_command("image", "inspect", WORKER_IMAGE)
        providers = await self._run(
            "provider", "list", "--environment", self.context.environment_id
        )
        if not isinstance(providers, list) or not any(
            provider.get("id") == WORKER_PROVIDER and provider.get("available")
            for provider in providers
        ):
            raise BBError(
                "Register software-factory worker-acp as software-factory-worker in the local BB environment",
                "configuration",
            )

    def _message(self, prompt: str) -> str:
        match = re.search(r"\n\nFactory operation: ([^\n]+)\n$", prompt)
        if match is None:
            raise BBError("Worker dispatch requires a saved operation identity", "protocol")
        return WorkerMessage(
            parent_thread_id=self.context.parent_thread_id, operation_id=match[1]
        ).model_dump_json()

    async def spawn_child(self, role: str, prompt: str, agent: AgentConfig, spawn_key: str) -> str:
        if agent.provider != "codex":
            raise BBError("This protected worker supports only Codex", "configuration")
        result = await self._run(
            "thread",
            "spawn",
            "--project",
            self.context.project_id,
            "--parent-thread",
            self.context.parent_thread_id,
            "--environment",
            self.context.environment_id,
            "--visibility",
            "visible",
            "--title",
            _child_title(role, spawn_key),
            "--prompt",
            self._message(prompt),
            "--provider",
            WORKER_PROVIDER,
            "--permission-mode",
            "accept-edits",
        )
        thread = result.get("thread", result) if isinstance(result, dict) else None
        if not isinstance(thread, dict) or not isinstance(thread.get("id"), str):
            raise BBError("BB worker creation returned no child identity", "protocol")
        return str(thread["id"])

    async def tell(self, thread_id: str, message: str) -> str | None:
        await self._records(thread_id)
        await self._run("thread", "tell", thread_id, self._message(message), "--mode", "steer")
        return None

    async def _records(self, thread_id: str) -> WorkerRecords:
        child = await read_bb(f"threads/{quote(thread_id, safe='')}")
        if not isinstance(child, dict) or (
            child.get("id"),
            child.get("parentThreadId"),
            child.get("projectId"),
            child.get("environmentId"),
            child.get("providerId"),
        ) != (
            thread_id,
            self.context.parent_thread_id,
            self.context.project_id,
            self.context.environment_id,
            WORKER_PROVIDER,
        ):
            raise BBError("BB child identity does not match this protected run", "protocol")
        return WorkerRecords(await thread_storage(thread_id))

    @staticmethod
    def _execution(receipt: WorkerReceipt) -> BBExecution:
        if receipt.execution_id is None:
            raise RecoveryNeeded(
                receipt.error or "Provider has not returned the saved request's execution identity"
            )
        status = receipt.status
        if status == "prepared" or (status == "completed" and not receipt.stopped):
            status = "running"
        return BBExecution(
            receipt.execution_id, status, receipt.output, receipt.reported, receipt.interaction_id
        )

    async def reconcile_operation(self, thread_id: str, operation_id: str) -> BBExecution | None:
        records = await self._records(thread_id)
        # A missing receipt can mean the BB worker is still starting. The caller
        # bounds this read by the saved operation deadline and can cancel it.
        while True:
            receipt = records.read(operation_id)
            if receipt is not None:
                if receipt.execution_id is not None:
                    return self._execution(receipt)
                if receipt.status in {"failed", "stopped"}:
                    raise RecoveryNeeded(
                        receipt.error or "Provider dispatch could not be established"
                    )
            child = await read_bb(f"threads/{quote(thread_id, safe='')}")
            if child.get("status") not in {"active", "running", "starting"} and not child.get(
                "queuedMessageCount"
            ):
                return None
            await asyncio.sleep(0.1)

    async def execution(self, thread_id: str, execution_id: str) -> BBExecution:
        matches = [
            receipt
            for receipt in (await self._records(thread_id)).all()
            if receipt.execution_id == execution_id
        ]
        if len(matches) != 1:
            raise RecoveryNeeded("The exact provider execution cannot be established")
        return self._execution(matches[0])

    async def wait(self, thread_id: str) -> None:
        while True:
            if (await self.inspect_thread(thread_id)).stopped:
                return
            await asyncio.sleep(0.1)

    async def inspect_thread(self, thread_id: str) -> BBThreadState:
        records = await self._records(thread_id)
        child = await read_bb(f"threads/{quote(thread_id, safe='')}")
        queued = await read_bb(f"threads/{quote(thread_id, safe='')}/queued-messages")
        if not isinstance(queued, list):
            raise BBError("BB did not return a pending-message list", "protocol")
        receipts = records.all()
        stopped = (
            (
                child.get("status") in {"idle", "error"}
                or (child.get("status") == "active" and bool(receipts))
            )
            and child.get("activeBackgroundAgentCount") == 0
            and child.get("queuedMessageCount") == 0
            and not queued
        )
        for receipt in receipts:
            if receipt.container_creation_requested:
                container = await inspect_container(receipt.container_id, receipt.operation_id)
                stopped = (
                    stopped and container is not None and container["status"]["state"] == "stopped"
                )
            stopped = stopped and receipt.stopped
        return BBThreadState(str(child.get("status", "unknown")), stopped)

    async def stop(self, thread_id: str) -> None:
        records = await self._records(thread_id)
        records.request_stop()
        await super().stop(thread_id)
        for receipt in records.all():
            if receipt.container_creation_requested and not await stop_container(
                receipt.container_id, receipt.operation_id
            ):
                raise RecoveryNeeded("The saved provider container could not be confirmed stopped")
        for _ in range(50):
            try:
                with session_lease(records.root / "worker.lock", thread_id):
                    for receipt in records.all():
                        if receipt.container_creation_requested and not await stop_container(
                            receipt.container_id, receipt.operation_id
                        ):
                            raise RecoveryNeeded("Container creation remains uncertain")
                        receipt.stopped = True
                        if receipt.status != "completed":
                            receipt.status, receipt.error = (
                                "stopped",
                                "Worker stopped by controller",
                            )
                        records.save(receipt)
            except OwnershipError:
                await asyncio.sleep(0.1)
                continue
            if (await self.inspect_thread(thread_id)).stopped:
                return
            await asyncio.sleep(0.1)
        raise RecoveryNeeded("BB worker exit is not confirmed; ownership is retained")
