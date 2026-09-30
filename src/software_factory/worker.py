"""Protected execution authorized only by saved Factory run and operation records."""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from software_factory.artifacts import ArtifactStore
from software_factory.config import FactoryConfig, Step
from software_factory.context import RunContext, checkpoint_path
from software_factory.contracts.models import BlockingQuestion
from software_factory.execution import ExecutionError, RecoveryNeeded
from software_factory.git import git
from software_factory.interactions import Interaction
from software_factory.ownership import session_lease
from software_factory.persistence import RunStore
from software_factory.worker_container import ContainerProvider
from software_factory.worker_records import WorkerReceipt, WorkerRecords, request_key


def worker_identity(run_id: str, role: str) -> str:
    return "worker_" + request_key(f"{run_id}:{role}")[:32]


class WorkerExecutor:
    def __init__(
        self,
        workspace: Path,
        context: RunContext,
        worker_id: str,
        provider_factory: Callable[..., ContainerProvider] = ContainerProvider,
    ) -> None:
        self.workspace, self.context, self.worker_id = workspace.resolve(), context, worker_id
        self.provider_factory = provider_factory
        self.records = WorkerRecords(context.storage_root / "factory" / "workers" / worker_id)
        self.cancelled = asyncio.Event()

    async def execute(self, operation_id: str) -> str:
        store = RunStore(checkpoint_path(self.context))
        run_id = operation_id.split(":")[0]
        run = await store.get_run(run_id)
        if (
            run is None
            or run["parent_thread_id"] != self.context.session_id
            or Path(run["workspace"]).resolve() != self.workspace
        ):
            raise ValueError("worker request does not match the saved run")
        operations = await store.operations(run_id)
        operation = next((op for op in operations if op["operation_id"] == operation_id), None)
        if operation is None or operation["state"] not in {
            "dispatching",
            "dispatched",
            "completed",
        }:
            raise ValueError("worker operation was not authorized for dispatch")
        if worker_identity(run_id, str(operation["role"])) != self.worker_id or (
            operation["thread_id"] is not None and operation["thread_id"] != self.worker_id
        ):
            raise ValueError("worker operation belongs to a different worker")
        artifacts = ArtifactStore(
            self.workspace,
            run_id,
            storage_root=self.context.storage_root / "factory/artifacts",
            create=False,
        )
        config = FactoryConfig.model_validate_json(artifacts.read_text("config.json"))
        if config.resolved_settings is None:
            raise ValueError("worker request has no saved model settings")
        step = cast(Step, {"revise": "revision"}.get(operation["step"], operation["step"]))
        agent = config.resolved_settings.effective[step]
        if agent.provider != "codex":
            raise ValueError("the protected worker currently supports only the Codex provider")
        self.records.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with session_lease(self.records.root / "worker.lock", self.worker_id):
            saved = self.records.read(operation_id)
            digest = hashlib.sha256(str(operation["request"]).encode()).hexdigest()
            if saved is not None:
                if saved.request_hash != digest or saved.requested != agent:
                    raise ValueError("saved worker request differs from the controller request")
                if saved.status == "completed" and saved.stopped and saved.output is not None:
                    return saved.output
                raise RecoveryNeeded("worker request already started; it will not be sent again")
            run = await store.get_run(run_id)
            if (
                run is None
                or run["stop_target"]
                or self.records.stop_requested
                or self.cancelled.is_set()
            ):
                raise ValueError("run was stopped before provider dispatch")
            previous = sorted(self.records.all(), key=lambda receipt: receipt.created_at)
            if any(not receipt.stopped for receipt in previous):
                raise RecoveryNeeded("a previous provider container has not been confirmed stopped")
            provider_thread = previous[-1].provider_thread_id if previous else None
            receipt = WorkerReceipt(
                version=2,
                operation_id=operation_id,
                container_id="factory-" + request_key(self.worker_id + ":" + operation_id)[:32],
                request_hash=digest,
                requested=agent,
            )
            self.records.save(receipt)
            protected = await protected_paths(self.workspace, config)
            provider = self.provider_factory(
                receipt.container_id,
                operation_id,
                self.workspace,
                artifacts.root,
                self.records.root / "provider",
                sorted(set(protected)),
                read_only=str(operation["role"]).split(":")[0] != "builder",
            )
            try:
                async with asyncio.timeout(
                    max(0, float(operation["deadline_at"]) - time.time())
                ) as deadline_scope:
                    if self.records.stop_requested or self.cancelled.is_set():
                        raise asyncio.CancelledError()

                    def before_create() -> None:
                        if self.records.stop_requested or self.cancelled.is_set():
                            raise asyncio.CancelledError()
                        receipt.container_creation_requested = True
                        self.records.save(receipt)

                    await provider.start(before_create=before_create)
                    params: dict[str, Any] = {
                        "model": agent.model,
                        "cwd": str(self.workspace),
                        "permissions": "factory",
                        "approvalPolicy": "never",
                        "config": {"model_reasoning_effort": agent.reasoning_level},
                    }
                    if provider_thread:
                        params["threadId"] = provider_thread
                        response = await provider.request("thread/resume", params)
                    else:
                        params.update(selectedCapabilityRoots=[], allowProviderModelFallback=False)
                        response = await provider.request("thread/start", params)
                    receipt.provider_thread_id = response["thread"]["id"]
                    receipt.connection_evidence = {
                        "worker_runtime": "codex",
                        "runtime_version": "0.154.0",
                        "model_connection": "native",
                        "requested_model": agent.model,
                        "actual_model": response["model"],
                        "requested_effort": agent.reasoning_level,
                        "effective_effort": response.get("reasoningEffort"),
                    }
                    receipt.reported = (
                        agent.model_copy(
                            update={
                                "model": response["model"],
                                "reasoning_level": response.get("reasoningEffort")
                                or agent.reasoning_level,
                            }
                        )
                        if response.get("reasoningEffort")
                        else None
                    )
                    if response["model"] != agent.model or (
                        response.get("reasoningEffort")
                        and response["reasoningEffort"] != agent.reasoning_level
                    ):
                        raise ExecutionError(
                            "provider changed the requested model or effort", "configuration"
                        )
                    self.records.save(receipt)
                    latest = await store.get_run(run_id)
                    if (
                        self.cancelled.is_set()
                        or self.records.stop_requested
                        or latest is None
                        or latest["stop_target"]
                    ):
                        raise asyncio.CancelledError()
                    started = await provider.request(
                        "turn/start",
                        {
                            "threadId": receipt.provider_thread_id,
                            "clientUserMessageId": operation_id,
                            "input": [{"type": "text", "text": str(operation["request"])}],
                            "model": agent.model,
                            "effort": agent.reasoning_level,
                            "permissions": "factory",
                            "approvalPolicy": "never",
                        },
                    )
                    receipt.execution_id = started["turn"]["id"]
                    receipt.status = "running"
                    self.records.save(receipt)
                    await self._complete(provider, receipt, store, deadline_scope)
            except asyncio.CancelledError:
                receipt.status, receipt.error = "stopped", "worker cancelled"
                self.cancelled.set()
            except Exception as exc:
                receipt.status, receipt.error = "failed", str(exc)
            finally:
                try:
                    if receipt.container_creation_requested:
                        await asyncio.shield(provider.close())
                    receipt.stopped = True
                finally:
                    self.records.save(receipt)
            return receipt.output or receipt.error or "Provider stopped without output"

    async def _complete(
        self,
        provider: ContainerProvider,
        receipt: WorkerReceipt,
        store: RunStore,
        deadline_scope: asyncio.Timeout,
    ) -> None:
        output: list[str] = []
        pending_request: Any = None
        run_id = receipt.operation_id.split(":")[0]
        while True:
            run = await store.get_run(run_id)
            if (
                self.cancelled.is_set()
                or self.records.stop_requested
                or run is None
                or run["stop_target"]
            ):
                raise asyncio.CancelledError()
            operation = next(
                op
                for op in await store.operations(run_id)
                if op["operation_id"] == receipt.operation_id
            )
            now = time.time()
            active_deadline = min(float(operation["deadline_at"]), float(run["deadline_at"]))
            overall_deadline = run["overall_expires_at"] or float("inf")
            if run["wait_started_at"] is not None:
                if float(run["wait_started_at"]) >= active_deadline:
                    raise TimeoutError("worker work timeout exceeded before input wait")
                deadline = overall_deadline
            else:
                deadline = min(active_deadline, overall_deadline)
            if now >= deadline:
                raise TimeoutError("worker work timeout or overall run expiry exceeded")
            deadline_scope.reschedule(
                None
                if deadline == float("inf")
                else asyncio.get_running_loop().time() + deadline - now
            )
            if pending_request is not None and receipt.interaction_id:
                interaction = await store.interaction(run_id, receipt.interaction_id)
                if interaction is None:
                    raise RecoveryNeeded("saved provider question is unavailable")
                if interaction.answers is not None:
                    if not await store.deliver_interaction(run_id, interaction.interaction_id):
                        raise RecoveryNeeded("answer delivery is stopped or was already attempted")
                    await provider.respond(
                        pending_request,
                        {
                            "answers": {
                                key: {"answers": [answer]}
                                for key, answer in interaction.answers.items()
                            }
                        },
                    )
                    pending_request = None
                    receipt.status = "running"
                    self.records.save(receipt)
            try:
                event = await asyncio.wait_for(provider.notifications.get(), 0.25)
            except TimeoutError:
                continue
            method, params = event["method"], event.get("params", {})
            if method == "connection/closed":
                raise ExecutionError(
                    "provider closed before the saved turn completed", "connection"
                )
            if params.get("threadId") != receipt.provider_thread_id:
                if "id" in event:
                    await provider.reject_request(event["id"])
                continue
            if (
                method == "item/tool/requestUserInput"
                and params.get("turnId") == receipt.execution_id
            ):
                if pending_request is not None or "id" not in event:
                    raise ExecutionError(
                        "provider sent an overlapping or unidentified question", "protocol"
                    )
                questions = [
                    BlockingQuestion(
                        id=question["id"],
                        question=question["question"],
                        options=[option["label"] for option in question.get("options") or []],
                    )
                    for question in params.get("questions", [])
                ]
                interaction = await store.save_interaction(
                    Interaction(
                        interaction_id=f"{receipt.operation_id}:input:{params['itemId']}",
                        run_id=run_id,
                        source="child",
                        operation_id=receipt.operation_id,
                        questions=questions,
                    )
                )
                pending_request = event["id"]
                receipt.status = "waiting_for_input"
                receipt.question = interaction.display()
                receipt.interaction_id = interaction.interaction_id
                self.records.save(receipt)
                continue
            if "id" in event:
                await provider.reject_request(event["id"])
                continue
            if method == "item/completed" and params.get("turnId") == receipt.execution_id:
                item = params["item"]
                if item.get("type") == "agentMessage" and item.get("phase") != "commentary":
                    output.append(item["text"])
            if method == "turn/completed" and params["turn"]["id"] == receipt.execution_id:
                turn = params["turn"]
                if turn["status"] != "completed":
                    raise ExecutionError(str(turn.get("error") or turn["status"]), "provider")
                texts = [
                    item["text"]
                    for item in turn.get("items", [])
                    if item.get("type") == "agentMessage" and item.get("phase") != "commentary"
                ]
                receipt.output = "\n".join(texts or output)
                if not receipt.output:
                    raise ExecutionError("completed provider turn has no final output", "protocol")
                receipt.status = "completed"
                return


async def protected_paths(workspace: Path, config: FactoryConfig) -> list[Path]:
    protected = [
        workspace / ".git",
        workspace / ".factory",
        workspace / ".bb",
    ]
    protected.extend(workspace / path.rstrip("/") for path in config.safety.protected_paths)
    ignored = await git(
        workspace,
        "ls-files",
        "--others",
        "--ignored",
        "--exclude-standard",
        "--directory",
        "-z",
    )
    protected.extend(workspace / path.decode().rstrip("/") for path in ignored.split(b"\0") if path)
    for flag in ("--git-dir", "--git-common-dir"):
        path = (await git(workspace, "rev-parse", "--path-format=absolute", flag)).decode().strip()
        protected.append(Path(path))
    return sorted(set(protected))
