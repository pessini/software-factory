from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from langgraph.errors import NodeCancelledError

from software_factory.bb.client import BBError
from software_factory.config import ModelChoice, Selection
from software_factory.engine import FactoryEngine
from tests.conftest import FakeBB, init_repository
from tests.integration.test_factory_engine import (
    build_output,
    context,
    plan_output,
    review_output,
    write_answer,
)


def focused_output(role: str) -> str:
    review = json.loads(review_output())
    return json.dumps(
        {
            "version": 1,
            "reviewer": role,
            "content_version": review["content_version"],
            "summary": "Inspected the change within the assigned scope",
            "findings": [],
            "criteria": review["criteria"] if role == "request" else [],
        }
    )


def judge_output() -> str:
    return json.dumps({**json.loads(review_output()), "assessments": []})


def scripts() -> dict:
    return {
        "planner": [{"output": plan_output()}],
        "builder": [{"output": build_output(), "action": write_answer("good")}],
        "request_reviewer": [{"output": focused_output("request")}],
        "code_reviewer": [{"output": focused_output("code")}],
        "security_reviewer": [{"output": focused_output("security")}],
        "judge": [{"output": judge_output()}],
    }


async def test_handoff_requires_three_separate_reviews_and_a_judge(tmp_path, factory_yaml):
    init_repository(tmp_path, factory_yaml + "\nreview_mode: focused\n", "raise SystemExit(0)\n")
    bb = FakeBB(tmp_path, scripts())
    engine = FactoryEngine(workspace=tmp_path, context=context(tmp_path), execution=bb)
    result = await engine.run("Write the answer")
    assert result["status"] == "ready_for_review", result.get("error")
    handoff = result["handoff"]
    assert set(handoff["reviews"]) == {"request", "code", "security"}
    for role, review in handoff["reviews"].items():
        assert review["reviewer"] == role
        assert review["content_version"] == result["content_version"]
        saved = Path(result["review_path"]).parent / f"{role}-01.json"
        assert json.loads(saved.read_text()) == review
    assert handoff["judge"]["approved"] is True
    for step in ("request_review", "code_review", "security_review", "judge"):
        assert result["execution_summary"]["steps"][step]["dispatch_count"] == 1
    assert {role for role, _ in bb.prompts} == set(scripts())


@pytest.mark.parametrize("role", ["code_reviewer", "judge"])
async def test_invalid_review_can_be_corrected_once_with_the_same_settings(
    tmp_path, factory_yaml, role
):
    init_repository(tmp_path, factory_yaml + "\nreview_mode: focused\n", "raise SystemExit(0)\n")
    responses = scripts()
    responses[role].insert(0, {"output": "invalid JSON"})
    bb = FakeBB(tmp_path, responses)
    engine = FactoryEngine(workspace=tmp_path, context=context(tmp_path), execution=bb)
    result = await engine.run("Write the answer")
    assert result["status"] == "ready_for_review", result.get("error")
    step = "code_review" if role == "code_reviewer" else "judge"
    assert result["execution_summary"]["steps"][step]["dispatch_count"] == 2


def with_findings(output: str, *findings: dict) -> str:
    return json.dumps({**json.loads(output), "findings": list(findings)})


def assessment(role: str, disposition: str, target: str | None = None) -> dict:
    return {
        "reviewer": role,
        "finding_id": "finding-001",
        "disposition": disposition,
        "reason": "Verified answer.txt against the request and the supplied check evidence",
        "evidence_refs": ["source:answer.txt"],
        "retained_finding_id": target,
    }


async def test_judge_removes_unsupported_claims_and_merges_duplicates_before_revision(
    tmp_path, factory_yaml
):
    init_repository(tmp_path, factory_yaml + "\nreview_mode: focused\n", "raise SystemExit(0)\n")
    responses = scripts()
    finding = json.loads(review_output(False))["findings"][0]
    for role in ("request", "code", "security"):
        reported = (
            {**finding, "category": "security", "title": "Unsupported data leak claim"}
            if role == "security"
            else finding
        )
        responses[f"{role}_reviewer"] = [
            {"output": with_findings(focused_output(role), reported)},
            {"output": focused_output(role)},
        ]
    rejected = {
        **json.loads(review_output(False)),
        "findings": [{**finding, "id": "answer-defect"}],
        "assessments": [
            assessment("request", "retained", "answer-defect"),
            assessment("code", "duplicate", "answer-defect"),
            assessment("security", "unsupported"),
        ],
    }
    responses["judge"] = [{"output": json.dumps(rejected)}, {"output": judge_output()}]
    responses["builder"].append({"output": build_output(), "action": write_answer("good")})
    bb = FakeBB(tmp_path, responses)
    result = await FactoryEngine(workspace=tmp_path, context=context(tmp_path), execution=bb).run(
        "Write the answer"
    )
    assert result["status"] == "ready_for_review", result.get("error")
    assert result["test_attempt_count"] == 2
    assert result["review_revision_count"] == 1
    assert set(result["handoff"]["findings"]) == {"answer-defect"}
    assert result["handoff"]["findings"]["answer-defect"]["status"] == "fixed"
    revision = next(
        prompt for role, prompt in bb.prompts if role == "builder" and "blocking_findings" in prompt
    )
    assert "answer-defect" in revision
    assert "Unsupported data leak claim" not in revision
    assert "finding-001" not in revision


async def test_judge_can_accept_when_only_unsupported_findings_remain(tmp_path, factory_yaml):
    init_repository(tmp_path, factory_yaml + "\nreview_mode: focused\n", "raise SystemExit(0)\n")
    responses = scripts()
    finding = {**json.loads(review_output(False))["findings"][0], "severity": "critical"}
    responses["code_reviewer"] = [{"output": with_findings(focused_output("code"), finding)}]
    responses["judge"] = [
        {
            "output": json.dumps(
                {
                    **json.loads(judge_output()),
                    "assessments": [assessment("code", "unsupported")],
                }
            )
        }
    ]
    result = await FactoryEngine(
        workspace=tmp_path, context=context(tmp_path), execution=FakeBB(tmp_path, responses)
    ).run("Write the answer")
    assert result["status"] == "ready_for_review", result.get("error")
    assert not result["handoff"]["findings"]
    assert result["review_revision_count"] == 0
    assert result["handoff"]["judge"]["assessments"][0]["disposition"] == "unsupported"


@pytest.mark.parametrize(
    "failure", ["failed_provider", "invalid_output", "wrong_role", "stale_version"]
)
async def test_a_partial_required_review_cannot_supply_approval(tmp_path, factory_yaml, failure):
    init_repository(tmp_path, factory_yaml + "\nreview_mode: focused\n", "raise SystemExit(0)\n")
    responses = scripts()
    invalid = "invalid JSON"
    if failure == "wrong_role":
        invalid = focused_output("security")
    elif failure == "stale_version":
        invalid = focused_output("code").replace("__CONTENT_VERSION__", "0" * 64)

    def fail(_):
        raise BBError("required provider failed")

    responses["code_reviewer"] = [
        {"output": invalid, **({"action": fail} if failure == "failed_provider" else {})},
        {"output": invalid},
    ]
    bb = FakeBB(tmp_path, responses)
    result = await FactoryEngine(workspace=tmp_path, context=context(tmp_path), execution=bb).run(
        "Write the answer"
    )
    assert result["status"] == ("recovery_needed" if failure == "failed_provider" else "blocked")
    assert not result["review_approved"]
    assert not result.get("handoff")
    assert "judge" not in bb.spawn_counts
    root = Path(result["final_report_path"]).parent
    assert (root / "review/request-01.json").is_file()


@pytest.mark.parametrize(
    "interruption", ["review_saved", "judge_dispatched", "correction_dispatched"]
)
async def test_restart_keeps_saved_reviews_and_does_not_repeat_provider_dispatch(
    tmp_path, factory_yaml, interruption
):
    init_repository(tmp_path, factory_yaml + "\nreview_mode: focused\n", "raise SystemExit(0)\n")
    responses = scripts()
    if interruption == "correction_dispatched":
        responses["judge"].insert(0, {"output": "invalid JSON"})

    class InterruptedBB(FakeBB):
        interrupted = False

        async def start_worker(self, role, prompt, agent, spawn_key):
            thread_id = await super().start_worker(role, prompt, agent, spawn_key)
            if role == "judge" and interruption == "judge_dispatched" and not self.interrupted:
                self.interrupted = True
                raise asyncio.CancelledError()
            return thread_id

        async def continue_worker(self, thread_id, message):
            result = await super().continue_worker(thread_id, message)
            if interruption == "correction_dispatched" and not self.interrupted:
                self.interrupted = True
                raise asyncio.CancelledError()
            return result

    bb = InterruptedBB(tmp_path, responses)

    async def interrupt(event):
        if (
            event.node == "code_review"
            and event.status == "completed"
            and interruption == "review_saved"
            and not bb.interrupted
        ):
            bb.interrupted = True
            raise asyncio.CancelledError()

    engine = FactoryEngine(
        workspace=tmp_path, context=context(tmp_path), execution=bb, event_sink=interrupt
    )
    selection = Selection(steps={"judge": ModelChoice(model="model-b", reasoning_level="low")})
    with pytest.raises(NodeCancelledError):
        await engine.run("Write the answer", "fct_focused_restart", selection=selection)
    result = await FactoryEngine(
        workspace=tmp_path, context=context(tmp_path), execution=bb
    ).resume("fct_focused_restart")
    assert result["status"] == "ready_for_review", result.get("error")
    assert bb.spawn_counts == {role: 1 for role in scripts()}
    assert result["execution_summary"]["steps"]["judge"]["requested"]["model"] == "model-b"
    assert result["execution_summary"]["steps"]["judge"]["dispatch_count"] == (
        2 if interruption == "correction_dispatched" else 1
    )


@pytest.mark.parametrize("role", ["code_reviewer", "judge"])
async def test_cancellation_stops_the_active_review_and_never_finalizes(
    tmp_path, factory_yaml, role
):
    init_repository(tmp_path, factory_yaml + "\nreview_mode: focused\n", "raise SystemExit(0)\n")
    active = asyncio.Event()

    class WaitingBB(FakeBB):
        async def wait(self, thread_id):
            if self.thread_roles[thread_id] == role:
                active.set()
                await asyncio.Event().wait()
            await super().wait(thread_id)

    bb = WaitingBB(tmp_path, scripts())
    engine = FactoryEngine(workspace=tmp_path, context=context(tmp_path), execution=bb)
    task = asyncio.create_task(engine.run("Write the answer", "fct_cancel_review"))
    try:
        await asyncio.wait_for(active.wait(), 15)
        await engine.cancel_external("fct_cancel_review")
        result = await asyncio.wait_for(task, 15)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert result["status"] == "cancelled"
    assert not result["review_approved"]
    assert not result.get("handoff")
    assert f"thread-{role}" in bb.stopped
    assert (tmp_path / "answer.txt").read_text() == "good"


async def test_a_file_change_between_reviews_stops_the_next_reviewer(tmp_path, factory_yaml):
    init_repository(tmp_path, factory_yaml + "\nreview_mode: focused\n", "raise SystemExit(0)\n")

    async def edit_between_reviews(event):
        if event.node == "request_review" and event.status == "completed":
            (tmp_path / "answer.txt").write_text("later user edit")

    bb = FakeBB(tmp_path, scripts())
    engine = FactoryEngine(
        workspace=tmp_path, context=context(tmp_path), execution=bb, event_sink=edit_between_reviews
    )
    result = await engine.run("Write the answer")
    assert result["status"] == "blocked"
    assert "stale evidence" in result["error"]
    assert "code_reviewer" not in bb.spawn_counts
    assert (tmp_path / "answer.txt").read_text() == "later user edit"


@pytest.mark.parametrize("interruption", ["revision_dispatched", "revision_saved"])
async def test_restart_recovers_builder_revision_edits_before_new_checks(
    tmp_path, factory_yaml, interruption
):
    init_repository(tmp_path, factory_yaml + "\nreview_mode: focused\n", "raise SystemExit(0)\n")
    responses = scripts()
    responses["builder"][0]["action"] = write_answer("bad")
    responses["builder"].append({"output": build_output(), "action": write_answer("good")})
    finding = json.loads(review_output(False))["findings"][0]
    responses["code_reviewer"].insert(0, {"output": with_findings(focused_output("code"), finding)})
    for role in ("request_reviewer", "security_reviewer"):
        responses[role] *= 2
    responses["judge"].insert(
        0,
        {
            "output": json.dumps(
                {
                    **json.loads(review_output(False)),
                    "assessments": [assessment("code", "retained", "finding-001")],
                }
            )
        },
    )

    class InterruptedBB(FakeBB):
        interrupted = False

        async def continue_worker(self, thread_id, message):
            result = await super().continue_worker(thread_id, message)
            if (
                self.thread_roles[thread_id] == "builder"
                and interruption == "revision_dispatched"
                and not self.interrupted
            ):
                self.interrupted = True
                raise asyncio.CancelledError()
            return result

    bb = InterruptedBB(tmp_path, responses)

    async def interrupt(event):
        if (
            event.node == "revise"
            and event.status == "completed"
            and interruption == "revision_saved"
            and not bb.interrupted
        ):
            bb.interrupted = True
            raise asyncio.CancelledError()

    engine = FactoryEngine(
        workspace=tmp_path, context=context(tmp_path), execution=bb, event_sink=interrupt
    )
    with pytest.raises(NodeCancelledError):
        await engine.run("Write the answer", "fct_revision_restart")
    assert (tmp_path / "answer.txt").read_text() == "good"
    result = await FactoryEngine(
        workspace=tmp_path, context=context(tmp_path), execution=bb
    ).resume("fct_revision_restart")
    assert result["status"] == "ready_for_review", result.get("error")
    assert result["test_attempt_count"] == 2
    assert result["execution_summary"]["steps"]["revision"]["dispatch_count"] == 1
    assert result["handoff"]["findings"]["finding-001"]["status"] == "fixed"


@pytest.mark.parametrize(
    "fault",
    [
        "omitted_finding",
        "duplicate_assessment",
        "invalid_reference",
        "missing_target",
        "missing_criteria",
        "unexplained_blocker",
        "approval_with_blocker",
    ],
)
async def test_an_invalid_judge_decision_cannot_approve_or_trigger_revision(
    tmp_path, factory_yaml, fault
):
    init_repository(tmp_path, factory_yaml + "\nreview_mode: focused\n", "raise SystemExit(0)\n")
    responses = scripts()
    finding = json.loads(review_output(False))["findings"][0]
    responses["code_reviewer"] = [{"output": with_findings(focused_output("code"), finding)}]
    judge = {
        **json.loads(review_output(False)),
        "assessments": [assessment("code", "retained", "finding-001")],
    }
    if fault == "omitted_finding":
        judge = json.loads(judge_output())
    elif fault == "duplicate_assessment":
        judge["assessments"] *= 2
    elif fault == "invalid_reference":
        judge["assessments"][0]["evidence_refs"] = ["source:invented.py"]
    elif fault == "missing_target":
        judge["assessments"][0]["retained_finding_id"] = "missing"
    elif fault == "missing_criteria":
        judge["criteria"] = []
    elif fault == "unexplained_blocker":
        judge["assessments"][0]["reason"] = " "
    elif fault == "approval_with_blocker":
        judge["approved"] = True
    responses["judge"] = [{"output": json.dumps(judge)}] * 2
    bb = FakeBB(tmp_path, responses)
    result = await FactoryEngine(workspace=tmp_path, context=context(tmp_path), execution=bb).run(
        "Write the answer"
    )
    assert result["status"] == "blocked"
    assert not result["review_approved"]
    assert result["review_revision_count"] == 0
    assert not result.get("handoff")


@pytest.mark.parametrize(
    "role", ["request_reviewer", "code_reviewer", "security_reviewer", "judge"]
)
async def test_review_roles_cannot_edit_implementation_files(tmp_path, factory_yaml, role):
    init_repository(tmp_path, factory_yaml + "\nreview_mode: focused\n", "raise SystemExit(0)\n")
    responses = scripts()
    responses[role][0]["action"] = write_answer("unexpected edit")
    result = await FactoryEngine(
        workspace=tmp_path, context=context(tmp_path), execution=FakeBB(tmp_path, responses)
    ).run("Write the answer")
    assert result["status"] == "blocked"
    assert "read-only" in result["error"]
    assert not result["test_passed"]
    assert not result["review_approved"]
    assert (tmp_path / "answer.txt").read_text() == "unexpected edit"


@pytest.mark.parametrize("phase", ["judge", "finalize"])
async def test_missing_required_review_evidence_blocks_judgment_and_finalization(
    tmp_path, factory_yaml, phase
):
    init_repository(tmp_path, factory_yaml + "\nreview_mode: focused\n", "raise SystemExit(0)\n")

    async def remove_review(event):
        if event.node == phase and event.status == "started":
            (
                context(tmp_path).thread_storage
                / "factory/artifacts"
                / event.run_id
                / "review/security-01.json"
            ).unlink()

    result = await FactoryEngine(
        workspace=tmp_path,
        context=context(tmp_path),
        execution=FakeBB(tmp_path, scripts()),
        event_sink=remove_review,
    ).run("Write the answer")
    assert result["status"] == "blocked"
    assert not result["review_approved"]
    assert not result.get("handoff")


@pytest.mark.parametrize("phase", ["judge", "revise"])
async def test_stale_judge_rejection_cannot_send_builder_over_later_user_edits(
    tmp_path, factory_yaml, phase
):
    init_repository(tmp_path, factory_yaml + "\nreview_mode: focused\n", "raise SystemExit(0)\n")
    responses = scripts()
    finding = json.loads(review_output(False))["findings"][0]
    responses["code_reviewer"].insert(0, {"output": with_findings(focused_output("code"), finding)})
    for role in ("request_reviewer", "security_reviewer"):
        responses[role] *= 2
    responses["judge"].insert(
        0,
        {
            "output": json.dumps(
                {
                    **json.loads(review_output(False)),
                    "assessments": [assessment("code", "retained", "finding-001")],
                }
            )
        },
    )
    responses["builder"].append(
        {"output": build_output(), "action": write_answer("overwritten by revision")}
    )

    async def later_edit(event):
        if (
            event.node == phase
            and event.status == ("completed" if phase == "judge" else "started")
            and event.attempt == 1
        ):
            (tmp_path / "answer.txt").write_text("later user edit")

    result = await FactoryEngine(
        workspace=tmp_path,
        context=context(tmp_path),
        execution=FakeBB(tmp_path, responses),
        event_sink=later_edit,
    ).run("Write the answer")
    assert result["status"] == "blocked"
    assert "stale evidence" in result["error"]
    assert not result["test_passed"]
    assert (tmp_path / "answer.txt").read_text() == "later user edit"
