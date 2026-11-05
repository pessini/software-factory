"""Local MCP tools over the same Factory control operations used by the CLI."""

from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from software_factory.config import Selection
from software_factory.control import FactoryControl
from software_factory.planning_control import PlanningControl
from software_factory.web import web_link


def create_mcp(control: FactoryControl, planning: PlanningControl | None = None) -> MCPServer:
    server = MCPServer(
        "Software Factory",
        instructions="Use saved Factory run IDs. Status reads do not start work.",
    )

    @server.tool()
    async def factory_start(
        request: str, request_id: str, selection: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Save a request and return its run ID. Reuse the request ID only for identical input."""
        return await control.start(
            request,
            request_id=request_id,
            selection=Selection.model_validate(selection) if selection else None,
        )

    @server.tool()
    async def factory_status(run_id: str, after_event: int = 0) -> dict[str, Any]:
        """Read saved status, pending questions, workers, reports, and missed events."""
        return await control.status(run_id, after_event=after_event)

    @server.tool()
    async def factory_answer(
        run_id: str, interaction_id: str, answers: dict[str, str]
    ) -> dict[str, Any]:
        """Save answers to an exact pending interaction, then continue its run."""
        return await control.answer(run_id, interaction_id, answers)

    @server.tool()
    async def factory_resume(run_id: str) -> dict[str, Any]:
        """Request reconciliation and continuation under the saved limits and authorization."""
        return await control.resume(run_id)

    @server.tool()
    async def factory_cancel(run_id: str) -> dict[str, Any]:
        """Save a stop request. Read status until worker exit is confirmed."""
        return await control.cancel(run_id)

    @server.tool()
    async def factory_models() -> list[dict[str, Any]]:
        """Discover supported native worker model and effort choices."""
        return await control.models()

    @server.tool()
    async def factory_settings(
        run_id: str | None = None, selection: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Read saved run settings, or validate and change settings before a run starts."""
        return await control.settings(
            run_id=run_id,
            selection=Selection.model_validate(selection) if selection is not None else None,
        )

    if planning is not None:
        add_planning_tools(server, planning)
    return server


def add_planning_tools(server: MCPServer, planning: PlanningControl) -> None:
    def linked(result: dict[str, Any]) -> dict[str, Any]:
        return {**result, "web_url": web_link(planning.storage_root, result["session_id"])}

    @server.tool()
    async def factory_plan_start(request: str, request_id: str) -> dict[str, Any]:
        """Start a Grill planning interview. Reuse the request ID only for identical input."""
        return linked(await planning.start(request, request_id=request_id))

    @server.tool()
    async def factory_plan_list() -> list[dict[str, Any]]:
        """List planning sessions for this workspace."""
        return await planning.sessions()

    @server.tool()
    async def factory_plan_status(session_id: str, after_event: int = 0) -> dict[str, Any]:
        """Read the current revision: questions with recommendations, plan, handoff, history."""
        return linked(await planning.status(session_id, after_event=after_event))

    @server.tool()
    async def factory_plan_answer(
        session_id: str, interaction_id: str, answers: dict[str, str]
    ) -> dict[str, Any]:
        """Submit the user's answers to the pending round. Prefix an answer with [discuss] to ask about it."""
        return linked(await planning.answer(session_id, interaction_id, answers))

    @server.tool()
    async def factory_plan_revise(session_id: str, feedback: str) -> dict[str, Any]:
        """Start a new plan revision from user feedback; earlier snapshots stay unchanged."""
        return linked(await planning.revise(session_id, feedback))

    @server.tool()
    async def factory_plan_approve(session_id: str, plan_hash: str) -> dict[str, Any]:
        """After explicit user approval, start one implementation run for this exact plan hash."""
        return linked(await planning.approve(session_id, plan_hash))

    @server.tool()
    async def factory_plan_cancel(session_id: str) -> dict[str, Any]:
        """Stop the current planning revision."""
        return linked(await planning.cancel(session_id))


def serve_mcp(
    workspace: Path, storage_root: Path | None = None, session_id: str | None = None
) -> None:
    create_mcp(
        FactoryControl(workspace, storage_root=storage_root, session_id=session_id),
        PlanningControl(workspace, storage_root=storage_root, session_id=session_id),
    ).run(transport="stdio")
