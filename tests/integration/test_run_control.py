from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from langgraph.errors import NodeCancelledError

from software_factory.engine import FactoryEngine
from software_factory.events import FactoryEvent
from tests.conftest import FakeBB, init_repository
from tests.integration.test_factory_engine import (
    build_output,
    context,
    database,
    plan_output,
    review_output,
    write_answer,
)


@pytest.mark.parametrize(
    "boundary",
    [
        ("bootstrap", "started"),
        ("bootstrap", "completed"),
        ("baseline", "started"),
        ("baseline", "completed"),
        ("finalize", "started"),
        ("plan", "started"),
        ("plan", "completed"),
        ("build", "started"),
        ("build", "completed"),
        ("test", "started"),
        ("test", "failed"),
        ("test", "completed"),
        ("repair", "started"),
        ("repair", "completed"),
        ("review", "started"),
        ("review", "failed"),
        ("review", "completed"),
        ("revise", "started"),
        ("revise", "completed"),
    ],
)
async def test_cancellation_at_a_node_boundary_cannot_become_accepted(
    tmp_path: Path, factory_yaml: str, boundary: tuple[str, str]
) -> None:
    init_repository(
        tmp_path,
        factory_yaml,
        "from pathlib import Path\nraise SystemExit(Path('answer.txt').exists() and Path('answer.txt').read_text() != 'good')\n",
    )
    bb = FakeBB(
        tmp_path,
        {
            "planner": [{"output": plan_output()}],
            "builder": [
                {"output": build_output(), "action": write_answer("bad")},
                {"output": build_output(), "action": write_answer("good")},
                {"output": build_output(), "action": write_answer("good")},
            ],
            "reviewer": [{"output": review_output(False)}, {"output": review_output()}],
        },
    )

    async def cancel_after_review(event: FactoryEvent) -> None:
        if (event.node, event.status) == boundary:
            await engine.cancel_external(event.run_id)

    engine = FactoryEngine(
        workspace=tmp_path,
        context=context(tmp_path),
        execution=bb,
        database_path=database(tmp_path),
        event_sink=cancel_after_review,
    )
    result = await engine.run("Write the answer", "fct_cancel_review")

    assert result["status"] == "cancelled"
    record = await engine.store.get_run(result["run_id"])
    assert record is not None and record["status"] == "cancelled"
    assert "CANCELLED" in Path(result["final_report_path"]).read_text()
    summary = Path(result["final_report_path"]).with_name("run-summary.json")
    assert json.loads(summary.read_text())["status"] == "cancelled"
    assert not any(
        event["metadata"].get("status") == "ready_for_review"
        for event in await engine.store.events(result["run_id"])
    )


async def test_resume_after_repair_dispatch_does_not_send_the_repair_twice(
    tmp_path: Path, factory_yaml: str
) -> None:
    init_repository(
        tmp_path,
        factory_yaml,
        "from pathlib import Path\nraise SystemExit(Path('answer.txt').exists() and Path('answer.txt').read_text() != 'good')\n",
    )

    class InterruptedRepairBB(FakeBB):
        tell_count = 0

        async def continue_worker(self, thread_id: str, message: str) -> str:
            receipt = await super().continue_worker(thread_id, message)
            self.tell_count += 1
            if self.tell_count == 1:
                raise asyncio.CancelledError()
            return receipt

    bb = InterruptedRepairBB(
        tmp_path,
        {
            "planner": [{"output": plan_output()}],
            "builder": [
                {"output": build_output(), "action": write_answer("bad")},
                {"output": build_output(), "action": write_answer("good")},
                {"output": build_output(), "action": write_answer("good")},
            ],
            "reviewer": [{"output": review_output()}],
        },
    )
    engine = FactoryEngine(
        workspace=tmp_path,
        context=context(tmp_path),
        execution=bb,
        database_path=database(tmp_path),
    )
    with pytest.raises(NodeCancelledError):
        await engine.run("Write the answer", "fct_repair_restart")
    result = await engine.resume("fct_repair_restart")

    assert result["status"] == "ready_for_review"
    assert bb.tell_count == 1
    assert result["test_repair_count"] == 1
    assert result["test_attempt_count"] == 2
    operations = await engine.store.operations(result["run_id"])
    repair = next(op for op in operations if op["step"] == "repair")
    assert repair["dispatch_count"] == 1 and repair["state"] == "completed"


async def test_failed_wait_stops_the_child_before_a_final_outcome(
    tmp_path: Path, factory_yaml: str
) -> None:
    init_repository(tmp_path, factory_yaml, "raise SystemExit(0)\n")
    bb = FakeBB(tmp_path, {"planner": [{"output": plan_output()}]})
    bb.wait_error = RuntimeError("provider failed during wait")
    engine = FactoryEngine(
        workspace=tmp_path,
        context=context(tmp_path),
        execution=bb,
        database_path=database(tmp_path),
    )
    result = await engine.run("Write the answer", "fct_wait_failure")

    assert result["status"] == "blocked"
    assert bb.stopped == ["thread-planner"]
    record = await engine.store.get_run(result["run_id"])
    assert record is not None and record["active_child_thread_id"] is None
    assert await engine.store.processes(result["run_id"]) == []


async def test_cancel_after_acceptance_returns_the_existing_result(
    tmp_path: Path, factory_yaml: str
) -> None:
    init_repository(tmp_path, factory_yaml, "raise SystemExit(0)\n")
    bb = FakeBB(
        tmp_path,
        {
            "planner": [{"output": plan_output()}],
            "builder": [{"output": build_output(), "action": write_answer("good")}],
            "reviewer": [{"output": review_output()}],
        },
    )
    cancellations = []

    async def cancel_on_acceptance(event: FactoryEvent) -> None:
        if event.node == "finalize" and event.status == "completed":
            cancellations.append(await engine.cancel_external(event.run_id))

    engine = FactoryEngine(
        workspace=tmp_path,
        context=context(tmp_path),
        execution=bb,
        database_path=database(tmp_path),
        event_sink=cancel_on_acceptance,
    )
    result = await engine.run("Write the answer", "fct_already_accepted")
    resumed = await engine.resume(result["run_id"])

    assert result["status"] == cancellations[0]["status"] == resumed["status"] == "ready_for_review"
    assert result["changed_files"] == resumed["changed_files"] == ["answer.txt"]
    assert resumed["test_attempt_count"] == 1
    assert bb.stopped == []


async def test_step_deadline_stops_a_child_with_a_hanging_wait(
    tmp_path: Path, factory_yaml: str
) -> None:
    config = factory_yaml.replace("limits:\n", "limits:\n  step_timeout_seconds: 1\n")
    init_repository(tmp_path, config, "raise SystemExit(0)\n")

    class HangingBB(FakeBB):
        async def wait(self, thread_id: str) -> None:
            await asyncio.Event().wait()

    bb = HangingBB(tmp_path, {"planner": [{"output": plan_output()}]})
    engine = FactoryEngine(workspace=tmp_path, context=context(tmp_path), execution=bb)
    result = await asyncio.wait_for(engine.run("Write the answer", "fct_step_timeout"), 5)
    assert result["status"] == "blocked"
    assert "step time limit exceeded" in result["error"]
    assert bb.stopped == ["thread-planner"]
    record = await engine.store.get_run(result["run_id"])
    assert record is not None and record["active_child_thread_id"] is None
