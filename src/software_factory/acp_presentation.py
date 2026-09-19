"""ACP messages and saved history. Workflow commands live in the shared controller."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import acp
from acp import InitializeResponse, update_agent_message_text
from acp.helpers import SessionUpdate
from acp.schema import AgentCapabilities, Implementation

from software_factory import __version__
from software_factory.events import FactoryEvent
from software_factory.interactions import Interaction
from software_factory.persistence import RunStore
from software_factory.session_presentation import EventPresentation
from software_factory.state import FINAL_STATUSES, FactoryState

COMMAND_HELP = "Use /status, /continue, /answer INTERACTION_ID ANSWER, /cancel, or /new REQUEST."


class ACPPresentation:
    connection: acp.Client | None
    store: RunStore

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
            protocol_version=acp.PROTOCOL_VERSION,
            agent_capabilities=AgentCapabilities(load_session=True),
            agent_info=Implementation(
                name="software-factory", title="Software Factory", version=__version__
            ),
        )

    async def _parse_answer(self, run_id: str, argument: str) -> tuple[Interaction, dict[str, str]]:
        interaction_id, separator, answer_text = argument.strip().partition(" ")
        interaction = await self.store.interaction(run_id, interaction_id)
        if interaction is None or not separator:
            raise ValueError("use /answer INTERACTION_ID ANSWER for a saved question")
        answers = (
            {interaction.questions[0].id: answer_text.strip()}
            if len(interaction.questions) == 1 and not answer_text.lstrip().startswith("{")
            else json.loads(answer_text)
        )
        if not isinstance(answers, dict):
            raise ValueError("answers must be a JSON object with one answer per question ID")
        interaction.validate_answers(answers)
        return interaction, answers

    async def _send(
        self,
        session_id: str,
        update: SessionUpdate,
        *,
        key: str | None = None,
        publish: bool = True,
    ) -> None:
        added = await self.store.append_session_update(
            session_id, key or uuid4().hex, update.model_dump(mode="json", by_alias=True)
        )
        if publish and added and self.connection is not None:
            await self.connection.session_update(session_id, update)

    async def _sync_events(self, session_id: str, *, publish: bool = True) -> None:
        for run_id in await self.store.session_runs(session_id):
            presentation = EventPresentation()
            for raw in await self.store.events(run_id):
                event = FactoryEvent.model_validate(raw)
                for index, update in enumerate(presentation.updates(event)):
                    await self._send(
                        session_id,
                        update,
                        key=f"event:{run_id}:{event.sequence}:{index}",
                        publish=publish,
                    )

    async def _result(
        self,
        session_id: str,
        result: FactoryState,
        *,
        publish: bool = True,
    ) -> None:
        run_id = result["run_id"]
        summary = f"Run {run_id}: {result['status']}."
        pending = [
            Interaction.model_validate(item)
            for item in result.get("interactions", [])
            if item["state"] == "waiting"
        ]
        if pending:
            summary += "\n\n" + "\n\n".join(item.display() for item in pending)
        elif result["status"] == "waiting_for_input" and any(
            item["state"] in {"answered", "delivered"} for item in result.get("interactions", [])
        ):
            summary += "\nAnswer saved. Use /status to read continuation progress."
        elif result.get("error"):
            summary += "\n" + str(result["error"])
        if (
            not pending
            and result.get("status") in FINAL_STATUSES
            and result.get("final_report_path")
        ):
            path = Path(str(result["final_report_path"]))
            if path.is_file():
                summary += "\n\n" + path.read_text(encoding="utf-8")
        summary += "\n\n" + COMMAND_HELP
        if not publish and any(
            item.get("sessionUpdate") == "agent_message_chunk"
            and item.get("content", {}).get("text") == summary
            for item in await self.store.session_history(session_id)
        ):
            return
        sequence = len(await self.store.events(run_id))
        await self._send(
            session_id,
            update_agent_message_text(summary),
            key=f"result:{run_id}:{result['status']}:{sequence}" if not publish else None,
            publish=publish,
        )
