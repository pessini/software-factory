from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4

import acp
from acp import (
    InitializeResponse,
    LoadSessionResponse,
    NewSessionResponse,
    PromptResponse,
    plan_entry,
    start_tool_call,
    update_agent_message_text,
    update_plan,
    update_tool_call,
)
from acp.schema import (
    AgentCapabilities,
    Implementation,
    SetSessionConfigOptionResponse,
)

from software_factory import __version__
from software_factory.bb import BBClient, BBClientProtocol, BBContext
from software_factory.bb.acp_input import request_text
from software_factory.engine import FactoryEngine, checkpoint_path, new_run_id
from software_factory.events import FactoryEvent
from software_factory.persistence import RunStore
from software_factory.session_settings import SessionSettings

PHASES = ["bootstrap", "plan", "build", "test", "repair", "review", "revise", "finalize"]


class SoftwareFactoryAgent:
    def __init__(self, context: BBContext, bb: BBClientProtocol | None = None) -> None:
        self.context = context
        self.bb = bb
        self.connection: acp.Client | None = None
        self.store = RunStore(checkpoint_path(context))
        self.cancellations: dict[str, asyncio.Event] = {}

    def on_connect(self, conn: acp.Client) -> None:
        self.connection = conn

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: Any = None,
        client_info: Any = None,
        **kwargs: Any,
    ) -> InitializeResponse:
        del client_capabilities, client_info, kwargs
        return InitializeResponse(
            protocol_version=protocol_version,
            agent_capabilities=AgentCapabilities(load_session=True),
            agent_info=Implementation(
                name="software-factory", title="Software Factory", version=__version__
            ),
        )

    async def new_session(
        self,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[Any] | None = None,
        **kwargs: Any,
    ) -> NewSessionResponse:
        del additional_directories, mcp_servers, kwargs
        session_id = f"factory_session_{uuid4().hex}"
        await self.store.initialize()
        previous = await self.store.latest_session(Path(cwd))
        await self.store.save_session(session_id, Path(cwd).resolve())
        if previous and previous["settings_json"]:
            await self.store.save_session_settings(session_id, str(previous["settings_json"]))
        options = await self.settings(Path(cwd)).options(session_id)
        await self.store.activate_session(session_id)
        return NewSessionResponse(session_id=session_id, config_options=list(options))

    async def load_session(
        self,
        cwd: str,
        session_id: str,
        mcp_servers: list[Any] | None = None,
        additional_directories: list[str] | None = None,
        **kwargs: Any,
    ) -> LoadSessionResponse:
        del mcp_servers, additional_directories, kwargs
        await self.store.initialize()
        session = await self.store.session(session_id)
        if session is None:
            raise ValueError(f"unknown ACP session: {session_id}")
        if Path(str(session["cwd"])).resolve() != Path(cwd).resolve():
            raise ValueError("ACP session belongs to a different workspace")
        options = await self.settings(Path(cwd)).options(session_id)
        await self.store.activate_session(session_id)
        return LoadSessionResponse(config_options=list(options))

    def settings(self, workspace: Path) -> SessionSettings:
        return SessionSettings(
            self.store, self.bb or BBClient.from_context(self.context, workspace)
        )

    async def set_config_option(
        self, session_id: str, config_id: str, value: str, **kwargs: Any
    ) -> SetSessionConfigOptionResponse:
        del kwargs
        await self.store.initialize()
        session = await self.store.session(session_id)
        if session is None:
            raise ValueError(f"unknown ACP session: {session_id}")
        options = await self.settings(Path(str(session["cwd"]))).change(
            session_id, config_id, value
        )
        return SetSessionConfigOptionResponse(config_options=list(options))

    async def prompt(self, session_id: str, prompt: list[Any], **kwargs: Any) -> PromptResponse:
        from software_factory.ownership import session_lease

        if request_text(prompt).startswith("[bb system]\n"):
            # BB echoes child completion back to the parent as a system prompt.
            # The controller already reads the exact saved child result.
            return PromptResponse(stop_reason="end_turn")
        await self.store.initialize()
        with session_lease(self.store.path, session_id):
            session = await self.store.session(session_id)
            if session and session["run_id"]:
                await self.store.release_unstarted_session_run(session_id, str(session["run_id"]))
            return await self._prompt_owned(session_id, prompt, **kwargs)

    async def _prompt_owned(
        self, session_id: str, prompt: list[Any], **kwargs: Any
    ) -> PromptResponse:
        del kwargs
        if self.connection is None:
            raise RuntimeError("ACP client connection is unavailable")
        connection = self.connection
        await self.store.initialize()
        session = await self.store.session(session_id)
        if session is None:
            raise ValueError(f"unknown ACP session: {session_id}")
        request = request_text(prompt)
        configuration, selection = await self.settings(Path(str(session["cwd"]))).read(session_id)
        run_id = new_run_id()
        await self.store.claim_session_run(session_id, run_id, session["settings_json"])
        cancellation = asyncio.Event()
        self.cancellations[run_id] = cancellation
        statuses: dict[str, Literal["pending", "in_progress", "completed"]] = {
            phase: "pending" for phase in PHASES
        }

        async def sink(event: FactoryEvent) -> None:
            node = event.node
            if node in statuses:
                next_status = cast(
                    Literal["pending", "in_progress", "completed"],
                    {
                        "started": "in_progress",
                        "completed": "completed",
                        "failed": "completed",
                        "cancelled": "completed",
                    }.get(event.status, statuses[node]),
                )
                statuses[node] = next_status
                await connection.session_update(
                    session_id,
                    update_plan(
                        plan_entry(phase.title(), status=statuses[phase]) for phase in PHASES
                    ),
                )
            call_id = f"{run_id}:{node}:{event.attempt}"
            if event.status in {"started", "info"}:
                update: Any = start_tool_call(
                    call_id,
                    event.message,
                    kind="execute",
                    status="completed" if event.status == "info" else "in_progress",
                    raw_input={"run_id": run_id, "node": node, **event.metadata},
                )
            else:
                status: Literal["completed", "failed"] = (
                    "completed" if event.status in {"completed", "info"} else "failed"
                )
                update = update_tool_call(
                    call_id,
                    title=event.message,
                    status=status,
                    raw_output={"run_id": run_id, "event": event.model_dump(mode="json")},
                )
            await connection.session_update(session_id, update)

        engine = FactoryEngine(
            workspace=Path(str(session["cwd"])),
            context=self.context,
            event_sink=sink,
            bb=self.bb,
            cancel_event=cancellation,
        )
        try:
            await connection.session_update(
                session_id,
                update_plan(plan_entry(phase.title(), status="pending") for phase in PHASES),
            )
            result = await engine.run(
                request, run_id, selection=selection, configuration=configuration
            )
            report_path = (
                Path(result.get("workspace_path", str(session["cwd"])))
                / result["final_report_path"]
            )
            report = report_path.read_text(encoding="utf-8")
            await connection.session_update(session_id, update_agent_message_text(report))
            reason: Literal["cancelled", "end_turn"] = (
                "cancelled" if result.get("status") == "cancelled" else "end_turn"
            )
            return PromptResponse(stop_reason=reason)
        finally:
            self.cancellations.pop(run_id, None)
            await self.store.release_unstarted_session_run(session_id, run_id)

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        del kwargs
        session = await self.store.session(session_id)
        if session is None or not session.get("run_id"):
            return
        run_id = str(session["run_id"])
        cancellation = self.cancellations.get(run_id)
        if cancellation:
            cancellation.set()
        engine = FactoryEngine(workspace=Path(str(session["cwd"])), context=self.context)
        await engine.cancel_external(run_id)


async def serve_acp() -> None:
    context = BBContext.from_environment()
    await acp.run_agent(cast(acp.Agent, SoftwareFactoryAgent(context)))
