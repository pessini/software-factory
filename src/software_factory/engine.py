from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from software_factory.artifacts import ArtifactIntegrityError, ArtifactStore
from software_factory.cleanup import stop_owned_work
from software_factory.config import (
    DeliveryPolicy,
    FactoryConfig,
    Selection,
    load_config,
    upgrade_config,
)
from software_factory.context import RunContext
from software_factory.delivery import Delivery
from software_factory.delivery_commands import DeliveryCommands
from software_factory.events import EventSink, null_event_sink
from software_factory.evidence import EVIDENCE_VERSION
from software_factory.execution import (
    ExecutionError,
    ExecutionManager,
    RecoveryNeeded,
    WaitingForInput,
)
from software_factory.github_delivery import GitHubDelivery, GitHubProtocol
from software_factory.graph import build_graph
from software_factory.ownership import OwnershipBusy, OwnershipError, RunOwnership
from software_factory.persistence import RunStore
from software_factory.planning import PLANNING_WORKFLOW, build_planning_graph
from software_factory.processes import CleanupUncertain
from software_factory.review_quality import ReviewFeedback, with_feedback
from software_factory.runtime import FactoryCancelled, FactoryRuntime
from software_factory.settings import resolve_config
from software_factory.state import FINAL_STATUSES, FactoryState


def new_run_id() -> str:
    return f"fct_{uuid4().hex[:12]}"


def checkpoint_path(context: RunContext) -> Path:
    return context.storage_root / "factory" / "checkpoints.sqlite"


class FactoryEngine:
    def __init__(
        self,
        *,
        workspace: Path,
        context: RunContext | None = None,
        execution: ExecutionManager | None = None,
        database_path: Path | None = None,
        event_sink: EventSink = null_event_sink,
        cancel_event: asyncio.Event | None = None,
        github: GitHubProtocol | None = None,
    ) -> None:
        self.workspace = workspace.resolve()
        from software_factory.context import FactoryContext

        self.context = context or FactoryContext.open(self.workspace)
        self.database_path = database_path or checkpoint_path(self.context)
        if execution is None:
            from software_factory.worker_manager import ProtectedWorkerManager

            execution = ProtectedWorkerManager(self.workspace, self.context)
        self.execution = execution
        self.store = RunStore(self.database_path)
        self.event_sink = event_sink
        self.cancel_event = cancel_event or asyncio.Event()
        self.active_run_id: str | None = None
        self.github = github

    async def run(
        self,
        request: str,
        run_id: str | None = None,
        *,
        selection: Selection | None = None,
        configuration: FactoryConfig | None = None,
        required_criteria: list[dict[str, str]] | None = None,
        prepare_only: bool = False,
        workflow: str = "implementation",
        planning: dict[str, Any] | None = None,
    ) -> FactoryState:
        if not request.strip():
            raise ValueError("request must not be empty")
        run_id = run_id or new_run_id()
        config = (
            configuration.model_copy(deep=True) if configuration else load_config(self.workspace)
        )
        source_config = config.model_copy(deep=True)
        if not self.context.legacy:
            config = upgrade_config(config)
        await self.store.initialize()
        initial: FactoryState = {
            "evidence_version": EVIDENCE_VERSION,
            "run_id": run_id,
            "request": request,
            "status": "pending",
            "context_version": 1 if self.context.legacy else 2,
            "session_id": self.context.session_id,
            "workspace_id": "workspace_" + hashlib.sha256(os.fsencode(self.workspace)).hexdigest(),
            "owner_uid": os.getuid(),
            "checkpoint_thread_id": f"{self.context.session_id}:{run_id}"
            if self.context.legacy
            else f"factory:{run_id}",
            "workspace_path": str(self.workspace),
            "storage_path": str(self.context.storage_root),
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
            "required_criteria": required_criteria or [],
            "workflow": workflow,
            "planning": planning or {},
            "delivery": {
                "status": "pending" if config.delivery.mode == "pull_request" else "manual"
            },
        }
        ownership = self._ownership(run_id, initial)
        await ownership.acquire(publish=False)
        try:
            if await self.store.get_run(run_id) is not None:
                raise ValueError(f"run already exists; use resume: {run_id}")
            created_at = time.time()
            deadline_at = created_at + config.limits.total_run_timeout_seconds
            await self.store.create_run(
                run_id,
                self.workspace,
                self.context.session_id,
                deadline_at=deadline_at,
                overall_expires_at=created_at + config.limits.overall_run_timeout_seconds,
            )
            ownership.publish_claim()
            await self.store.save_result(run_id, dict(initial))
            runtime = self._runtime(run_id, config=config)
            if not self.context.legacy:
                runtime.artifacts.write_json("config-source.json", source_config)
            runtime.artifacts.write_json("config.json", config)
            try:
                async with asyncio.timeout(config.limits.total_run_timeout_seconds):
                    await self.execution.check_protection(runtime.artifacts.root)
                    await self.execution.validate_workspace(self.workspace)
                    config.resolved_settings = resolve_config(
                        config,
                        selection or Selection(),
                        await self.execution.model_catalog(),
                    )
                    runtime.artifacts.write_json("config.json", config)
            except (ExecutionError, ValueError, TimeoutError) as exc:
                result = await runtime.finish({**initial, "error": str(exc)}, "failed")
                if result["status"] in FINAL_STATUSES:
                    ownership.release_claim()
                return result
            if prepare_only:
                return initial
            return await self._execute_owned(runtime, initial, deadline_at, ownership)
        finally:
            ownership.close()

    async def _record(self, run_id: str) -> dict[str, Any]:
        await self.store.initialize()
        record = await self.store.get_run(run_id)
        if record is None:
            raise ValueError(f"unknown run ID: {run_id}")
        if Path(str(record["workspace"])).resolve() != self.workspace:
            raise ValueError("run belongs to a different workspace")
        if record["parent_thread_id"] != self.context.session_id:
            raise ValueError("run belongs to a different Factory session")
        saved = json.loads(record["result_json"] or "{}")
        if not self.context.legacy and saved.get("context_version") != 2:
            raise ValueError("legacy run requires its original BB recovery context")
        return record

    def _ownership(self, run_id: str, state: Mapping[str, Any]) -> RunOwnership:
        return RunOwnership(
            self.workspace,
            run_id,
            self.database_path,
            workspace_scope=state.get("workflow") != PLANNING_WORKFLOW,
        )

    async def inspect(self, run_id: str) -> FactoryState:
        record = await self._record(run_id)
        saved = json.loads(record["result_json"] or "{}")
        if "review_quality" in saved:
            saved["review_quality"] = with_feedback(
                saved["review_quality"], await self.store.review_feedback(run_id)
            )
        return cast(
            FactoryState,
            {
                **saved,
                "run_id": run_id,
                "status": record["status"],
                "error": record["error"],
                "evidence_status": "stale"
                if saved.get("evidence_status") == "stale"
                else "legacy_unverified"
                if record["status"] == "accepted"
                else "verified"
                if record["status"] == "ready_for_review"
                else "incomplete",
                "interactions": [
                    item.model_dump(mode="json") for item in await self.store.interactions(run_id)
                ],
            },
        )

    def _delivery(self, runtime: FactoryRuntime) -> Delivery:
        github = self.github or GitHubDelivery(
            DeliveryCommands(self.workspace, self.store, runtime.run_id)
        )
        return Delivery(runtime, github)

    @asynccontextmanager
    async def delivery_runtime(self, run_id: str) -> AsyncIterator[FactoryRuntime]:
        await self._record(run_id)
        ownership = RunOwnership(self.workspace, run_id, self.database_path)
        await ownership.acquire()
        try:
            runtime = self._runtime(run_id, create=False)
            await stop_owned_work(self.store, run_id, self.execution)
            yield runtime
        finally:
            if not any(
                op["state"] in {"dispatching", "dispatched"}
                for op in await self.store.operations(run_id)
            ):
                ownership.release_claim()
            ownership.close()

    async def deliver(
        self, run_id: str, *, authorization: DeliveryPolicy | None = None
    ) -> FactoryState:
        state = await self.inspect(run_id)
        fix = state.get("delivery", {}).get("review_fix")
        if fix and fix["status"] != "completed":
            if authorization is not None:
                self._delivery(self._runtime(run_id, create=False)).policy(authorization)
            return await self.fix_reviews(run_id, fix["thread_ids"])
        async with self.delivery_runtime(run_id) as runtime:
            return await self._delivery(runtime).deliver(await self.inspect(run_id), authorization)

    async def review_threads(self, run_id: str) -> list[dict[str, Any]]:
        await self._record(run_id)
        runtime = self._runtime(run_id, create=False)
        state = await self.inspect(run_id)
        policy = self._delivery(runtime).policy()
        if not policy.repository or not state.get("delivery", {}).get("pr_number"):
            raise ValueError("run has no delivered PR")
        return await self._delivery(runtime).github.review_threads(
            policy.repository, state["delivery"]["pr_number"]
        )

    async def fix_reviews(self, run_id: str, thread_ids: list[str]) -> FactoryState:
        from software_factory.delivery_reviews import ReviewFixes

        await self._record(run_id)
        return await ReviewFixes(self, run_id).run(thread_ids)

    async def review_quality(self, run_id: str) -> dict[str, Any]:
        state = await self.inspect(run_id)
        if "review_quality" not in state:
            raise ValueError("this run has no saved risk-based review quality results")
        return state["review_quality"]

    async def record_review_feedback(self, run_id: str, feedback: ReviewFeedback) -> dict[str, Any]:
        record = await self._record(run_id)
        if record["status"] not in FINAL_STATUSES:
            raise ValueError("record review feedback after the run has finished")
        # Load protected evidence, not a user-supplied finding or file path.
        artifacts = ArtifactStore(
            self.workspace,
            run_id,
            storage_root=self.context.storage_root / "factory" / "artifacts",
            create=False,
        )
        from software_factory.contracts.models import FocusedReviewEnvelope

        relative = f"review/{feedback.reviewer}-{feedback.attempt:02d}.json"
        if not artifacts.path(relative).exists():
            raise ValueError("feedback refers to an unknown review round or reviewer")
        review = FocusedReviewEnvelope.model_validate_json(artifacts.read_text(relative))
        if feedback.finding_id not in {item.id for item in review.findings}:
            raise ValueError("feedback refers to an unknown reviewer finding")
        await self.review_quality(run_id)
        await self.store.add_review_feedback(
            run_id, {**feedback.model_dump(mode="json"), "content_version": review.content_version}
        )
        return await self.review_quality(run_id)

    async def answer(
        self, run_id: str, interaction_id: str, answers: dict[str, str]
    ) -> FactoryState:
        return await self.resume(run_id, interaction_id=interaction_id, answers=answers)

    async def retry(self, run_id: str) -> FactoryState:
        record = await self._record(run_id)
        if record["status"] != "recovery_needed" or record["stop_target"]:
            raise ValueError("retry supports recovery_needed runs without a pending stop only")
        return await self.resume(run_id)

    async def resume(
        self,
        run_id: str,
        *,
        interaction_id: str | None = None,
        answers: dict[str, str] | None = None,
    ) -> FactoryState:
        record = await self._record(run_id)
        if record["status"] in FINAL_STATUSES:
            if interaction_id is not None:
                await self.store.answer_interaction(run_id, interaction_id, answers or {})
            state = await self.inspect(run_id)
            if state["status"] == "ready_for_review" and state.get("delivery", {}).get(
                "status", "manual"
            ) not in {"manual", "no_change"}:
                review_fix = state["delivery"].get("review_fix")
                if review_fix and review_fix["status"] != "completed":
                    return await self.fix_reviews(run_id, review_fix["thread_ids"])
                return await self.deliver(run_id)
            return state
        saved = json.loads(record["result_json"] or "{}")
        if saved.get("evidence_version") != EVIDENCE_VERSION:
            raise ValueError("legacy run lacks Phase 1 evidence; cancel it and start a new run")
        if record["control_version"] != 2:
            raise ValueError(
                "legacy run cannot resume safely: execution identities and ownership were not recorded"
            )
        ownership = self._ownership(run_id, saved)
        while True:
            try:
                await ownership.acquire()
                break
            except OwnershipError as exc:
                if interaction_id is None:
                    raise
                interaction = await self.store.interaction(run_id, interaction_id)
                if interaction is None:
                    raise
                interaction.validate_answers(answers or {})
                if interaction.answers is not None:
                    if interaction.answers != answers:
                        raise ValueError(
                            "this interaction already has a different saved answer"
                        ) from None
                    return await self.inspect(run_id)
                if isinstance(exc, OwnershipBusy) and interaction.state == "waiting":
                    # A competing answer may hold the lock before saving its value.
                    await asyncio.sleep(0.01)
                    continue
                raise
        try:
            try:
                runtime = self._runtime(run_id, create=False)
            except (ArtifactIntegrityError, ValueError) as exc:
                result = await self._cleanup_without_artifacts(run_id, "blocked", str(exc))
                if result["status"] in FINAL_STATUSES:
                    ownership.release_claim()
                return result
            if interaction_id is not None:
                try:
                    await self.store.answer_interaction(run_id, interaction_id, answers or {})
                except TimeoutError as exc:
                    result = await runtime.finish(
                        {**await self.inspect(run_id), "error": str(exc)}, "failed"
                    )
                    if result["status"] in FINAL_STATUSES:
                        ownership.release_claim()
                    return result
            record = await self._record(run_id)
            return await self._execute_owned(runtime, None, float(record["deadline_at"]), ownership)
        finally:
            ownership.close()

    async def _execute_owned(
        self,
        runtime: FactoryRuntime,
        value: FactoryState | None,
        deadline_at: float,
        ownership: RunOwnership,
    ) -> FactoryState:
        self.active_run_id = runtime.run_id
        try:
            result = await self._execute_run(runtime, value, deadline_at)
            if result["status"] == "ready_for_review":
                result = await self._delivery(runtime).deliver(result)
            # After this point a concurrent cancel waits to acquire the lock.
            # A stop accepted by a callback during execution is handled here.
            self.active_run_id = None
            record = await self.store.get_run(runtime.run_id)
            if record and record["stop_target"] and result["status"] not in FINAL_STATUSES:
                self.active_run_id = runtime.run_id
                result = await runtime.finish(result, str(record["stop_target"]))
                self.active_run_id = None
            return result
        finally:
            self.active_run_id = None
            record = await self.store.get_run(runtime.run_id)
            if (
                record
                and record["status"] in FINAL_STATUSES
                and not any(
                    op["state"] in {"dispatching", "dispatched"}
                    for op in await self.store.operations(runtime.run_id)
                )
            ):
                ownership.release_claim()

    async def _execute_run(
        self, runtime: FactoryRuntime, value: FactoryState | None, deadline_at: float
    ) -> FactoryState:
        record = await self.store.get_run(runtime.run_id)
        assert record is not None
        saved = cast(FactoryState, json.loads(record["result_json"] or "{}"))
        saved.setdefault("run_id", runtime.run_id)
        if record["stop_target"]:
            return await runtime.finish(saved, str(record["stop_target"]))
        try:
            now = time.time()
            if record["overall_expires_at"] and now >= float(record["overall_expires_at"]):
                raise TimeoutError("overall run expiry exceeded")
            unanswered = [
                item
                for item in await self.store.interactions(runtime.run_id)
                if item.state == "waiting"
            ]
            if unanswered:
                if float(record["wait_started_at"] or now) >= deadline_at:
                    raise TimeoutError("active work timeout exceeded")
                return await self.inspect(runtime.run_id)
            if now >= deadline_at:
                raise TimeoutError("total run timeout exceeded")
            if value is None:
                await runtime.recover(
                    min(deadline_at, record["overall_expires_at"] or float("inf"))
                )
            await self.store.set_status(runtime.run_id, "running")
            await runtime.check_cancelled()
            return await self._invoke_before_deadline(
                runtime, value, min(deadline_at, record["overall_expires_at"] or float("inf"))
            )
        except (RecoveryNeeded, WaitingForInput, CleanupUncertain) as exc:
            return await runtime.pause(
                saved,
                "waiting_for_input" if isinstance(exc, WaitingForInput) else "recovery_needed",
                str(exc),
            )
        except FactoryCancelled as exc:
            stopped = await self.store.get_run(runtime.run_id)
            target = str(stopped["stop_target"] or "cancelled") if stopped else "cancelled"
            return await runtime.finish({**saved, "error": str(exc)}, target)
        except ExecutionError as exc:
            if exc.kind == "connection":
                return await runtime.pause(saved, "recovery_needed", str(exc))
            return await runtime.finish({**saved, "error": f"{exc.kind}: {exc}"}, "blocked")
        except TimeoutError as exc:
            error = str(exc) or "total run timeout exceeded"
            stopped = await self.store.request_stop(runtime.run_id, "failed", error)
            if stopped["status"] in FINAL_STATUSES:
                return cast(FactoryState, json.loads(stopped["result_json"]))
            return await runtime.finish({**saved, "error": error}, "failed")

    async def _invoke_before_deadline(
        self, runtime: FactoryRuntime, value: FactoryState | None, deadline_at: float
    ) -> FactoryState:
        remaining = deadline_at - time.time()
        if remaining <= 0:
            raise TimeoutError("total run timeout exceeded")
        async with asyncio.timeout(remaining):
            return await self._invoke(runtime, value)

    async def _invoke(self, runtime: FactoryRuntime, value: FactoryState | None) -> FactoryState:
        thread_id = (
            f"{self.context.session_id}:{runtime.run_id}"
            if self.context.legacy
            else f"factory:{runtime.run_id}"
        )
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        record = await self._record(runtime.run_id)
        workflow = json.loads(record["result_json"] or "{}").get("workflow")
        async with AsyncSqliteSaver.from_conn_string(str(self.database_path)) as saver:
            graph = (
                build_planning_graph(runtime, saver)
                if workflow == PLANNING_WORKFLOW
                else build_graph(runtime, saver)
            )
            config = {"configurable": {"thread_id": thread_id}}
            if value is None and not (await graph.aget_state(config)).values:
                value = cast(FactoryState, json.loads(record["result_json"] or "{}"))
            try:
                result = await graph.ainvoke(value, config)
            except (RecoveryNeeded, WaitingForInput) as exc:
                snapshot = await graph.aget_state(config)
                state = cast(FactoryState, snapshot.values or value or {"run_id": runtime.run_id})
                return await runtime.pause(
                    state,
                    "waiting_for_input" if isinstance(exc, WaitingForInput) else "recovery_needed",
                    str(exc),
                )
        return await runtime.report(cast(FactoryState, result))

    def _runtime(
        self, run_id: str, *, create: bool = True, config: FactoryConfig | None = None
    ) -> FactoryRuntime:
        artifacts = ArtifactStore(
            self.workspace,
            run_id,
            storage_root=self.context.storage_root / "factory" / "artifacts",
            create=create,
        )
        config = config or (
            load_config(self.workspace)
            if create
            else FactoryConfig.model_validate_json(artifacts.read_text("config.json"))
        )
        if not create and config.resolved_settings is None:
            raise ValueError("run has no saved model settings; start a new run")
        return FactoryRuntime(
            run_id=run_id,
            workspace=self.workspace,
            context=self.context,
            config=config,
            execution=self.execution,
            store=self.store,
            artifacts=artifacts,
            event_sink=self.event_sink,
            cancel_event=self.cancel_event,
        )

    async def cancel_external(
        self, run_id: str, *, status: str = "cancelled", error: str = "cancelled by user"
    ) -> FactoryState:
        record = await self._record(run_id)
        if record["status"] == "ready_for_review":
            saved = await self.inspect(run_id)
            delivery = saved.get("delivery", {})
            if delivery.get("status", "manual") not in {"manual", "no_change", "cancelled"}:
                await self.store.cancel_delivery(run_id)
                if self.active_run_id == run_id:
                    self.cancel_event.set()
                fix = delivery.get("review_fix")
                if fix and fix["status"] != "completed" and await self.store.get_run(fix["run_id"]):
                    await self.cancel_external(fix["run_id"])
                saved["delivery"] = {**delivery, "status": "stopping", "error": error}
                return cast(FactoryState, await self.store.save_result(run_id, dict(saved)))
            return saved
        record = await self.store.request_stop(run_id, status, error)
        saved = cast(
            FactoryState,
            {
                **json.loads(record["result_json"] or "{}"),
                "run_id": run_id,
                "status": record["status"],
                "error": record["error"],
            },
        )
        if record["status"] in FINAL_STATUSES:
            return saved
        if self.active_run_id == run_id:
            self.cancel_event.set()
            return saved
        ownership = self._ownership(run_id, saved)
        for retry in range(100):
            try:
                await ownership.acquire()
                break
            except OwnershipError:
                latest = await self.store.get_run(run_id)
                assert latest is not None
                if latest["status"] in FINAL_STATUSES:
                    return cast(
                        FactoryState,
                        {
                            **json.loads(latest["result_json"] or "{}"),
                            "run_id": run_id,
                            "status": latest["status"],
                            "error": latest["error"],
                        },
                    )
                if retry == 99:
                    return cast(
                        FactoryState,
                        {**saved, "status": latest["status"], "error": latest["error"]},
                    )
                await asyncio.sleep(0.05)
        try:
            try:
                artifacts = ArtifactStore(
                    self.workspace,
                    run_id,
                    storage_root=self.context.storage_root / "factory" / "artifacts",
                )
                config = (
                    FactoryConfig.model_validate_json(artifacts.read_text("config.json"))
                    if artifacts.path("config.json").exists()
                    else load_config(self.workspace)
                )
            except ArtifactIntegrityError, ValueError:
                result = await self._cleanup_without_artifacts(run_id, status, error)
                if result["status"] in FINAL_STATUSES:
                    ownership.release_claim()
                return result
            runtime = FactoryRuntime(
                run_id=run_id,
                workspace=self.workspace,
                context=self.context,
                execution=self.execution,
                store=self.store,
                artifacts=artifacts,
                config=config,
                event_sink=self.event_sink,
            )
            result = await runtime.finish(saved, status)
            if result["status"] in FINAL_STATUSES:
                ownership.release_claim()
            return result
        finally:
            ownership.close()

    async def _cleanup_without_artifacts(
        self, run_id: str, target: str, error: str
    ) -> FactoryState:
        record = await self.store.request_stop(run_id, target, error)
        saved = {
            **json.loads(record["result_json"] or "{}"),
            "run_id": run_id,
            "final_report_path": "",
        }
        self.execution.bind_run(self.store, run_id)
        try:
            await stop_owned_work(self.store, run_id, self.execution)
        except Exception as exc:
            await self.store.set_status(run_id, "recovery_needed", str(exc), result=saved)
        else:
            await self.store.set_status(
                run_id, str(record["stop_target"] or target), error, result=saved
            )
        return cast(FactoryState, await self.store.save_result(run_id, saved))
