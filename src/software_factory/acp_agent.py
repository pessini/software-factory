"""BB ACP presentation over the shared Factory controls."""

from __future__ import annotations

import json
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import acp
from acp import (
    LoadSessionResponse,
    NewSessionResponse,
    PromptResponse,
    update_agent_message_text,
    update_user_message_text,
)
from acp.schema import (
    SessionConfigOptionSelect,
    SessionNotification,
    SetSessionConfigOptionResponse,
)

from software_factory.acp_presentation import ACPPresentation
from software_factory.bb.acp_input import request_text
from software_factory.context import FactoryContext, checkpoint_path
from software_factory.control import FactoryControl
from software_factory.execution import ExecutionManager
from software_factory.panel import PanelRequest, panel_request
from software_factory.persistence import RunStore
from software_factory.state import FactoryState

PLANNING_COMMANDS = frozenset(
    {"plan", "plans", "plan-status", "plan-answer", "plan-revise", "plan-approve"}
)


class SoftwareFactoryAgent(ACPPresentation):
    def __init__(
        self,
        context: FactoryContext,
        execution: ExecutionManager | None = None,
        launch: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self.factory_context = context
        self.execution = execution
        self.launch = launch
        self.connection: acp.Client | None = None
        self.store = RunStore(checkpoint_path(context))

    def control(self, workspace: Path, session_id: str) -> FactoryControl:
        return FactoryControl(
            workspace,
            storage_root=self.factory_context.storage_root,
            session_id=session_id,
            execution=self.execution,
            launch=self.launch,
        )

    async def new_session(self, cwd: str, **kwargs: Any) -> NewSessionResponse:
        await self.store.initialize()
        session_id = "session_" + uuid4().hex
        workspace = Path(cwd).resolve(strict=True)
        if reference := os.environ.get("BB_THREAD_ID"):
            session_id = await self.store.interface_session("bb", reference, workspace)
        await self.store.save_session(session_id, workspace)
        await self.store.activate_session(session_id)
        options = await self._options(workspace, session_id)
        return NewSessionResponse(session_id=session_id, config_options=list(options))

    async def _options(
        self, workspace: Path, session_id: str, request: PanelRequest | None = None
    ) -> list[SessionConfigOptionSelect]:
        result = await panel_request(
            workspace,
            self.control(workspace, session_id).context,
            request or PanelRequest(action="read"),
            execution=self.execution,
            session_id=session_id,
        )
        return [
            SessionConfigOptionSelect.model_validate(item)
            for item in cast(list[Any], result["configOptions"])
        ]

    async def load_session(self, cwd: str, session_id: str, **kwargs: Any) -> LoadSessionResponse:
        await self.store.initialize()
        session = await self.store.session(session_id)
        if session is None or Path(str(session["cwd"])).resolve() != Path(cwd).resolve():
            raise ValueError("session does not belong to this workspace")
        if reference := os.environ.get("BB_THREAD_ID"):
            await self.store.interface_session("bb", reference, Path(cwd), session_id)
        await self.store.activate_session(session_id)
        await self._sync_events(session_id, publish=False)
        if self.connection is not None:
            for update in await self.store.session_history(session_id):
                notification = SessionNotification.model_validate(
                    {"sessionId": session_id, "update": update}
                )
                await self.connection.session_update(session_id, notification.update)
        try:
            options = await self._options(Path(cwd), session_id)
        except Exception:
            options = [
                SessionConfigOptionSelect.model_validate(item)
                for item in json.loads(session.get("config_options_json") or "[]")
            ]
        return LoadSessionResponse(config_options=list(options))

    async def set_config_option(
        self, session_id: str, config_id: str, value: str, **kwargs: Any
    ) -> SetSessionConfigOptionResponse:
        session = await self.store.session(session_id)
        if session is None:
            raise ValueError("unknown Factory session")
        options = await self._options(
            Path(str(session["cwd"])),
            session_id,
            PanelRequest(action="change", config_id=config_id, value=value),
        )
        return SetSessionConfigOptionResponse(config_options=list(options))

    async def prompt(self, session_id: str, prompt: list[Any], **kwargs: Any) -> PromptResponse:
        await self.store.initialize()
        session = await self.store.session(session_id)
        if session is None:
            raise ValueError("unknown Factory session")
        request = request_text(prompt).strip()
        if request.startswith("[bb system]\n"):
            return PromptResponse(stop_reason="end_turn")
        if not request:
            raise ValueError("request must not be empty")
        control = self.control(Path(str(session["cwd"])), session_id)
        first, _, argument = request.partition(" ")
        command = first.lower().lstrip("/")
        run_id = session["run_id"]
        await self._send(session_id, update_user_message_text(request), publish=False)
        if command in PLANNING_COMMANDS:
            text = await self._planning(Path(str(session["cwd"])), command, argument)
            await self._send(session_id, update_agent_message_text(text))
            return PromptResponse(stop_reason="end_turn")
        if command == "attach":
            await control.engine(argument)
            await self.store.save_session(session_id, Path(str(session["cwd"])), argument)
            run_id = argument
            result = await control.status(run_id)
        elif command == "new" or (
            run_id is None
            and command
            not in {"status", "inspect", "resume", "continue", "cancel", "answer", "retry"}
        ):
            result = await control.start(
                argument if command == "new" else request,
                request_id=str(kwargs.get("request_id") or uuid4().hex),
            )
        elif run_id is None:
            await self._send(
                session_id,
                update_agent_message_text("Send a work request, or /attach FACTORY_RUN_ID."),
            )
            return PromptResponse(stop_reason="end_turn")
        elif command in {"status", "inspect"}:
            result = await control.status(run_id)
        elif command in {"resume", "continue", "retry"}:
            result = await control.resume(run_id)
        elif command == "answer":
            interaction, answers = await self._parse_answer(run_id, argument)
            result = await control.answer(run_id, interaction.interaction_id, answers)
        elif command == "cancel":
            result = await control.cancel(run_id)
        else:
            await self._send(
                session_id,
                update_agent_message_text(
                    "Use /status, /continue, /answer ID ANSWER, /cancel, or /new REQUEST."
                ),
            )
            return PromptResponse(stop_reason="end_turn")
        await self._sync_events(session_id)
        await self._result(session_id, cast(FactoryState, result))
        return PromptResponse(stop_reason="end_turn")

    async def _planning(self, workspace: Path, command: str, argument: str) -> str:
        from software_factory.planning_control import PlanningControl, planning_summary
        from software_factory.web import web_link

        planning = PlanningControl(
            workspace,
            storage_root=self.factory_context.storage_root,
            execution=self.execution,
            launch=self.launch,
        )
        try:
            if command == "plans":
                sessions = await planning.sessions()
                return (
                    "\n".join(
                        f"{item['session_id']} r{item['revision']} {item['status']}: {item['request'][:80]}"
                        for item in sessions
                    )
                    or "No planning sessions. Start one with /plan REQUEST."
                )
            if command == "plan":
                if not argument.strip():
                    return "Use /plan REQUEST to start a Grill planning interview."
                result = await planning.start(argument, request_id=uuid4().hex)
            else:
                target, _, rest = argument.strip().partition(" ")
                if not target:
                    return f"Use /{command} PLANNING_SESSION_ID ..."
                if command == "plan-status":
                    result = await planning.status(target)
                elif command == "plan-answer":
                    current = await planning.status(target)
                    pending = current["pending"]
                    if pending is None:
                        return "This planning session has no pending questions."
                    answers = json.loads(rest)
                    if not isinstance(answers, dict):
                        return "Send a JSON object with one answer per question ID."
                    result = await planning.answer(target, pending["interaction_id"], answers)
                elif command == "plan-revise":
                    result = await planning.revise(target, rest)
                else:
                    result = await planning.approve(target, rest.strip())
        except (ValueError, json.JSONDecodeError) as exc:
            return f"Planning command failed: {exc}"
        link = web_link(planning.storage_root, result["session_id"])
        return planning_summary(result) + (
            f"\nOpen in browser: {link}"
            if link
            else "\nRun `software-factory web` to open the planning UI."
        )

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        # ACP cancellation ends presentation. Only the explicit /cancel control stops a job.
        return None


async def serve_acp() -> None:
    context = FactoryContext.open(Path.cwd())
    await acp.run_agent(cast(acp.Agent, SoftwareFactoryAgent(context)))
