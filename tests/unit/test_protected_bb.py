import json

import pytest

from software_factory.bb import BBContext
from software_factory.bb.client import BBError, RecoveryNeeded
from software_factory.bb.protected import ProtectedBBClient
from software_factory.config import AgentConfig
from software_factory.worker_records import WorkerReceipt, WorkerRecords


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    context = BBContext("parent", "project", "environment", tmp_path / "parent")
    client = ProtectedBBClient(context, tmp_path / "workspace")
    records = WorkerRecords(tmp_path / "child")
    calls = []

    async def read(path):
        if path.endswith("/queued-messages"):
            return []
        return {
            "id": "child",
            "parentThreadId": "parent",
            "projectId": "project",
            "environmentId": "environment",
            "providerId": "acp-software-factory-worker",
            "status": "idle",
            "activeBackgroundAgentCount": 0,
            "queuedMessageCount": 0,
        }

    async def storage(thread):
        return tmp_path / thread

    async def command(*args):
        calls.append(args)
        return {"thread": {"id": "child"}}

    async def stop(name, operation):
        return True

    async def inspect(name, operation):
        return {"status": {"state": "stopped"}}

    monkeypatch.setattr("software_factory.bb.protected.read_bb", read)
    monkeypatch.setattr("software_factory.bb.protected.thread_storage", storage)
    monkeypatch.setattr("software_factory.bb.protected.stop_container", stop)
    monkeypatch.setattr("software_factory.bb.protected.inspect_container", inspect)
    monkeypatch.setattr(client, "_run", command)
    choice = AgentConfig(
        provider="codex", model="selected", reasoning_level="high", permission_mode="auto"
    )
    receipt = WorkerReceipt(
        operation_id="run:plan:1:create",
        container_id="owned-container",
        request_hash="hash",
        requested=choice,
    )
    return client, records, receipt, calls


@pytest.mark.parametrize("creation_requested", [False, True])
async def test_cancel_recovers_receipt_after_worker_crash(bridge, creation_requested):
    client, records, receipt, calls = bridge
    receipt.container_creation_requested = creation_requested
    records.save(receipt)
    await client.stop("child")
    assert records.read(receipt.operation_id).stopped
    assert records.read(receipt.operation_id).status == "stopped"
    assert records.stop_requested
    assert (await client.inspect_thread("child")).stopped


async def test_cancel_keeps_uncertain_creation_open(bridge, monkeypatch):
    client, records, receipt, calls = bridge
    receipt.container_creation_requested = True
    records.save(receipt)

    async def unknown(*args):
        return False

    monkeypatch.setattr("software_factory.bb.protected.stop_container", unknown)
    with pytest.raises(RecoveryNeeded):
        await client.stop("child")
    assert not records.read(receipt.operation_id).stopped


async def test_dispatch_sends_only_authorized_operation_reference(bridge):
    client, records, receipt, calls = bridge
    await client.spawn_child(
        "planner",
        "Original prompt\n\nFactory operation: run:plan:1:create\n",
        receipt.requested,
        "run:planner",
    )
    args = calls[-1]
    assert args[args.index("--provider") + 1] == "acp-software-factory-worker"
    message = json.loads(args[args.index("--prompt") + 1])
    assert message == {
        "factory_worker": 1,
        "parent_thread_id": "parent",
        "operation_id": receipt.operation_id,
    }
    with pytest.raises(BBError, match="saved operation"):
        await client.spawn_child("planner", "unrecorded request", receipt.requested, "run:planner")


async def test_output_is_not_complete_until_exit_is_confirmed(bridge):
    client, records, receipt, calls = bridge
    receipt.status, receipt.execution_id, receipt.output = "completed", "exact", "answer"
    records.save(receipt)
    assert (await client.execution("child", "exact")).status == "running"
    with pytest.raises(RecoveryNeeded):
        await client.execution("child", "old-turn")
    receipt.stopped = True
    records.save(receipt)
    assert (await client.execution("child", "exact")).output == "answer"
