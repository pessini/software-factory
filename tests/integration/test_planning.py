"""Issue 10: Grill planning sessions and the approved-plan handoff, with scripted workers."""

import json
import subprocess
from pathlib import Path

import pytest

from software_factory.control import FactoryControl
from software_factory.planning_control import PlanningControl, planning_summary
from tests.conftest import FakeBB, init_repository
from tests.integration.test_factory_engine import (
    build_output,
    plan_output,
    review_output,
    write_answer,
)

CHECK = {"id": "criterion-001", "description": "answer.txt contains good"}


def grill_questions(*, affected: list[str] | None = None) -> str:
    return json.dumps(
        {
            "version": 1,
            "status": "questions",
            "summary": "The answer file needs an agreed value.",
            "topics": [
                {"id": "value", "title": "Answer value", "state": "ready"},
                {
                    "id": "format",
                    "title": "File format",
                    "state": "waiting",
                    "depends_on": ["value"],
                },
            ],
            "questions": [
                {
                    "id": "value",
                    "question": "Which value should answer.txt contain?",
                    "options": ["good", "fine"],
                    "recommendation": "good",
                    "context": "Existing checks expect a short word.",
                }
            ],
            "affected": affected or [],
        }
    )


def grill_ready(goal: str = "Write the agreed answer") -> str:
    return json.dumps(
        {
            "version": 1,
            "status": "ready",
            "summary": "answer.txt will contain good.",
            "topics": [
                {"id": "value", "title": "Answer value", "state": "settled", "decision": "good"}
            ],
            "decisions": [
                {
                    "id": "value",
                    "title": "Answer value",
                    "decision": "Write good",
                    "rationale": "Chosen by the user",
                }
            ],
            "plan": {
                "goal": goal,
                "scope": ["Create answer.txt"],
                "requirements": ["answer.txt contains exactly good"],
                "completion_checks": [CHECK],
                "exclusions": ["No other files"],
            },
        }
    )


def setup(tmp_path: Path, factory_yaml: str, planner: list[str], implement: bool = False):
    workspace = tmp_path / "target"
    workspace.mkdir()
    init_repository(
        workspace,
        factory_yaml,
        "from pathlib import Path\nraise SystemExit(Path('answer.txt').exists() and Path('answer.txt').read_text() != 'good')\n",
    )
    scripts = {"planner": [{"output": output} for output in planner]}
    if implement:
        scripts["planner"].append({"output": plan_output()})
        scripts["builder"] = [{"output": build_output(), "action": write_answer("good")}]
        scripts["reviewer"] = [{"output": review_output()}]
    execution = FakeBB(workspace, scripts)
    launched: list[str] = []
    holder: dict[str, PlanningControl] = {}

    async def launch(run_id: str) -> None:
        # Run the detached job inline: the same resume path a job process uses.
        launched.append(run_id)
        engine = await holder["planning"].base.engine(run_id)
        await engine.resume(run_id)

    planning = PlanningControl(
        workspace, storage_root=tmp_path / "controller", execution=execution, launch=launch
    )
    holder["planning"] = planning
    return workspace, execution, planning, launched


def clean(workspace: Path) -> bool:
    return not subprocess.check_output(["git", "status", "--porcelain"], cwd=workspace, text=True)


async def test_interview_round_trip_produces_an_immutable_plan(tmp_path, factory_yaml):
    workspace, execution, planning, _ = setup(
        tmp_path, factory_yaml, [grill_questions(), grill_ready()]
    )
    waiting = await planning.start("Plan the answer file", request_id="plan-1")
    assert waiting["status"] == "waiting_for_input", waiting["error"]
    question = waiting["pending"]["questions"][0]
    assert question["recommendation"] == "good"
    assert waiting["rounds"][0]["envelope"]["topics"][1]["state"] == "waiting"
    assert "Recommended" in planning_summary(waiting)

    again = await planning.start("Plan the answer file", request_id="plan-1")
    assert again["run_id"] == waiting["run_id"]
    with pytest.raises(ValueError, match="one non-empty answer"):
        await planning.answer(waiting["session_id"], waiting["pending"]["interaction_id"], {})

    planned = await planning.answer(
        waiting["session_id"], waiting["pending"]["interaction_id"], {"value": "good"}
    )
    assert planned["status"] == "planned", planned["error"]
    assert planned["pending"] is None
    assert planned["snapshot"]["plan"]["completion_checks"] == [CHECK]
    assert planned["snapshot"]["rounds"][0]["answers"] == {"value": "good"}
    assert len(planned["plan_hash"]) == 64
    assert execution.spawn_counts == {"planner": 1}
    assert "read-only Grill" in execution.prompts[0][1]
    assert clean(workspace)


async def test_approval_starts_exactly_one_implementation_run(tmp_path, factory_yaml):
    workspace, execution, planning, launched = setup(
        tmp_path, factory_yaml, [grill_ready()], implement=True
    )
    planned = await planning.start("Plan the answer file", request_id="plan-2")
    assert planned["status"] == "planned", planned["error"]
    with pytest.raises(ValueError, match="plan changed"):
        await planning.approve(planned["session_id"], "0" * 64)

    approved = await planning.approve(planned["session_id"], planned["plan_hash"])
    implementation = approved["implementation"]
    assert implementation["status"] == "ready_for_review", implementation["error"]
    assert (workspace / "answer.txt").read_text() == "good"
    request = next(
        prompt
        for role, prompt in execution.prompts
        if role == "planner" and "approved plan" in prompt
    )
    assert planned["plan_hash"] in request
    repeated = await planning.approve(planned["session_id"], planned["plan_hash"])
    assert repeated["handoff"] == approved["handoff"]
    assert launched.count(implementation["run_id"]) == 1
    assert execution.spawn_counts["builder"] == 1


async def test_revision_creates_new_snapshot_without_changing_the_approved_one(
    tmp_path, factory_yaml
):
    _, _, planning, _ = setup(
        tmp_path,
        factory_yaml,
        [grill_ready(), grill_questions(affected=["Answer value"]), grill_ready("Write fine")],
    )
    first = await planning.start("Plan the answer file", request_id="plan-3")
    snapshot = first["snapshot"]
    with pytest.raises(ValueError, match="feedback"):
        await planning.revise(first["session_id"], " ")
    revised = await planning.revise(first["session_id"], "Revisit the answer value")
    assert revised["revision"] == 2
    assert revised["status"] == "waiting_for_input"
    assert revised["rounds"][0]["envelope"]["affected"] == ["Answer value"]
    with pytest.raises(ValueError, match="before revising"):
        await planning.revise(first["session_id"], "again")
    with pytest.raises(ValueError, match="earlier plan revision"):
        await planning.answer(first["session_id"], first["run_id"] + ":grill:1", {"value": "x"})
    second = await planning.answer(
        revised["session_id"], revised["pending"]["interaction_id"], {"value": "fine"}
    )
    assert second["status"] == "planned"
    assert second["snapshot"]["previous_run_id"] == first["run_id"]
    assert second["plan_hash"] != first["plan_hash"]
    assert planning._snapshot(first["run_id"]) == snapshot
    with pytest.raises(ValueError, match="plan changed"):
        await planning.approve(first["session_id"], first["plan_hash"])
    assert [item["revision"] for item in second["history"]] == [1, 2]
    assert [item["session_id"] for item in await planning.sessions()] == [first["session_id"]]


async def test_waiting_planning_run_does_not_reserve_the_workspace(tmp_path, factory_yaml):
    workspace, execution, planning, _ = setup(
        tmp_path, factory_yaml, [grill_questions()], implement=True
    )
    waiting = await planning.start("Plan the answer file", request_id="plan-4")
    assert waiting["status"] == "waiting_for_input"

    async def launch(run_id: str) -> None:
        await (await control.engine(run_id)).resume(run_id)

    control = FactoryControl(
        workspace, storage_root=tmp_path / "controller", execution=execution, launch=launch
    )
    result = await control.start("Write the answer", request_id="impl-4")
    assert result["status"] == "ready_for_review", result.get("error")


async def test_python_planning_export_preserves_waiting_and_exact_snapshot(tmp_path, factory_yaml):
    import hashlib

    from software_factory.planning_export import export_planning

    _, _, planning, _ = setup(tmp_path, factory_yaml, [grill_questions(), grill_ready()])
    waiting = await planning.start("Plan the answer file", request_id="export-session")
    archive = await export_planning(planning)
    revision = archive["sessions"][0]["revisions"][0]
    assert revision["status"] == "waiting_for_input"
    assert revision["rounds"][0]["interaction_id"] == waiting["pending"]["interaction_id"]
    planned = await planning.answer(
        waiting["session_id"], waiting["pending"]["interaction_id"], {"value": "good"}
    )
    archive = await export_planning(planning)
    revision = archive["sessions"][0]["revisions"][0]
    assert hashlib.sha256(revision["snapshot_content"].encode()).hexdigest() == planned["plan_hash"]
    assert revision["rounds"][0]["answers"] == {"value": "good"}


async def test_export_includes_failed_bootstrap_without_document_artifacts(tmp_path, factory_yaml):
    from software_factory.planning_export import export_planning

    workspace, _, _, _ = setup(tmp_path, factory_yaml, [])
    nested = workspace / "nested"
    (nested / ".factory").mkdir(parents=True)
    (nested / ".factory/factory.yaml").write_text(factory_yaml)

    async def launch(run_id):
        await (await planning.base.engine(run_id)).resume(run_id)

    planning = PlanningControl(
        nested,
        storage_root=tmp_path / "failed-controller",
        execution=FakeBB(nested, {}),
        launch=launch,
    )
    failed = await planning.start("Plan from a non-root checkout", request_id="failed-bootstrap")
    assert failed["status"] == "failed"
    revision = (await export_planning(planning))["sessions"][0]["revisions"][0]
    assert revision["status"] == "failed" and revision["documents"] == {}
    assert revision["source_record"]["error"] == failed["error"]


async def test_export_retains_historical_implementation_link_and_report(tmp_path, factory_yaml):
    from software_factory.planning_export import export_planning

    _, _, planning, _ = setup(tmp_path, factory_yaml, [grill_ready()], implement=True)
    planned = await planning.start("Plan the answer file", request_id="export-handoff")
    approved = await planning.approve(planned["session_id"], planned["plan_hash"])
    implementation = approved["implementation"]
    original_report = Path(implementation["final_report_path"]).read_text()
    revision = (await export_planning(planning))["sessions"][0]["revisions"][0]
    assert revision["legacy_handoff"]["implementation_run_id"] == implementation["run_id"]
    assert revision["legacy_implementation"]["report"] == original_report
    assert revision["legacy_implementation"]["pull_request"] == implementation["pull_request"]


@pytest.mark.parametrize(
    "controller_state,operations,error",
    [
        ("running", [], "reconcile the original controller"),
        ("unavailable", [], "reconcile the original controller"),
        ("recovery_needed", [], "reconcile the original controller"),
        ("idle", [{"state": "dispatching"}], "reconcile worker operations"),
        ("idle", [{"state": "prepared"}], "reconcile worker operations"),
    ],
)
async def test_export_rejects_unsettled_linked_implementation(
    tmp_path, factory_yaml, monkeypatch, controller_state, operations, error
):
    from software_factory.planning_export import export_planning

    _, _, planning, _ = setup(tmp_path, factory_yaml, [grill_ready()], implement=True)
    planned = await planning.start("Plan the answer file", request_id="unsettled-export")
    approved = await planning.approve(planned["session_id"], planned["plan_hash"])
    implementation_id = approved["implementation"]["run_id"]
    original_status = planning.base.status

    async def status(run_id, **kwargs):
        result = await original_status(run_id, **kwargs)
        if run_id == implementation_id:
            assert result["status"] == "ready_for_review"
            result.update(controller_state=controller_state, operations=operations)
        return result

    monkeypatch.setattr(planning.base, "status", status)
    with pytest.raises(ValueError, match=error):
        await export_planning(planning)
