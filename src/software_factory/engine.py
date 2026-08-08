from __future__ import annotations

import asyncio
import os
import signal
import time
from contextlib import suppress
from pathlib import Path
from typing import cast
from uuid import uuid4

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from software_factory.artifacts import ArtifactStore
from software_factory.bb import BBClient, BBClientProtocol, BBContext
from software_factory.config import load_config
from software_factory.events import EventSink, null_event_sink
from software_factory.graph import build_graph
from software_factory.persistence import RunStore
from software_factory.runtime import FactoryRuntime
from software_factory.state import FactoryState


def new_run_id() -> str:
    return f"fct_{uuid4().hex[:12]}"


def checkpoint_path(context: BBContext) -> Path:
    return context.thread_storage / "factory" / "checkpoints.sqlite"


class FactoryEngine:
    def __init__(
        self,
        *,
        workspace: Path,
        context: BBContext,
        bb: BBClientProtocol | None = None,
        database_path: Path | None = None,
        event_sink: EventSink = null_event_sink,
        cancel_event: asyncio.Event | None = None,
    ) -> None:
        self.workspace = workspace.resolve()
        self.context = context
        self.bb = bb or BBClient.from_context(context, self.workspace)
        self.database_path = database_path or checkpoint_path(context)
        self.store = RunStore(self.database_path)
        self.event_sink = event_sink
        self.cancel_event = cancel_event or asyncio.Event()

    async def run(self, request: str, run_id: str | None = None) -> FactoryState:
        if not request.strip():
            raise ValueError("request must not be empty")
        run_id = run_id or new_run_id()
        config = load_config(self.workspace)
        deadline_at = time.time() + config.limits.total_run_timeout_seconds
        await self.store.initialize()
        await self.store.create_run(
            run_id,
            self.workspace,
            self.context.parent_thread_id,
            deadline_at=deadline_at,
        )
        record = await self.store.get_run(run_id)
        if record is not None and isinstance(record.get("deadline_at"), (int, float)):
            deadline_at = float(record["deadline_at"])
        runtime = self._runtime(run_id)
        initial: FactoryState = {
            "run_id": run_id,
            "request": request.strip(),
            "status": "pending",
            "bb_parent_thread_id": self.context.parent_thread_id,
            "bb_project_id": self.context.project_id,
            "bb_environment_id": self.context.environment_id,
            "workspace_path": str(self.workspace),
            "storage_path": str(self.context.thread_storage),
            "test_repair_count": 0,
            "max_test_repairs": config.limits.max_test_repairs,
            "test_attempt_count": 0,
            "test_passed": False,
            "review_revision_count": 0,
            "max_review_revisions": config.limits.max_review_revisions,
            "review_attempt_count": 0,
            "review_approved": False,
            "changed_files": [],
            "active_child_thread_id": None,
            "error": None,
        }
        return await self._invoke_before_deadline(runtime, initial, deadline_at)

    async def resume(self, run_id: str) -> FactoryState:
        await self.store.initialize()
        record = await self.store.get_run(run_id)
        if record is None:
            raise ValueError(f"unknown run ID: {run_id}")
        if Path(str(record["workspace"])).resolve() != self.workspace:
            raise ValueError("run belongs to a different workspace")
        runtime = self._runtime(run_id)
        raw_deadline = record.get("deadline_at")
        if isinstance(raw_deadline, (int, float)):
            deadline_at = float(raw_deadline)
        else:
            deadline_at = time.time() + runtime.config.limits.total_run_timeout_seconds
            await self.store.set_deadline(run_id, deadline_at)
        return await self._invoke_before_deadline(runtime, None, deadline_at)

    async def _invoke_before_deadline(
        self, runtime: FactoryRuntime, value: FactoryState | None, deadline_at: float
    ) -> FactoryState:
        remaining = deadline_at - time.time()
        if remaining <= 0:
            await self.cancel_external(
                runtime.run_id, status="failed", error="total run timeout exceeded"
            )
            raise TimeoutError("total run timeout exceeded")
        try:
            async with asyncio.timeout(remaining):
                return await self._invoke(runtime, value)
        except TimeoutError:
            self.cancel_event.set()
            await self.cancel_external(
                runtime.run_id, status="failed", error="total run timeout exceeded"
            )
            raise TimeoutError("total run timeout exceeded") from None

    async def _invoke(self, runtime: FactoryRuntime, value: FactoryState | None) -> FactoryState:
        thread_id = f"{self.context.parent_thread_id}:{runtime.run_id}"
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        async with AsyncSqliteSaver.from_conn_string(str(self.database_path)) as saver:
            graph = build_graph(runtime, saver)
            result = await graph.ainvoke(value, {"configurable": {"thread_id": thread_id}})
        return cast(FactoryState, result)

    def _runtime(self, run_id: str) -> FactoryRuntime:
        return FactoryRuntime(
            run_id=run_id,
            workspace=self.workspace,
            context=self.context,
            config=load_config(self.workspace),
            bb=self.bb,
            store=self.store,
            artifacts=ArtifactStore(self.workspace, run_id),
            event_sink=self.event_sink,
            cancel_event=self.cancel_event,
        )

    async def cancel_external(
        self, run_id: str, *, status: str = "cancelled", error: str = "cancelled by user"
    ) -> None:
        self.cancel_event.set()
        await self.store.initialize()
        record = await self.store.get_run(run_id)
        if record is None:
            raise ValueError(f"unknown run ID: {run_id}")
        # Persist first so the active workflow observes cancellation before a stopped
        # child or process can return control and advance the graph.
        await self.store.set_status(run_id, status, error)
        record = await self.store.get_run(run_id)
        if record is None:
            raise ValueError(f"unknown run ID: {run_id}")
        child = record.get("active_child_thread_id")
        if isinstance(child, str) and child:
            await self.bb.stop(child)
            await self.store.clear_active_child(run_id)
        process_group = record.get("active_process_group")
        if isinstance(process_group, int):
            with suppress(ProcessLookupError):
                os.killpg(process_group, signal.SIGTERM)
            await self.store.set_process_group(run_id, None)
