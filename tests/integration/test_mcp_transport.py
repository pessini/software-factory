"""Exercise real MCP stdio and detached jobs without sending model requests."""

import asyncio
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from software_factory.control import FactoryControl
from tests.conftest import FakeBB, init_repository


async def test_mcp_disconnect_keeps_saved_cancel_and_controller_job(tmp_path, factory_yaml):
    workspace = tmp_path / "target"
    workspace.mkdir()
    init_repository(workspace, factory_yaml, "pass\n")
    storage = tmp_path / "controller"

    async def prepare_only(run_id):
        pass

    control = FactoryControl(
        workspace, storage_root=storage, execution=FakeBB(workspace, {}), launch=prepare_only
    )
    run_id = (await control.start("Write the answer", request_id="mcp-disconnect"))["run_id"]
    env = {key: value for key, value in os.environ.items() if not key.startswith("BB_")}
    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "software_factory.cli",
            "--storage",
            str(storage),
            "mcp",
            "--workspace",
            str(workspace),
        ],
        env=env,
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        assert {tool.name for tool in (await session.list_tools()).tools} == {
            "factory_start",
            "factory_status",
            "factory_answer",
            "factory_resume",
            "factory_cancel",
            "factory_models",
            "factory_settings",
        }
        result = await session.call_tool("factory_cancel", {"run_id": run_id})
        assert not result.isError, result
        assert result.structuredContent["status"] in {"stopping", "cancelled"}
    # A fresh server has no in-memory state from the first connection.
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        async with asyncio.timeout(15):
            while True:
                result = await session.call_tool("factory_status", {"run_id": run_id})
                assert not result.isError, result
                if result.structuredContent["status"] == "cancelled":
                    break
                await asyncio.sleep(0.1)
        assert result.structuredContent["operations"] == []
        assert result.structuredContent["jobs"]
