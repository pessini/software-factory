#!/usr/bin/env python3
"""Real HTTP/CLI/ACP/MCP and Docker, simulated native model; no live credentials."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import mkdtemp

from acp.schema import TextContentBlock
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from software_factory.acp_agent import SoftwareFactoryAgent
from software_factory.context import FactoryContext

ROOT = Path(__file__).resolve().parents[1]


def load_fixture():
    spec = importlib.util.spec_from_file_location(
        "worker_acceptance", ROOT / "scripts/accept-rust-workers.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def main():
    module = load_fixture()
    root = Path(mkdtemp(prefix="issue-18-", dir=ROOT / ".checks"))
    shutil.copy2(ROOT / "target/debug/factory-runtime", root / "factory-runtime")
    fixture = module.Fixture(root, "journey", "factory-test-worker:dev")
    policy = fixture.workspace / ".factory/factory.yaml"
    policy.parent.mkdir()
    policy.write_text(json.dumps(fixture.config))  # JSON is YAML; no new dependency.
    module.execute(["git", "add", "."], cwd=fixture.workspace)
    module.execute(["git", "commit", "-qm", "Policy"], cwd=fixture.workspace)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    token = "issue-18-local-acceptance-token"
    origin = f"http://127.0.0.1:{port}"
    env = {
        **os.environ,
        "FACTORY_HTTP_BIND": f"127.0.0.1:{port}",
        "FACTORY_HTTP_TOKEN": token,
        "FACTORY_CONTROL_URL": origin,
        "FACTORY_HOME": str(root / "client-storage"),
    }
    child = None
    log = (root / "service.log").open("w+")

    def api(operation, **payload):
        request = urllib.request.Request(
            origin + "/api/control/" + operation,
            data=json.dumps(payload).encode(),
            headers={"authorization": f"Bearer {token}", "content-type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=90) as response:
            return json.load(response)

    def boot():
        process = subprocess.Popen(fixture.argv("serve"), env=env, stdout=log, stderr=log)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            assert process.poll() is None, (root / "service.log").read_text()
            try:
                api("plan_list")
                return process
            except OSError:
                time.sleep(0.1)
        raise AssertionError("HTTP service did not start")

    def wait_status(session, expected):
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            value = api("plan_status", session_id=session)
            if value["status"] == expected:
                return value
            assert value["status"] not in {"failed", "blocked", "recovery_needed"}, value
            time.sleep(0.2)
        raise AssertionError(f"did not reach {expected}")

    try:
        child = boot()
        started = api("plan_start", request="Update greeting", request_id="cross-client")
        session = started["session_id"]
        waiting = wait_status(session, "waiting_for_input")
        pending = waiting["pending"]
        child.kill()
        child.wait(timeout=15)
        child = boot()
        assert api("plan_status", session_id=session)["pending"] == pending
        module.execute(
            [
                sys.executable,
                "-m",
                "software_factory.cli",
                "plan-answer",
                session,
                pending["interaction_id"],
                '{"recipient":"world"}',
                "-w",
                str(fixture.workspace),
            ],
            env=env,
        )
        planned = wait_status(session, "planned")
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "software_factory.cli", "mcp", "-w", str(fixture.workspace)],
            env=env,
        )
        for _ in range(2):
            async with stdio_client(params) as (read, write), ClientSession(read, write) as client:
                await client.initialize()
                status = await client.call_tool("factory_plan_status", {"session_id": session})
                assert not status.is_error, status
                assert status.structured_content["rounds"][0]["answers"] == {"recipient": "world"}
        payload = {"session_id": session, "plan_hash": planned["plan_hash"]}
        with ThreadPoolExecutor(2) as pool:
            approvals = list(pool.map(lambda _: api("plan_approve", **payload), range(2)))
        run_id = approvals[0]["implementation"]["run_id"]
        assert approvals[1]["implementation"]["run_id"] == run_id
        child.kill()
        child.wait(timeout=15)
        child = boot()
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            result = api("status", run_id=run_id)
            if result["status"] == "ready_for_review":
                break
            assert result["status"] not in {"failed", "blocked", "recovery_needed"}, result
            await asyncio.sleep(0.2)
        assert result["status"] == "ready_for_review", result
        assert "Hello, world!" in (fixture.workspace / "greeting.txt").read_text()
        context = FactoryContext.open(fixture.workspace, storage_root=root / "acp-storage")
        # In-process ACP presentation, using its public session and prompt methods.
        previous = {key: os.environ.get(key) for key in env if key.startswith("FACTORY_")}
        os.environ.update({key: value for key, value in env.items() if key.startswith("FACTORY_")})
        try:
            agent = SoftwareFactoryAgent(context)
            chat = await agent.new_session(str(fixture.workspace))
            await agent.prompt(
                chat.session_id, [TextContentBlock(type="text", text=f"/plan-status {session}")]
            )
            await agent.prompt(
                chat.session_id, [TextContentBlock(type="text", text=f"/attach {run_id}")]
            )
            assert (await agent.store.session(chat.session_id))["run_id"] == run_id
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        assert "ready_for_review" in api("plan_report", session_id=session)["report"]
        operations = fixture.call("status", run_id)["workflow"]["operations"]
        assert all(op["dispatch_count"] == 1 for op in operations.values())
        record = {
            "schema_version": 1,
            "binary_sha256": hashlib.sha256((root / "factory-runtime").read_bytes()).hexdigest(),
            "source_hashes": {
                str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted((ROOT / "rust/factory-runtime/src").rglob("*"))
                if path.is_file()
            },
            "kind": "simulated_native_provider_real_docker_http_cli_acp_mcp",
            "planning_session_id": session,
            "implementation_run_id": run_id,
            "status": result["status"],
            "root": str(root),
            "service_restarts": 2,
            "operations": len(operations),
            "browser": "not available",
        }
        (ROOT / "docs/acceptance/issue-18.json").write_text(json.dumps(record, indent=2) + "\n")
        print(json.dumps(record), flush=True)
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            child.wait(timeout=15)
        log.close()
        fixture.cleanup_containers()


if __name__ == "__main__":
    asyncio.run(main())
