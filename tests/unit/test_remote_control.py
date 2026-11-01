"""Existing transport adapters share the authenticated versioned control boundary."""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from typer.testing import CliRunner

from software_factory.cli import app
from software_factory.control import FactoryControl
from software_factory.planning_control import PlanningControl
from software_factory.remote import RemoteTransport


@pytest.fixture
def service(tmp_path, monkeypatch):
    calls = []
    workspace = tmp_path / "target"
    workspace.mkdir()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            assert self.headers["authorization"] == "Bearer token"
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"workspace": str(workspace), "sessions": []}).encode())

        def do_POST(self):
            value = json.loads(self.rfile.read(int(self.headers["content-length"])))
            operation = self.path.rsplit("/", 1)[-1]
            calls.append((operation, value))
            self.send_response(409 if value.get("plan_hash") == "stale" else 200)
            self.end_headers()
            result = (
                {"error": "stale plan hash"}
                if value.get("plan_hash") == "stale"
                else {
                    "session_id": "pln_saved",
                    "run_id": "run-saved",
                    "status": "waiting_for_input",
                }
            )
            self.wfile.write(json.dumps(result).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("FACTORY_CONTROL_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("FACTORY_HTTP_TOKEN", "token")
    monkeypatch.setenv("FACTORY_HOME", str(tmp_path / "storage"))
    try:
        yield workspace, calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


async def test_shared_controls_and_cli_use_saved_remote_ids(service):
    workspace, calls = service
    planning = PlanningControl(workspace)
    await planning.answer("pln_saved", "round-1", {"recipient": "world"})
    control = FactoryControl(workspace)
    assert (await control.status("run-saved", after_event=4))["run_id"] == "run-saved"
    result = await asyncio.to_thread(
        CliRunner().invoke, app, ["plan-status", "pln_saved", "-w", str(workspace)]
    )
    assert result.exit_code == 0, result.output
    assert calls == [
        (
            "plan_answer",
            {
                "session_id": "pln_saved",
                "interaction_id": "round-1",
                "answers": {"recipient": "world"},
            },
        ),
        ("status", {"run_id": "run-saved", "after_event": 4}),
        ("plan_status", {"session_id": "pln_saved", "after_event": 0}),
    ]
    with pytest.raises(ValueError, match="stale plan hash"):
        await planning.approve("pln_saved", "stale")


async def test_wrong_workspace_fails_before_any_remote_mutation(service, tmp_path):
    _, calls = service
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(ValueError, match="different workspace"):
        await PlanningControl(other).start("Greet", request_id="one")
    assert calls == []


def test_remote_credentials_require_an_origin_and_https_off_loopback(tmp_path):
    for url in [
        "http://example.com",
        "https://name:secret@example.com",
        "https://example.com/path",
        "file:///tmp/service",
    ]:
        with pytest.raises(ValueError):
            RemoteTransport(url, "token", tmp_path)


async def test_acp_retries_keep_the_callers_request_identity(service):
    from acp.schema import TextContentBlock

    from software_factory.acp_agent import SoftwareFactoryAgent
    from software_factory.context import FactoryContext

    workspace, calls = service
    agent = SoftwareFactoryAgent(FactoryContext.open(workspace))
    # Public session storage is presentation state; this test supplies its session
    # without invoking the unrelated model-settings panel on the fixture service.
    await agent.store.initialize()
    await agent.store.save_session("chat", workspace)
    for _ in range(2):
        await agent.prompt(
            "chat", [TextContentBlock(type="text", text="/new Greet")], request_id="stable-request"
        )
    starts = [payload for operation, payload in calls if operation == "start"]
    assert [payload["request_id"] for payload in starts] == ["stable-request", "stable-request"]
