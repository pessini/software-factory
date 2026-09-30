"""BB child agent for a saved factory operation, with an isolated provider."""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote

import acp
from acp import (
    InitializeResponse,
    LoadSessionResponse,
    NewSessionResponse,
    PromptResponse,
    update_agent_message_text,
)
from acp.schema import AgentCapabilities, Implementation

from software_factory.artifacts import ArtifactStore
from software_factory.bb import BBContext
from software_factory.bb.acp_input import request_text
from software_factory.bb.client import BBError, RecoveryNeeded, _child_title
from software_factory.bb.local_api import read_bb, thread_storage
from software_factory.config import FactoryConfig, Step
from software_factory.contracts.models import BlockingQuestion
from software_factory.git import git
from software_factory.interactions import Interaction
from software_factory.ownership import session_lease
from software_factory.persistence import RunStore
from software_factory.state import FINAL_STATUSES
from software_factory.worker_container import ContainerProvider
from software_factory.worker_records import WorkerMessage, WorkerReceipt, WorkerRecords, request_key


class WorkerAgent:
    def __init__(
        self,
        context: BBContext,
        provider_factory: Callable[..., ContainerProvider] = ContainerProvider,
    ) -> None:
        self.context, self.provider_factory = context, provider_factory
        self.records = WorkerRecords(context.thread_storage)
        self.connection: acp.Client | None = None
        self.workspace: Path | None = None
        self.cancelled = asyncio.Event()
        self.handoff = asyncio.Event()
        self.awaiting_handoff = False

    def on_connect(self, connection: acp.Client) -> None:
        self.connection = connection

    async def initialize(self, protocol_version: int, **kwargs: Any) -> InitializeResponse:
        return InitializeResponse(
            protocol_version=protocol_version,
            agent_capabilities=AgentCapabilities(load_session=True),
            agent_info=Implementation(name="software-factory-worker", version="1"),
        )

    async def new_session(self, cwd: str, **kwargs: Any) -> NewSessionResponse:
        self.workspace = Path(cwd).resolve()
        return NewSessionResponse(session_id=self.context.parent_thread_id)

    async def load_session(self, cwd: str, session_id: str, **kwargs: Any) -> LoadSessionResponse:
        if session_id != self.context.parent_thread_id:
            raise ValueError("worker session belongs to another BB child")
        self.workspace = Path(cwd).resolve()
        return LoadSessionResponse()

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        if session_id == self.context.parent_thread_id:
            if self.awaiting_handoff:
                self.handoff.set()
            else:
                self.cancelled.set()

    async def prompt(self, session_id: str, prompt: list[Any], **kwargs: Any) -> PromptResponse:
        if session_id != self.context.parent_thread_id or self.workspace is None:
            raise ValueError("worker session is not loaded")
        message = WorkerMessage.model_validate_json(request_text(prompt))
        output = await self.execute(message)
        self.awaiting_handoff = True
        self.handoff.clear()
        try:
            if self.connection is not None:
                await self.connection.session_update(session_id, update_agent_message_text(output))
            await self._hold_result(message)
        finally:
            self.awaiting_handoff = False
        return PromptResponse(stop_reason="cancelled" if self.cancelled.is_set() else "end_turn")

    async def _hold_result(self, message: WorkerMessage) -> None:
        """Avoid BB steering its active parent when a child finishes a step."""
        storage = await thread_storage(message.parent_thread_id)
        store = RunStore(storage / "factory/checkpoints.sqlite")
        while not self.handoff.is_set() and not self.records.stop_requested:
            run = await store.get_run(message.operation_id.split(":")[0])
            if (
                run is None
                or run["status"] in FINAL_STATUSES
                or run["stop_target"]
                or time.time() >= float(run["deadline_at"])
            ):
                return
            with suppress(TimeoutError):
                await asyncio.wait_for(self.handoff.wait(), 0.1)

    async def execute(self, message: WorkerMessage) -> str:
        assert self.workspace is not None
        child = await read_bb(f"threads/{quote(self.context.parent_thread_id, safe='')}")
        if (
            child.get("parentThreadId") != message.parent_thread_id
            or child.get("projectId") != self.context.project_id
            or child.get("environmentId") != self.context.environment_id
        ):
            raise ValueError("worker request does not belong to this BB child and environment")
        parent_storage = await thread_storage(message.parent_thread_id)
        store = RunStore(parent_storage / "factory/checkpoints.sqlite")
        run_id = message.operation_id.split(":")[0]
        run = await store.get_run(run_id)
        if (
            run is None
            or run["parent_thread_id"] != message.parent_thread_id
            or Path(run["workspace"]).resolve() != self.workspace
        ):
            raise ValueError("worker request does not match the saved run")
        operations = await store.operations(run_id)
        operation = next(
            (op for op in operations if op["operation_id"] == message.operation_id), None
        )
        if operation is None or operation["state"] not in {
            "dispatching",
            "dispatched",
            "completed",
        }:
            raise ValueError("worker operation was not authorized for dispatch")
        role = str(operation["role"])
        expected_title = _child_title(role.split(":")[0], f"{run_id}:{role}")
        if child.get("title") != expected_title or (
            operation["thread_id"] is not None
            and operation["thread_id"] != self.context.parent_thread_id
        ):
            raise ValueError("worker operation was authorized for a different child")
        artifacts = ArtifactStore(
            self.workspace, run_id, storage_root=parent_storage / "factory/artifacts", create=False
        )
        config = FactoryConfig.model_validate_json(artifacts.read_text("config.json"))
        if config.resolved_settings is None:
            raise ValueError("worker request has no saved model settings")
        step = cast(Step, {"revise": "revision"}.get(operation["step"], operation["step"]))
        agent = config.resolved_settings.effective[step]
        if agent.provider != "codex":
            raise ValueError("the protected worker currently supports only the Codex provider")
        self.records.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with session_lease(self.records.root / "worker.lock", self.context.parent_thread_id):
            saved = self.records.read(message.operation_id)
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
                operation_id=message.operation_id,
                container_id="factory-"
                + request_key(self.context.parent_thread_id + ":" + message.operation_id)[:32],
                request_hash=digest,
                requested=agent,
            )
            self.records.save(receipt)
            protected = await protected_paths(self.workspace, config)
            provider = self.provider_factory(
                receipt.container_id,
                message.operation_id,
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
                        raise BBError(
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
                            "clientUserMessageId": message.operation_id,
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
                raise BBError("provider closed before the saved turn completed", "connection")
            if params.get("threadId") != receipt.provider_thread_id:
                if "id" in event:
                    await provider.reject_request(event["id"])
                continue
            if (
                method == "item/tool/requestUserInput"
                and params.get("turnId") == receipt.execution_id
            ):
                if pending_request is not None or "id" not in event:
                    raise BBError(
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
                if self.connection is not None:
                    await self.connection.session_update(
                        self.context.parent_thread_id,
                        update_agent_message_text(receipt.question),
                    )
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
                    raise BBError(str(turn.get("error") or turn["status"]), "provider")
                texts = [
                    item["text"]
                    for item in turn.get("items", [])
                    if item.get("type") == "agentMessage" and item.get("phase") != "commentary"
                ]
                receipt.output = "\n".join(texts or output)
                if not receipt.output:
                    raise BBError("completed provider turn has no final output", "protocol")
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


def serve_worker(context: BBContext) -> None:
    asyncio.run(acp.run_agent(cast(acp.Agent, WorkerAgent(context))))
