"""Local MCP tools over the same Factory control operations used by the CLI."""

from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from software_factory.config import Selection
from software_factory.control import FactoryControl


def create_mcp(control: FactoryControl) -> FastMCP:
    server = FastMCP(
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

    return server


def serve_mcp(
    workspace: Path, storage_root: Path | None = None, session_id: str | None = None
) -> None:
    create_mcp(FactoryControl(workspace, storage_root=storage_root, session_id=session_id)).run(
        transport="stdio"
    )
