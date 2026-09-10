from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from langgraph.errors import NodeCancelledError

from software_factory.bb.client import BBError, BBExecution, BBThreadState
from software_factory.config import AgentConfig
from software_factory.engine import FactoryEngine
from software_factory.events import FactoryEvent
from software_factory.ownership import OwnershipError
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
    "interruption", ["before_create", "after_create", "after_result", "correction"]
)
async def test_restart_reconciles_the_saved_request_before_any_new_dispatch(
    tmp_path: Path, factory_yaml: str, interruption: str
) -> None:
    init_repository(tmp_path, factory_yaml, "raise SystemExit(0)\n")

    class InterruptedBB(FakeBB):
        interrupted = False
        tells = 0

        async def spawn_child(
            self, role: str, prompt: str, agent: AgentConfig, spawn_key: str
        ) -> str:
            if role == "planner" and interruption == "before_create" and not self.interrupted:
                self.interrupted = True
                raise asyncio.CancelledError()
            thread_id = await super().spawn_child(role, prompt, agent, spawn_key)
            if role == "planner" and interruption == "after_create" and not self.interrupted:
                self.interrupted = True
                raise asyncio.CancelledError()
            return thread_id

        async def tell(self, thread_id: str, message: str) -> str:
            receipt = await super().tell(thread_id, message)
            self.tells += 1
            if interruption == "correction" and not self.interrupted:
                self.interrupted = True
                raise asyncio.CancelledError()
            return receipt

    planner = (
        [{"output": "invalid JSON"}, {"output": plan_output()}]
        if interruption == "correction"
        else [{"output": plan_output()}]
    )
    bb = InterruptedBB(
        tmp_path,
        {
            "planner": planner,
            "builder": [{"output": build_output(), "action": write_answer("good")}],
            "reviewer": [{"output": review_output()}],
        },
    )

    async def interrupt_after_result(event: FactoryEvent) -> None:
        if (
            interruption == "after_result"
            and event.node == "plan"
            and event.status == "completed"
            and not bb.interrupted
        ):
            bb.interrupted = True
            raise asyncio.CancelledError()

    engine = FactoryEngine(
        workspace=tmp_path,
        context=context(tmp_path),
        bb=bb,
        database_path=database(tmp_path),
        event_sink=interrupt_after_result,
    )
    with pytest.raises(NodeCancelledError):
        await engine.run("Write the answer", "fct_operation_restart")
    before = await engine.store.get_run("fct_operation_restart")
    result = await engine.resume("fct_operation_restart")
    after = await engine.store.get_run("fct_operation_restart")
    assert before is not None and after is not None
    assert before["deadline_at"] == after["deadline_at"]
    if interruption == "before_create":
        assert result["status"] == "recovery_needed"
        assert not bb.spawn_counts
    else:
        assert result["status"] == "accepted"
        assert bb.spawn_counts == {"planner": 1, "builder": 1, "reviewer": 1}
        assert bb.tells == (1 if interruption == "correction" else 0)
        assert all(
            op["dispatch_count"] == 1 for op in await engine.store.operations(result["run_id"])
        )


@pytest.mark.parametrize("lost_response", ["create", "follow_up", "wait"])
async def test_connection_loss_recovers_without_resending_work(
    tmp_path: Path, factory_yaml: str, lost_response: str
) -> None:
    init_repository(
        tmp_path,
        factory_yaml,
        "from pathlib import Path\nraise SystemExit(Path('answer.txt').read_text() != 'good')\n",
    )

    class DisconnectedBB(FakeBB):
        tells = 0
        lost = False

        async def spawn_child(
            self, role: str, prompt: str, agent: AgentConfig, spawn_key: str
        ) -> str:
            thread_id = await super().spawn_child(role, prompt, agent, spawn_key)
            if lost_response == "create" and not self.lost:
                self.lost = True
                raise BBError("connection reset after acceptance", "connection")
            return thread_id

        async def tell(self, thread_id: str, message: str) -> str:
            receipt = await super().tell(thread_id, message)
            self.tells += 1
            if lost_response == "follow_up" and not self.lost:
                self.lost = True
                raise BBError("connection reset after acceptance", "connection")
            return receipt

        async def wait(self, thread_id: str) -> None:
            if lost_response == "wait" and not self.lost:
                self.lost = True
                raise BBError("connection reset while waiting", "connection")
            await super().wait(thread_id)

    bb = DisconnectedBB(
        tmp_path,
        {
            "planner": [{"output": plan_output()}],
            "builder": [
                {"output": build_output(), "action": write_answer("bad")},
                {"output": build_output(), "action": write_answer("good")},
            ],
            "reviewer": [{"output": review_output()}],
        },
    )
    engine = FactoryEngine(
        workspace=tmp_path, context=context(tmp_path), bb=bb, database_path=database(tmp_path)
    )
    result = await engine.run("Write the answer", "fct_connection_loss")

    assert result["status"] == "accepted"
    assert bb.tells == 1 and bb.spawn_counts == {"planner": 1, "builder": 1, "reviewer": 1}
    assert bb.stopped == []
    operations = await engine.store.operations(result["run_id"])
    assert all(op["dispatch_count"] == 1 for op in operations)
    assert sum(op["retry_count"] for op in operations) == (1 if lost_response == "wait" else 0)


@pytest.mark.parametrize("wrong_identity", ["receipt", "output", "unknown"])
async def test_unrelated_or_unknown_execution_never_supplies_a_result(
    tmp_path: Path, factory_yaml: str, wrong_identity: str
) -> None:
    init_repository(
        tmp_path,
        factory_yaml,
        "from pathlib import Path\nraise SystemExit(Path('answer.txt').read_text() != 'good')\n",
    )

    class UnverifiedBB(FakeBB):
        async def tell(self, thread_id: str, message: str) -> str:
            receipt = await super().tell(thread_id, message)
            return "different-receipt" if wrong_identity == "receipt" else receipt

        async def reconcile_operation(
            self, thread_id: str, operation_id: str
        ) -> BBExecution | None:
            if wrong_identity == "unknown":
                return None
            return await super().reconcile_operation(thread_id, operation_id)

        async def execution(self, thread_id: str, execution_id: str) -> BBExecution:
            if wrong_identity == "output":
                return BBExecution("earlier-request", "completed", plan_output())
            return await super().execution(thread_id, execution_id)

    bb = UnverifiedBB(
        tmp_path,
        {
            "planner": [{"output": plan_output()}],
            "builder": [
                {"output": build_output(), "action": write_answer("bad")},
                {"output": build_output(), "action": write_answer("good")},
            ],
        },
    )
    engine = FactoryEngine(
        workspace=tmp_path, context=context(tmp_path), bb=bb, database_path=database(tmp_path)
    )
    result = await engine.run("Write the answer", "fct_unverified_result")
    assert result["status"] == "recovery_needed"
    assert "reviewer" not in bb.spawn_counts
    operations = await engine.store.operations(result["run_id"])
    if wrong_identity == "receipt":
        repair = next(op for op in operations if op["step"] == "repair")
        assert repair["execution_id"] == "different-receipt"
    assert operations[-1]["state"] != "completed"


@pytest.mark.parametrize("continuation", ["answer", "cancel"])
async def test_waiting_for_user_input_preserves_the_request_and_deadline(
    tmp_path: Path, factory_yaml: str, continuation: str
) -> None:
    init_repository(tmp_path, factory_yaml, "raise SystemExit(0)\n")

    class QuestionBB(FakeBB):
        needs_answer = True

        async def execution(self, thread_id: str, execution_id: str) -> BBExecution:
            if thread_id == "thread-planner" and self.needs_answer:
                return BBExecution(execution_id, "waiting_for_input")
            return await super().execution(thread_id, execution_id)

        async def wait(self, thread_id: str) -> None:
            if thread_id == "thread-planner" and self.needs_answer:
                await asyncio.Event().wait()

    bb = QuestionBB(
        tmp_path,
        {
            "planner": [{"output": plan_output()}],
            "builder": [{"output": build_output(), "action": write_answer("good")}],
            "reviewer": [{"output": review_output()}],
        },
    )
    engine = FactoryEngine(
        workspace=tmp_path, context=context(tmp_path), bb=bb, database_path=database(tmp_path)
    )
    waiting = await asyncio.wait_for(engine.run("Write the answer", "fct_question"), 10)
    assert waiting["status"] == "waiting_for_input"
    operation = (await engine.store.operations(waiting["run_id"]))[0]
    assert operation["state"] == "dispatched"
    if continuation == "answer":
        bb.needs_answer = False
        result = await engine.resume(waiting["run_id"])
        assert result["status"] == "accepted"
    else:
        result = await engine.cancel_external(waiting["run_id"])
        assert result["status"] == "cancelled"
        assert bb.stopped == ["thread-planner"]
    recovered = (await engine.store.operations(waiting["run_id"]))[0]
    assert recovered["deadline_at"] == operation["deadline_at"]
    assert recovered["execution_id"] == operation["execution_id"]
    assert recovered["dispatch_count"] == 1
    assert bb.spawn_counts["planner"] == 1


async def test_connection_retry_budget_survives_restart(tmp_path: Path, factory_yaml: str) -> None:
    init_repository(tmp_path, factory_yaml, "raise SystemExit(0)\n")

    class OfflineBB(FakeBB):
        offline = True

        async def wait(self, thread_id: str) -> None:
            if self.offline:
                raise BBError("connection lost", "connection")

    bb = OfflineBB(
        tmp_path,
        {
            "planner": [{"output": plan_output()}],
            "builder": [{"output": build_output(), "action": write_answer("good")}],
            "reviewer": [{"output": review_output()}],
        },
    )
    engine = FactoryEngine(workspace=tmp_path, context=context(tmp_path), bb=bb)
    result = await engine.run("Write the answer", "fct_retry_budget")
    assert result["status"] == "recovery_needed"
    before = (await engine.store.operations(result["run_id"]))[0]
    assert before["retry_count"] == 2

    resumed_engine = FactoryEngine(workspace=tmp_path, context=context(tmp_path), bb=bb)
    still_offline = await resumed_engine.resume(result["run_id"])
    assert still_offline["status"] == "recovery_needed"
    assert (await engine.store.operations(result["run_id"]))[0]["retry_count"] == 2
    bb.offline = False
    recovered = await resumed_engine.resume(result["run_id"])
    assert recovered["status"] == "accepted"
    after = (await engine.store.operations(result["run_id"]))[0]
    assert after["deadline_at"] == before["deadline_at"]
    assert after["retry_count"] == 2 and after["dispatch_count"] == 1
    assert bb.spawn_counts == {"planner": 1, "builder": 1, "reviewer": 1}


@pytest.mark.parametrize("damaged_artifact", [False, True])
async def test_unconfirmed_stop_keeps_ownership_until_child_exit_is_confirmed(
    tmp_path: Path, factory_yaml: str, damaged_artifact: bool
) -> None:
    init_repository(tmp_path, factory_yaml, "raise SystemExit(0)\n")

    class UncertainBB(FakeBB):
        confirms_stop = False

        async def reconcile_operation(
            self, thread_id: str, operation_id: str
        ) -> BBExecution | None:
            return None

        async def inspect_thread(self, thread_id: str) -> BBThreadState:
            return BBThreadState("stopping", self.confirms_stop)

    bb = UncertainBB(tmp_path, {"planner": [{"output": plan_output()}]})
    engine = FactoryEngine(workspace=tmp_path, context=context(tmp_path), bb=bb)
    result = await engine.run("Write the answer", "fct_stop_uncertain")
    assert result["status"] == "recovery_needed"
    if damaged_artifact:
        Path(result["final_report_path"]).write_text("interrupted report publication")
    stopping = await engine.cancel_external(result["run_id"])
    assert stopping["status"] == "recovery_needed"
    record = await engine.store.get_run(result["run_id"])
    assert record is not None and record["active_child_thread_id"] == "thread-planner"
    assert record["stop_target"] == "cancelled"
    contender = FactoryEngine(
        workspace=tmp_path,
        context=context(tmp_path),
        bb=FakeBB(tmp_path, {}),
        database_path=database(tmp_path).with_name("other.sqlite"),
    )
    with pytest.raises(OwnershipError, match="reserved for recovery"):
        await contender.run("Other work", "fct_unconfirmed_takeover")

    bb.confirms_stop = True
    stopped = await engine.cancel_external(result["run_id"])
    assert stopped["status"] == "cancelled"
    record = await engine.store.get_run(result["run_id"])
    assert record is not None and record["active_child_thread_id"] is None
    assert bb.stopped == ["thread-planner", "thread-planner"]
    if damaged_artifact:
        assert stopped["final_report_path"] == ""


async def test_environment_must_match_the_owned_workspace_before_dispatch(
    tmp_path: Path, factory_yaml: str
) -> None:
    init_repository(tmp_path, factory_yaml, "raise SystemExit(0)\n")
    bb = FakeBB(tmp_path / "different-workspace", {})
    engine = FactoryEngine(workspace=tmp_path, context=context(tmp_path), bb=bb)
    result = await engine.run("Write the answer", "fct_wrong_environment")
    assert result["status"] == "failed"
    assert "owned workspace" in result["error"]
    assert not bb.spawn_counts
    assert await engine.store.operations(result["run_id"]) == []
