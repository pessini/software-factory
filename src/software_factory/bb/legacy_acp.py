from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Literal, cast
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
from software_factory.bb import BBClient, BBClientProtocol, BBContext
from software_factory.bb.acp_input import request_text
from software_factory.bb.client import BBError, RecoveryNeeded
from software_factory.engine import FactoryEngine, checkpoint_path, new_run_id
from software_factory.events import FactoryEvent
from software_factory.ownership import OwnershipError, session_lease
from software_factory.persistence import RunStore
from software_factory.session_settings import SessionSettings

COMMAND_HELP = (
    "Use /status to inspect this run, /continue to resume it, /retry to retry recovery, "
    "/answer INTERACTION_ID ANSWER to answer a saved question, /cancel to stop, "
    "or /new REQUEST to start new work after this run ends."
)


class SoftwareFactoryAgent(ACPPresentation):
    def __init__(self, context: BBContext, execution: BBClientProtocol | None = None) -> None:
        self.context = context
        self.bb = execution
        self.connection: acp.Client | None = None
        self.store = RunStore(checkpoint_path(context))
        self.cancellations: dict[str, asyncio.Event] = {}

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
        try:
            options = await self.settings(Path(cwd)).options(session_id)
        except (BBError, ValueError) as exc:
            options = [
                SessionConfigOptionSelect.model_validate(item)
                for item in json.loads(session.get("config_options_json") or "[]")
            ]
            await self._send(
                session_id,
                update_agent_message_text(
                    f"Saved history is available. Settings could not load: {exc}"
                ),
                publish=False,
            )
        await self.store.activate_session(session_id)
        await self._sync_events(session_id, publish=False)
        session = await self.store.session(session_id)
        if session and session["run_id"] and await self.store.get_run(str(session["run_id"])):
            engine = FactoryEngine(workspace=Path(cwd), context=self.context, execution=self.bb)
            await self._result(
                session_id, await engine.inspect(str(session["run_id"])), publish=False
            )
        if self.connection is not None:
            for update in await self.store.session_history(session_id):
                notification = SessionNotification.model_validate(
                    {"sessionId": session_id, "update": update}
                )
                await self.connection.session_update(session_id, notification.update)
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
        if request_text(prompt).startswith("[bb system]\n"):
            # BB echoes child completion back to the parent as a system prompt.
            # The controller already reads the exact saved child result.
            return PromptResponse(stop_reason="end_turn")
        await self.store.initialize()
        while True:
            owns_session = False
            try:
                with session_lease(self.store.path, session_id):
                    owns_session = True
                    session = await self.store.session(session_id)
                    if session and session["run_id"]:
                        await self.store.release_unstarted_session_run(
                            session_id, str(session["run_id"])
                        )
                    return await self._prompt_owned(session_id, prompt, **kwargs)
            except OwnershipError:
                if owns_session:
                    raise
                request = request_text(prompt).strip()
                session = await self.store.session(session_id)
                if (
                    not request.lower().startswith("/answer ")
                    or not session
                    or not session["run_id"]
                ):
                    raise
                run_id = str(session["run_id"])
                interaction, answers = await self._parse_answer(run_id, request.partition(" ")[2])
                if interaction.answers is None:
                    # The first answer may own the session before its database write.
                    # Wait for that write or for ownership to become available.
                    await asyncio.sleep(0.01)
                    continue
                if interaction.answers != answers:
                    raise ValueError(
                        "this interaction already has a different saved answer"
                    ) from None
                engine = FactoryEngine(
                    workspace=Path(str(session["cwd"])), context=self.context, execution=self.bb
                )
                result = await engine.inspect(run_id)
                await self._send(session_id, update_user_message_text(request), publish=False)
                await self._result(session_id, result)
                return PromptResponse(
                    stop_reason="cancelled" if result["status"] == "cancelled" else "end_turn"
                )

    async def _prompt_owned(
        self, session_id: str, prompt: list[Any], **kwargs: Any
    ) -> PromptResponse:
        del kwargs
        if self.connection is None:
            raise RuntimeError("ACP client connection is unavailable")
        session = await self.store.session(session_id)
        if session is None:
            raise ValueError(f"unknown ACP session: {session_id}")
        request = request_text(prompt).strip()
        if not request:
            raise ValueError("request must not be empty")
        await self._send(session_id, update_user_message_text(request), publish=False)
        first, _, argument = request.partition(" ")
        command = first.lower().lstrip("/")
        normalized = request.lower().rstrip("?.!")
        if normalized in {
            "status",
            "/status",
            "inspect",
            "/inspect",
            "what is the status",
            "what's the status",
            "what is the status of this run",
        }:
            command = "status"
        elif normalized in {
            "continue",
            "/continue",
            "resume",
            "/resume",
            "retry",
            "/retry",
            "cancel",
            "/cancel",
        }:
            command = normalized.lstrip("/")
        elif first.lower() not in {"/new", "/answer"}:
            command = "message"
        run_id = str(session["run_id"]) if session["run_id"] else None
        cancellation = asyncio.Event()

        async def sink(event: FactoryEvent) -> None:
            del event
            try:
                await self._sync_events(session_id)
            except (ConnectionError, BrokenPipeError) as exc:
                raise RecoveryNeeded(
                    "ACP client disconnected; reconnect and continue the saved run"
                ) from exc

        engine = FactoryEngine(
            workspace=Path(str(session["cwd"])),
            context=self.context,
            event_sink=sink,
            execution=self.bb,
            cancel_event=cancellation,
        )
        if command == "message" and run_id:
            await self._send(session_id, update_agent_message_text(COMMAND_HELP))
            return PromptResponse(stop_reason="end_turn")
        if not run_id and command not in {"message", "new"}:
            await self._send(
                session_id,
                update_agent_message_text(
                    "This session has no run. Send a work request to start one."
                ),
            )
            return PromptResponse(stop_reason="end_turn")
        if command in {"message", "new"}:
            request = argument.strip() if command == "new" else request
            if not request:
                raise ValueError("/new requires a work request")
            configuration, selection = await self.settings(Path(str(session["cwd"]))).read(
                session_id
            )
            # read() can initialize an old session's saved settings.
            session = await self.store.session(session_id)
            assert session is not None
            run_id = new_run_id()
            await self.store.claim_session_run(session_id, run_id, session["settings_json"])
        assert run_id is not None
        self.cancellations[run_id] = cancellation
        try:
            if command in {"message", "new"}:
                await self._send(session_id, update_agent_message_text(f"Starting run {run_id}."))
                result = await engine.run(
                    request, run_id, selection=selection, configuration=configuration
                )
            elif command == "status":
                result = await engine.inspect(run_id)
            elif command == "retry":
                result = await engine.retry(run_id)
            elif command == "cancel":
                result = await engine.cancel_external(run_id)
            elif command == "answer":
                interaction, answers = await self._parse_answer(run_id, argument)
                result = await engine.answer(run_id, interaction.interaction_id, answers)
            else:
                result = await engine.resume(run_id)
            await self._sync_events(session_id)
            await self._result(session_id, result)
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
        engine = FactoryEngine(
            workspace=Path(str(session["cwd"])), context=self.context, execution=self.bb
        )
        result = await engine.cancel_external(run_id)
        await self._sync_events(session_id)
        await self._result(session_id, result)


async def serve_acp() -> None:
    context = BBContext.from_environment()
    await acp.run_agent(cast(acp.Agent, SoftwareFactoryAgent(context)))
