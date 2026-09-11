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


async def test_reconcile_waits_for_receipt_while_bb_reports_active(bridge, monkeypatch):
    from software_factory.bb import protected

    client, records, receipt, calls = bridge
    original = protected.read_bb

    async def active(path):
        value = await original(path)
        if isinstance(value, dict):
            value["status"] = "active"
            if records.root.exists():
                receipt.execution_id, receipt.status = "actual-turn", "running"
                records.save(receipt)
            else:
                records.root.mkdir(parents=True)
        return value

    monkeypatch.setattr(protected, "read_bb", active)
    execution = await client.reconcile_operation("child", receipt.operation_id)
    assert execution.execution_id == "actual-turn"


async def test_followup_waits_for_receipt_before_bb_reports_active(bridge, monkeypatch):
    from software_factory.bb import protected

    client, records, receipt, calls = bridge
    original = protected.read_bb
    reads = 0

    async def delayed_start(path):
        nonlocal reads
        value = await original(path)
        if isinstance(value, dict):
            reads += 1
            if reads == 3:
                receipt.execution_id, receipt.status = "followup-turn", "running"
                records.save(receipt)
        return value

    monkeypatch.setattr(protected, "read_bb", delayed_start)
    execution = await client.reconcile_operation("child", receipt.operation_id)
    assert execution is not None
    assert execution.execution_id == "followup-turn"


async def test_stopped_bb_task_can_retain_its_previous_error_status(bridge, monkeypatch):
    from software_factory.bb import protected

    client, records, receipt, calls = bridge
    original = protected.read_bb

    async def errored(path):
        value = await original(path)
        if isinstance(value, dict):
            value["status"] = "error"
        return value

    monkeypatch.setattr(protected, "read_bb", errored)
    await client.stop("child")
    assert (await client.inspect_thread("child")).stopped


@pytest.mark.parametrize(
    "saved_stop,container_state,queued,expected",
    [
        (True, "stopped", False, True),
        (False, "stopped", False, False),
        (True, "running", False, False),
        (True, "stopped", True, False),
    ],
)
async def test_active_bb_handoff_requires_all_exit_evidence(
    bridge, monkeypatch, saved_stop, container_state, queued, expected
):
    from software_factory.bb import protected

    client, records, receipt, calls = bridge
    receipt.container_creation_requested, receipt.stopped = True, saved_stop
    receipt.status, receipt.execution_id = "completed", "actual-turn"
    records.save(receipt)
    original = protected.read_bb

    async def active(path):
        value = await original(path)
        if isinstance(value, dict):
            value["status"] = "active"
        elif queued:
            value = [{"id": "pending"}]
        return value

    async def inspect(*args):
        return {"status": {"state": container_state}}

    monkeypatch.setattr(protected, "read_bb", active)
    monkeypatch.setattr(protected, "inspect_container", inspect)
    assert (await client.inspect_thread("child")).stopped is expected
