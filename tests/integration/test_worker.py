"""Exercise saved controller operations through the worker protocol boundary."""

import asyncio
import time

import pytest

from software_factory.artifacts import ArtifactStore
from software_factory.bb import BBContext
from software_factory.bb.client import RecoveryNeeded
from software_factory.bb.worker_acp import WorkerAgent
from software_factory.config import AgentConfig, Selection, SettingsSnapshot, load_config
from software_factory.persistence import RunStore
from software_factory.worker_records import WorkerMessage
from tests.conftest import init_repository


class Provider:
    instances = []

    def __init__(self, *args, **kwargs):
        self.notifications = asyncio.Queue()
        self.calls = []
        self.closed = False
        self.read_only = kwargs["read_only"]
        self.instances.append(self)

    async def start(self, *, before_create=None):
        if before_create is not None:
            before_create()

    async def close(self):
        self.closed = True

    async def request(self, method, params):
        self.calls.append((method, params))
        if method in {"thread/start", "thread/resume"}:
            return {
                "thread": {"id": "provider-thread"},
                "model": "selected-model",
                "reasoningEffort": "high",
            }
        assert method == "turn/start"
        for turn, text in [("stale-turn", "wrong output"), ("exact-turn", "correct output")]:
            await self.notifications.put(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": "provider-thread",
                        "turn": {
                            "id": turn,
                            "status": "completed",
                            "items": [{"type": "agentMessage", "text": text}],
                        },
                    },
                }
            )
        return {"turn": {"id": "exact-turn"}}


@pytest.fixture
async def worker(tmp_path, factory_yaml, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    init_repository(workspace, factory_yaml, "pass\n")
    parent = tmp_path / "parent"
    store = RunStore(parent / "factory/checkpoints.sqlite")
    await store.initialize()
    await store.create_run("run1", workspace, "parent", deadline_at=time.time() + 60)
    operation = await store.prepare_operation(
        "run1", "build", 1, "create", "Original request", role="builder", timeout_seconds=60
    )
    await store.update_operation(operation["operation_id"], state="dispatching", dispatch_count=1)
    config = load_config(workspace)
    choice = AgentConfig(
        provider="codex",
        model="selected-model",
        reasoning_level="high",
        permission_mode="accept-edits",
    )
    config.resolved_settings = SettingsSnapshot(
        config_version=1,
        requested=Selection(),
        effective={step: choice for step in ["plan", "build", "repair", "review", "revision"]},
        catalog=[],
    )
    artifacts = ArtifactStore(workspace, "run1", storage_root=parent / "factory/artifacts")
    artifacts.write_text("config.json", config.model_dump_json())

    async def read_bb(path):
        return {
            "title": "Builder · Software Factory · run1:builder",
            "parentThreadId": "parent",
            "projectId": "project",
            "environmentId": "environment",
        }

    async def storage(thread):
        assert thread == "parent"
        return parent

    monkeypatch.setattr("software_factory.bb.worker_acp.read_bb", read_bb)
    monkeypatch.setattr("software_factory.bb.worker_acp.thread_storage", storage)
    Provider.instances = []
    agent = WorkerAgent(BBContext("child", "project", "environment", tmp_path / "child"), Provider)
    await agent.new_session(str(workspace))
    return (
        agent,
        WorkerMessage(parent_thread_id="parent", operation_id=operation["operation_id"]),
        store,
    )


async def test_worker_matches_exact_turn_and_does_not_repeat_completed_request(worker):
    agent, message, store = worker
    assert await agent.execute(message) == "correct output"
    receipt = agent.records.read(message.operation_id)
    assert receipt.execution_id == "exact-turn"
    assert receipt.reported.model == "selected-model"
    assert receipt.stopped
    assert Provider.instances[0].closed
    assert not Provider.instances[0].read_only
    assert Provider.instances[0].calls[-1][1]["input"][0]["text"] == "Original request"
    assert await agent.execute(message) == "correct output"
    assert len(Provider.instances) == 1


async def test_worker_does_not_repeat_uncertain_request(worker):
    agent, message, store = worker
    await agent.execute(message)
    receipt = agent.records.read(message.operation_id)
    receipt.status, receipt.stopped, receipt.output = "running", False, None
    agent.records.save(receipt)
    with pytest.raises(RecoveryNeeded, match="will not be sent again"):
        await agent.execute(message)
    assert len(Provider.instances) == 1


async def test_worker_rejects_another_parent_before_provider_start(worker):
    agent, message, store = worker
    with pytest.raises(ValueError, match="does not belong"):
        await agent.execute(message.model_copy(update={"parent_thread_id": "other-parent"}))
    assert not Provider.instances


async def test_worker_honors_saved_cancel_before_provider_start(worker):
    agent, message, store = worker
    await store.request_stop("run1")
    with pytest.raises(ValueError, match="stopped before provider"):
        await agent.execute(message)
    assert not Provider.instances


async def test_cancel_during_repository_discovery_is_not_lost(worker, monkeypatch):
    from software_factory.bb.worker_acp import protected_paths

    agent, message, store = worker

    async def cancelled_discovery(workspace, config):
        paths = await protected_paths(workspace, config)
        await agent.cancel("child")
        return paths

    monkeypatch.setattr("software_factory.bb.worker_acp.protected_paths", cancelled_discovery)
    assert await agent.execute(message) == "worker cancelled"
    receipt = agent.records.read(message.operation_id)
    assert receipt.stopped
    assert not receipt.container_creation_requested
    assert not Provider.instances[0].calls


async def test_worker_rejects_model_substitution_and_stops_provider(worker):
    agent, message, store = worker

    class SubstitutingProvider(Provider):
        async def request(self, method, params):
            response = await super().request(method, params)
            if method == "thread/start":
                response["model"] = "different-model"
            return response

    agent.provider_factory = SubstitutingProvider
    assert "changed the requested model" in await agent.execute(message)
    assert agent.records.read(message.operation_id).status == "failed"
    assert agent.records.read(message.operation_id).stopped
    assert all(method != "turn/start" for method, params in Provider.instances[0].calls)


async def test_worker_rejects_an_operation_assigned_to_another_child(worker):
    agent, message, store = worker
    await store.update_operation(message.operation_id, thread_id="other-child")
    with pytest.raises(ValueError, match="different child"):
        await agent.execute(message)
    assert not Provider.instances


async def test_startup_validation_failure_does_not_claim_a_container(worker):
    agent, message, store = worker

    class InvalidConfiguration(Provider):
        async def start(self, *, before_create=None):
            raise ValueError("login file is missing")

    agent.provider_factory = InvalidConfiguration
    assert await agent.execute(message) == "login file is missing"
    receipt = agent.records.read(message.operation_id)
    assert receipt.stopped
    assert not receipt.container_creation_requested
    assert not Provider.instances[0].calls


async def test_completed_worker_holds_bb_notice_until_next_controller_request(worker):
    from acp.schema import TextContentBlock

    agent, message, store = worker
    ready = asyncio.Event()

    class Client:
        async def session_update(self, *args):
            ready.set()

    agent.connection = Client()
    task = asyncio.create_task(
        agent.prompt("child", [TextContentBlock(type="text", text=message.model_dump_json())])
    )
    async with asyncio.timeout(5):
        await ready.wait()
        assert not task.done()
        assert agent.records.read(message.operation_id).stopped
        await agent.cancel("child")
        assert (await task).stop_reason == "end_turn"
    assert not agent.cancelled.is_set()
    operation = await store.prepare_operation(
        "run1", "repair", 1, "follow_up", "Repair request", role="builder", timeout_seconds=60
    )
    await store.update_operation(operation["operation_id"], state="dispatching", dispatch_count=1)
    await agent.execute(message.model_copy(update={"operation_id": operation["operation_id"]}))
    assert Provider.instances[-1].calls[0][0] == "thread/resume"
