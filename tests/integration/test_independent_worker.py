"""Saved Factory authorization at the protected provider boundary, without BB."""

import time

import pytest

from software_factory.artifacts import ArtifactStore
from software_factory.config import AgentConfig, Selection, SettingsSnapshot, load_config
from software_factory.context import FactoryContext, checkpoint_path
from software_factory.execution import RecoveryNeeded
from software_factory.persistence import RunStore
from software_factory.worker import WorkerExecutor, worker_identity
from tests.conftest import init_repository
from tests.integration.test_worker import Provider


@pytest.fixture
async def independent_worker(tmp_path, factory_yaml):
    workspace = tmp_path / "target"
    workspace.mkdir()
    init_repository(workspace, factory_yaml, "pass\n")
    context = FactoryContext.open(workspace, storage_root=tmp_path / "controller")
    store = RunStore(checkpoint_path(context))
    await store.initialize()
    await store.create_run("run1", workspace, context.session_id, deadline_at=time.time() + 60)
    operation = await store.prepare_operation(
        "run1", "build", 1, "create", "Original request", role="builder", timeout_seconds=60
    )
    await store.update_operation(operation["operation_id"], state="dispatching", dispatch_count=1)
    choice = AgentConfig(
        provider="codex",
        model="selected-model",
        reasoning_level="high",
        permission_mode="accept-edits",
    )
    config = load_config(workspace)
    config.resolved_settings = SettingsSnapshot(
        config_version=1,
        requested=Selection(),
        effective={step: choice for step in ["plan", "build", "repair", "review", "revision"]},
        catalog=[],
    )
    ArtifactStore(
        workspace, "run1", storage_root=context.storage_root / "factory/artifacts"
    ).write_json("config.json", config)
    Provider.instances = []
    worker = WorkerExecutor(workspace, context, worker_identity("run1", "builder"), Provider)
    return worker, operation["operation_id"], store


async def test_native_worker_uses_saved_request_and_exact_execution(independent_worker):
    worker, operation_id, _ = independent_worker
    assert await worker.execute(operation_id) == "correct output"
    assert await worker.execute(operation_id) == "correct output"
    assert len(Provider.instances) == 1
    assert Provider.instances[0].calls[-1][1]["input"][0]["text"] == "Original request"
    assert Provider.instances[0].closed


async def test_uncertain_native_execution_is_never_repeated(independent_worker):
    worker, operation_id, _ = independent_worker
    await worker.execute(operation_id)
    receipt = worker.records.read(operation_id)
    receipt.status, receipt.stopped, receipt.output = "running", False, None
    worker.records.save(receipt)
    with pytest.raises(RecoveryNeeded, match="will not be sent again"):
        await worker.execute(operation_id)
    assert len(Provider.instances) == 1


async def test_worker_identity_cannot_authorize_another_role(independent_worker):
    worker, operation_id, _ = independent_worker
    wrong = WorkerExecutor(
        worker.workspace, worker.context, worker_identity("run1", "planner"), Provider
    )
    with pytest.raises(ValueError, match="different worker"):
        await wrong.execute(operation_id)
    assert Provider.instances == []


async def test_saved_stop_prevents_native_dispatch(independent_worker):
    worker, operation_id, store = independent_worker
    await store.request_stop("run1")
    with pytest.raises(ValueError, match="stopped before provider"):
        await worker.execute(operation_id)
    assert Provider.instances == []


@pytest.mark.parametrize("role,step", [("planner", "plan"), ("reviewer", "review")])
async def test_independent_reader_roles_select_read_only_container(independent_worker, role, step):
    worker, _, store = independent_worker
    operation = await store.prepare_operation(
        "run1", step, 1, "create", "Read the source", role=role, timeout_seconds=60
    )
    await store.update_operation(operation["operation_id"], state="dispatching", dispatch_count=1)
    reader = WorkerExecutor(
        worker.workspace, worker.context, worker_identity("run1", role), Provider
    )
    await reader.execute(operation["operation_id"])
    assert Provider.instances[0].read_only
