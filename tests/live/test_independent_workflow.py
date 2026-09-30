"""Opt-in native acceptance through real CLI/MCP processes, with BB absent."""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from software_factory.config import default_config
from software_factory.control import FactoryControl
from software_factory.state import FINAL_STATUSES
from software_factory.worker_records import WorkerRecords
from tests.conftest import init_repository

pytestmark = pytest.mark.skipif(
    os.environ.get("FACTORY_LIVE_NATIVE") != "1",
    reason="requires explicit model/usage authorization, a login, and Apple Container",
)

REQUEST = (
    "Implement double(value: int) -> int in source.py. Return twice the input for "
    "negative, zero, and positive integers. Add unittest behavior tests in test_double.py. "
    "Keep quality_check.py and .factory/factory.yaml unchanged. Do not commit or push."
)


@pytest.mark.parametrize("interface", ["cli", "mcp"])
async def test_native_workflow_without_bb(tmp_path, monkeypatch, interface):
    model = os.environ["FACTORY_ACCEPTANCE_MODEL"]
    output = Path(os.environ["FACTORY_ACCEPTANCE_OUTPUT"]).resolve()
    output.mkdir(parents=True, exist_ok=True)
    for name in tuple(os.environ):
        if name.startswith("BB_"):
            monkeypatch.delenv(name)
    workspace = tmp_path / "target"
    workspace.mkdir()
    config = yaml.safe_load(default_config())
    config["review_mode"] = "single"
    for agent in config["agents"].values():
        agent.update(model=model, reasoning_level="low")
    config["quality"]["commands"] = [
        {
            "name": "behavior",
            "argv": [sys.executable, "-B", "quality_check.py"],
            "timeout_seconds": 30,
        }
    ]
    config["limits"].update(
        max_test_repairs=0,
        max_review_revisions=0,
        total_run_timeout_seconds=360,
        overall_run_timeout_seconds=360,
        step_timeout_seconds=120,
        connection_retries=0,
    )
    config["safety"]["protected_paths"].append("quality_check.py")
    (workspace / "source.py").write_text("def double(value: int) -> int:\n    return value\n")
    init_repository(
        workspace,
        yaml.safe_dump(config),
        "from source import double\nfor value in (-3, 0, 4):\n    assert double(value) == 2 * value\n",
    )
    storage = tmp_path / "controller"
    control = FactoryControl(workspace, storage_root=storage)
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "software_factory.cli", "--storage", str(storage), "mcp", "-w", str(workspace)],
        env=dict(os.environ),
    )
    request_id = "native-acceptance-" + uuid4().hex
    record = {"interface": interface, "model": model, "effort": "low", "result": "incomplete"}
    run_id = None
    try:
        if interface == "cli":
            result = await asyncio.to_thread(
                subprocess.run,
                [
                    sys.executable,
                    "-m",
                    "software_factory.cli",
                    "--storage",
                    str(storage),
                    "start",
                    REQUEST,
                    "--request-id",
                    request_id,
                    "-w",
                    str(workspace),
                ],
                env=dict(os.environ),
                text=True,
                capture_output=True,
                timeout=90,
                check=True,
            )
            started = json.loads(result.stdout)
        else:
            async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(
                    "factory_start", {"request": REQUEST, "request_id": request_id}
                )
                assert not result.isError, result
                started = result.structuredContent
        run_id = started["run_id"]
        record.update(run_id=run_id, workspace=str(workspace), storage=str(storage))
        # Reconnect after the initiating CLI or MCP process has exited.
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            async with asyncio.timeout(390):
                while True:
                    response = await session.call_tool("factory_status", {"run_id": run_id})
                    assert not response.isError, response
                    status = response.structuredContent
                    if status["status"] in FINAL_STATUSES or status["status"] == "recovery_needed":
                        break
                    await asyncio.sleep(1)
        record["status"] = status
        assert status["status"] == "ready_for_review", status
        assert len([operation for operation in status["operations"] if operation["role"]]) == 3
        assert all(op["dispatch_count"] == 1 for op in status["operations"])
        receipts = [
            receipt.model_dump(mode="json")
            for directory in (storage / "factory" / "workers").iterdir()
            for receipt in WorkerRecords(directory).all()
        ]
        record["receipts"] = receipts
        assert len(receipts) == 3
        assert all(receipt["stopped"] for receipt in receipts)
        assert all(receipt["connection_evidence"]["actual_model"] == model for receipt in receipts)
        record["result"] = "passed"
    finally:
        if run_id and record["result"] != "passed":
            await control.cancel(run_id)
        (output / (request_id + ".json")).write_text(json.dumps(record, indent=2) + "\n")
