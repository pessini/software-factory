"""Shared local commands for CLI, ACP presentation, and MCP connections."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import aiosqlite

from software_factory.artifacts import ArtifactStore
from software_factory.config import FactoryConfig, Selection, load_config
from software_factory.context import FactoryContext, checkpoint_path
from software_factory.engine import FactoryEngine, new_run_id
from software_factory.execution import ExecutionManager
from software_factory.ownership import command_lease
from software_factory.persistence import RunStore
from software_factory.processes import CleanupUncertain, group_alive, process_identity
from software_factory.settings import resolve_config
from software_factory.state import FINAL_STATUSES
from software_factory.worker_manager import ProtectedWorkerManager


class FactoryControl:
    def __init__(
        self,
        workspace: Path,
        *,
        storage_root: Path | None = None,
        session_id: str | None = None,
        execution: ExecutionManager | None = None,
        launch: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self.workspace = workspace.expanduser().resolve(strict=True)
        self.context = FactoryContext.open(
            self.workspace, storage_root=storage_root, session_id=session_id
        )
        self.store = RunStore(checkpoint_path(self.context))
        self.execution = execution or ProtectedWorkerManager(self.workspace, self.context)
        self.launch = launch or self._launch

    async def _launch(self, run_id: str) -> None:
        from software_factory.jobs import launch_job

        await launch_job(self.workspace, self.context, run_id)

    async def engine(self, run_id: str) -> FactoryEngine:
        await self.store.initialize()
        run = await self.store.get_run(run_id)
        if run is None:
            raise ValueError(f"unknown run ID: {run_id}")
        if Path(run["workspace"]).resolve() != self.workspace:
            raise ValueError("run belongs to a different workspace")
        saved = json.loads(run["result_json"] or "{}")
        if saved.get("context_version") != 2:
            raise ValueError(
                "legacy run: use the original BB recovery path; see docs/interface-migration.md"
            )
        if saved.get("owner_uid", os.getuid()) != os.getuid():
            raise ValueError("run belongs to a different local user")
        context = FactoryContext.open(
            self.workspace,
            storage_root=self.context.storage_root,
            session_id=str(run["parent_thread_id"]),
        )
        execution = (
            ProtectedWorkerManager(self.workspace, context)
            if isinstance(self.execution, ProtectedWorkerManager)
            else self.execution
        )
        return FactoryEngine(workspace=self.workspace, context=context, execution=execution)

    async def start(
        self,
        request: str,
        *,
        request_id: str,
        selection: Selection | None = None,
        configuration: FactoryConfig | None = None,
        required_criteria: list[dict[str, str]] | None = None,
        workflow: str = "implementation",
        planning: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not request.strip() or not request_id.strip() or len(request_id) > 200:
            raise ValueError("a request and a request ID of 1 to 200 characters are required")
        payload: dict[str, Any] = {
            "workspace": str(self.workspace),
            "request": request,
            "selection": selection.model_dump(mode="json") if selection else None,
            "configuration": configuration.model_dump(mode="json") if configuration else None,
        }
        # Added fields join the digest only when used, so earlier request IDs still match.
        extra = {"required_criteria": required_criteria, "workflow": workflow, "planning": planning}
        payload.update(
            {key: value for key, value in extra.items() if value and value != "implementation"}
        )
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        await self.store.initialize()
        async with command_lease(self.store.path, "start:" + request_id):
            async with aiosqlite.connect(self.store.path) as db:
                await db.execute("BEGIN IMMEDIATE")
                cursor = await db.execute(
                    "SELECT input_hash, run_id FROM factory_start_requests WHERE request_id = ?",
                    (request_id,),
                )
                previous = await cursor.fetchone()
                if previous:
                    if previous[0] != digest:
                        raise ValueError("request ID was already used with different input")
                    await db.commit()
                    run_id = str(previous[1])
                    if await self.store.get_run(run_id) is not None:
                        return await self.status(run_id)
                else:
                    run_id = new_run_id()
                    await db.execute(
                        "INSERT INTO factory_start_requests VALUES (?, ?, ?)",
                        (request_id, digest, run_id),
                    )
                    await db.commit()
            session = await self.store.session(self.context.session_id)
            if session is not None and Path(str(session["cwd"])).resolve() != self.workspace:
                raise ValueError("session belongs to a different workspace")
            if session is None:
                await self.store.save_session(self.context.session_id, self.workspace)
            async with command_lease(self.store.path, self.context.session_id):
                session = await self.store.session(self.context.session_id)
                assert session is not None
                if session["run_id"]:
                    await self.store.release_unstarted_session_run(
                        self.context.session_id, str(session["run_id"])
                    )
                config = configuration
                if session["settings_json"]:
                    settings = json.loads(session["settings_json"])
                    config = config or FactoryConfig.model_validate(settings["config"])
                    selection = selection or Selection.model_validate(settings["selection"])
                config = config or load_config(self.workspace)
                await self.store.claim_session_run(
                    self.context.session_id, run_id, session["settings_json"]
                )
                engine = FactoryEngine(
                    workspace=self.workspace, context=self.context, execution=self.execution
                )
                try:
                    result = await engine.run(
                        request,
                        run_id,
                        selection=selection,
                        configuration=config,
                        required_criteria=required_criteria,
                        prepare_only=True,
                        workflow=workflow,
                        planning=planning,
                    )
                finally:
                    # Release only a claim with no saved run, operation, or process.
                    await self.store.release_unstarted_session_run(self.context.session_id, run_id)
            if result["status"] not in FINAL_STATUSES:
                await self.launch(run_id)
            return await self.status(run_id)

    async def status(self, run_id: str, *, after_event: int = 0) -> dict[str, Any]:
        if after_event < 0:
            raise ValueError("event position cannot be negative")
        engine = await self.engine(run_id)
        result: dict[str, Any] = dict(await engine.inspect(run_id))
        record = await self.store.get_run(run_id)
        assert record is not None
        events = await self.store.events(run_id)
        operations = await self.store.operations(run_id)
        active = [op for op in operations if op["state"] not in {"completed", "stopped"}]
        result.update(
            events=[event for event in events if event["sequence"] > after_event],
            event_position=events[-1]["sequence"] if events else 0,
            operations=operations,
            current_step=active[-1]["step"] if active else None,
            workers=await self.store.list_children(run_id),
            deadline_at=record["deadline_at"],
            overall_expires_at=record["overall_expires_at"],
            checkpoint_thread_id="factory:" + run_id,
        )
        result["worker_states"] = [
            {
                "worker_id": op["thread_id"],
                "operation_id": op["operation_id"],
                "execution_id": op["execution_id"],
                "state": op["state"],
            }
            for op in operations
            if op["thread_id"]
        ]
        root = self.context.storage_root / "factory" / "artifacts" / run_id
        result["reports"] = [
            str(path) for path in root.glob("*.md") if path.resolve().is_relative_to(root.resolve())
        ]
        async with aiosqlite.connect(self.store.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM factory_jobs WHERE run_id = ? ORDER BY rowid", (run_id,)
            )
            result["jobs"] = [dict(row) for row in await cursor.fetchall()]
        result["controller_state"] = "idle"
        if result["jobs"]:
            job = result["jobs"][-1]
            if job["state"] in {"queued", "running"}:
                try:
                    alive = bool(
                        job["process_group"]
                        and await process_identity(job["process_group"]) == job["identity"]
                        and await group_alive(job["process_group"])
                    )
                    result["controller_state"] = "running" if alive else "recovery_needed"
                except CleanupUncertain, OSError:
                    result["controller_state"] = "unavailable"
            elif job["state"] == "recovery_needed":
                result["controller_state"] = "recovery_needed"
        return result

    async def answer(
        self, run_id: str, interaction_id: str, answers: dict[str, str]
    ) -> dict[str, Any]:
        await self.engine(run_id)
        changed = await self.store.answer_interaction(run_id, interaction_id, answers)
        if changed:
            await self.launch(run_id)
        return await self.status(run_id)

    async def resume(self, run_id: str) -> dict[str, Any]:
        await self.engine(run_id)
        record = await self.store.get_run(run_id)
        assert record is not None
        if record["status"] not in FINAL_STATUSES:
            await self.launch(run_id)
        return await self.status(run_id)

    async def cancel(self, run_id: str) -> dict[str, Any]:
        engine = await self.engine(run_id)
        record = await self.store.get_run(run_id)
        assert record is not None
        if record["status"] == "ready_for_review":
            await engine.cancel_external(run_id)
        else:
            stopped = await self.store.request_stop(run_id)
            if stopped["status"] not in FINAL_STATUSES:
                await self.launch(run_id)
        return await self.status(run_id)

    async def models(self) -> list[dict[str, Any]]:
        return [model.model_dump(mode="json") for model in await self.execution.model_catalog()]

    async def settings(
        self, *, selection: Selection | None = None, run_id: str | None = None
    ) -> dict[str, Any]:
        await self.store.initialize()
        if run_id is not None:
            engine = await self.engine(run_id)
            if selection is not None:
                raise ValueError("saved run settings cannot be changed")
            artifacts = ArtifactStore(
                self.workspace,
                run_id,
                storage_root=engine.context.storage_root / "factory" / "artifacts",
                create=False,
            )
            return FactoryConfig.model_validate_json(artifacts.read_text("config.json")).model_dump(
                mode="json"
            )
        async with command_lease(self.store.path, self.context.session_id):
            session = await self.store.session(self.context.session_id)
            if session is None:
                await self.store.save_session(self.context.session_id, self.workspace)
            elif Path(str(session["cwd"])).resolve() != self.workspace:
                raise ValueError("session belongs to a different workspace")
            saved_selection = Selection()
            if session and session["settings_json"]:
                saved = json.loads(session["settings_json"])
                config, saved_selection = (
                    FactoryConfig.model_validate(saved["config"]),
                    Selection.model_validate(saved["selection"]),
                )
            else:
                config = load_config(self.workspace)
            chosen = selection if selection is not None else saved_selection
            resolved = resolve_config(config, chosen, await self.execution.model_catalog())
            if selection is not None or not session or not session["settings_json"]:
                await self.store.save_session_settings(
                    self.context.session_id,
                    json.dumps(
                        {
                            "config": config.model_dump(mode="json"),
                            "selection": chosen.model_dump(mode="json"),
                        }
                    ),
                )
            return {
                "session_id": self.context.session_id,
                "configuration": config.model_dump(mode="json"),
                "selection": chosen.model_dump(mode="json"),
                "resolved": resolved.model_dump(mode="json"),
            }
