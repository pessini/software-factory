from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from software_factory.artifacts import ArtifactIntegrityError
from software_factory.bb.client import RecoveryNeeded, WaitingForInput
from software_factory.contracts import BuildEnvelope, PlanEnvelope, ReviewEnvelope
from software_factory.contracts.models import JudgeEnvelope
from software_factory.evidence import check_passed, compare_check
from software_factory.focused_review import run_focused_review
from software_factory.git import capture_snapshot, changed_files, repository_root
from software_factory.interactions import Interaction
from software_factory.prompts import (
    builder_prompt,
    data_block,
    planner_prompt,
    repair_prompt,
    reviewer_prompt,
    revision_prompt,
)
from software_factory.reporting import plan_markdown, review_findings
from software_factory.runtime import FactoryCancelled, FactoryRuntime
from software_factory.state import FINAL_STATUSES, FactoryState
from software_factory.worktree_policy import WorktreePolicyError


class NodeAction(Protocol):
    def __call__(self, state: FactoryState) -> Awaitable[FactoryState]: ...


class WorkflowNodes:
    """Execute workflow phases while graph.py owns only routing and topology."""

    def __init__(self, runtime: FactoryRuntime) -> None:
        self.runtime = runtime
        self.worktree = runtime.worktree

    def guarded(
        self, name: str, action: Callable[[FactoryState], Awaitable[FactoryState]]
    ) -> NodeAction:
        async def execute(state: FactoryState) -> FactoryState:
            if name == "failed":
                return await action(state)
            try:
                await self.runtime.check_cancelled()
                result = await action(state)
                if result.get("status") not in FINAL_STATUSES:
                    await self.runtime.check_cancelled()
                return result
            except FactoryCancelled as exc:
                if name == "finalize":
                    return await self.failed({**state, "status": "cancelled", "error": str(exc)})
                return await self._cancelled(name, exc)

        return execute

    async def bootstrap(self, state: FactoryState) -> FactoryState:
        runtime = self.runtime
        await runtime.emit("bootstrap", "started", "validating repository and policy")
        try:
            root = await repository_root(runtime.workspace)
            if root != runtime.workspace.resolve():
                raise ValueError(f"workspace must be repository root: {root}")
            baseline = await changed_files(runtime.workspace)
            if baseline:
                raise ValueError(f"worktree must be clean before a run: {', '.join(baseline)}")
            snapshot = await capture_snapshot(runtime.workspace)
            runtime.artifacts.write_json(
                "protection.json",
                {
                    "version": 2,
                    "starting_head": snapshot.head,
                    "starting_head_ref": snapshot.head_ref,
                    "starting_refs": snapshot.refs,
                    "workspace": str(runtime.workspace),
                    "starting_files": sorted(snapshot.worktree),
                    "repository_state": await self.worktree.repository_baseline(),
                    "allowed_changes": "Builder: non-ignored application and test files; no Git index or reference changes",
                    "protected_paths": self.worktree.protected_paths,
                    "read_only_scope": "all tracked, untracked and ignored files",
                    "violation_action": "stop and preserve the workspace for inspection",
                },
            )
            runtime.artifacts.write_json("config.json", runtime.config)
            await runtime.bb.check_protection(runtime.artifacts.root)
            await runtime.bb.validate_workspace(runtime.workspace)
            runtime.artifacts.write_text("request.md", state["request"].rstrip() + "\n")
            await runtime.evidence.prepare(state["request"], runtime.config)
            await runtime.store.set_status(runtime.run_id, "running")
            await runtime.emit("bootstrap", "completed", "repository and policy validated")
            return {"status": "running", "baseline_files": baseline, "error": None}
        except RecoveryNeeded, WaitingForInput:
            raise
        except Exception as exc:
            await runtime.emit("bootstrap", "failed", str(exc))
            return {"status": "failed", "error": str(exc)}

    async def baseline(self, state: FactoryState) -> FactoryState:
        runtime = self.runtime
        await runtime.emit(
            "baseline", "started", "checking the starting files before Builder edits"
        )
        try:
            payload = await self._checks("baseline", 1)
            path = runtime.artifacts.write_json("baseline.json", payload)
            await runtime.emit(
                "baseline", "completed", "baseline results saved, including existing failures"
            )
            return {"baseline_result_path": str(path), "error": None}
        except RecoveryNeeded, WaitingForInput, FactoryCancelled:
            raise
        except Exception as exc:
            return {"status": "blocked", "error": str(exc)}

    async def plan(self, state: FactoryState) -> FactoryState:
        runtime = self.runtime
        await runtime.emit("plan", "started", "Planner analyzing request")
        try:
            async with self.worktree.read_only("Planner") as check_workspace:
                thread_id, output = await runtime.get_or_spawn(
                    "planner",
                    planner_prompt(
                        runtime.run_id, state["request"], runtime.workspace, runtime.artifacts.root
                    ),
                    runtime.config.agents.planner,
                    step="plan",
                )
                envelope = await runtime.parse_with_correction(
                    "planner",
                    thread_id,
                    output,
                    PlanEnvelope,
                    check_workspace=check_workspace,
                    step="plan",
                )
                round_number = 1
                while envelope.questions:
                    interaction = await runtime.store.save_interaction(
                        Interaction(
                            interaction_id=f"{runtime.run_id}:plan:{round_number}",
                            run_id=runtime.run_id,
                            source="planner",
                            questions=envelope.questions,
                        )
                    )
                    if interaction.answers is None:
                        await runtime.emit("plan", "waiting", interaction.display())
                        raise WaitingForInput(interaction.display())
                    await runtime.check_cancelled()
                    round_number += 1
                    thread_id, output = await runtime.get_or_spawn(
                        "planner",
                        "Continue the same plan using these saved answers. Return the PlanEnvelope JSON.\n"
                        + data_block(
                            {
                                "questions": [q.model_dump() for q in interaction.questions],
                                "answers": interaction.answers,
                            }
                        ),
                        runtime.config.agents.planner,
                        step="plan",
                        attempt=round_number,
                        operation_kind="clarification",
                        tell_existing=True,
                    )
                    await runtime.store.deliver_interaction(
                        runtime.run_id, interaction.interaction_id
                    )
                    envelope = await runtime.parse_with_correction(
                        "planner",
                        thread_id,
                        output,
                        PlanEnvelope,
                        check_workspace=check_workspace,
                        step="plan",
                        attempt=round_number,
                    )
                if envelope.status == "blocked" or not envelope.acceptance_criteria:
                    raise ValueError(envelope.summary or "Planner blocked")
            json_path = runtime.artifacts.write_json("plan.json", envelope)
            markdown_path = runtime.artifacts.write_text("plan.md", plan_markdown(envelope))
            await runtime.emit("plan", "completed", "plan and acceptance criteria validated")
            return {
                "planner_thread_id": thread_id,
                "plan_json_path": str(json_path),
                "plan_markdown_path": str(markdown_path),
                "error": None,
            }
        except FactoryCancelled as exc:
            return await self._cancelled("plan", exc)
        except RecoveryNeeded, WaitingForInput:
            raise
        except Exception as exc:
            await runtime.emit("plan", "failed", str(exc))
            return {"status": "blocked", "error": str(exc)}

    async def build(self, state: FactoryState) -> FactoryState:
        runtime = self.runtime
        await runtime.emit("build", "started", "Builder implementing validated plan")
        try:
            plan_path = runtime.workspace / state["plan_markdown_path"]
            thread_id, envelope, actual = await self._builder_handoff(
                builder_prompt(runtime.run_id, state["request"], plan_path), step="build"
            )
            report = runtime.artifacts.write_json("build-report.json", envelope)
            runtime.artifacts.write_json("changes.json", {"changed_files": actual})
            await runtime.emit("build", "completed", f"Builder changed {len(actual)} file(s)")
            return {
                "builder_thread_id": thread_id,
                "build_report_path": str(report),
                "changed_files": actual,
                "review_approved": False,
                "test_passed": False,
                "error": None,
            }
        except FactoryCancelled as exc:
            return await self._cancelled("build", exc)
        except RecoveryNeeded, WaitingForInput:
            raise
        except Exception as exc:
            await runtime.emit("build", "failed", str(exc))
            return {"status": "blocked", "error": str(exc)}

    async def test(self, state: FactoryState) -> FactoryState:
        runtime = self.runtime
        attempt = state.get("test_attempt_count", 0) + 1
        await runtime.emit("test", "started", "running trusted quality commands", attempt=attempt)
        try:
            payload = await self._checks("tests", attempt)
            passed = payload["passed"]
            path = runtime.artifacts.write_json(f"tests/attempt-{attempt:02d}.json", payload)
            failures = [item for item in payload["commands"] if not check_passed(item)]
            baseline_only = bool(failures) and all(
                item["comparison"] != "new_failure" for item in failures
            )
            await runtime.emit(
                "test",
                "completed" if passed else "failed",
                "quality commands passed" if passed else "quality commands failed",
                attempt=attempt,
                metadata={"result_path": str(path)},
            )
            return {
                "test_attempt_count": attempt,
                "test_passed": passed,
                "test_result_path": str(path),
                "content_version": payload["content_version"],
                "review_approved": False,
                "status": "blocked" if baseline_only else "running",
                "error": None
                if passed
                else "baseline failures require user action; changed output alone does not establish a new failure"
                if baseline_only
                else "configured quality commands failed",
            }
        except FactoryCancelled as exc:
            return await self._cancelled("test", exc)
        except RecoveryNeeded, WaitingForInput:
            raise
        except Exception as exc:
            await runtime.emit("test", "failed", str(exc), attempt=attempt)
            return {
                "test_attempt_count": attempt,
                "test_passed": False,
                "review_approved": False,
                "status": "blocked",
                "error": str(exc),
            }

    async def repair(self, state: FactoryState) -> FactoryState:
        runtime = self.runtime
        repair_count = state.get("test_repair_count", 0) + 1
        await runtime.emit(
            "repair",
            "started",
            "Builder repairing deterministic test failure",
            attempt=repair_count,
        )
        try:
            result = json.loads(
                runtime.artifacts.read_text(f"tests/attempt-{state['test_attempt_count']:02d}.json")
            )
            logs = [
                item["log_path"]
                for item in result["commands"]
                if item["comparison"] == "new_failure"
            ]
            thread_id, envelope, actual = await self._builder_handoff(
                repair_prompt(
                    runtime.run_id,
                    state["test_attempt_count"],
                    logs,
                    request=state["request"],
                    plan_path=runtime.workspace / state["plan_markdown_path"],
                    changed=state["changed_files"],
                ),
                tell_existing=True,
                step="repair",
                attempt=repair_count,
            )
            report = runtime.artifacts.write_json(f"build-repair-{repair_count:02d}.json", envelope)
            await runtime.emit(
                "repair", "completed", "Builder repair completed", attempt=repair_count
            )
            return {
                "builder_thread_id": thread_id,
                "test_repair_count": repair_count,
                "build_report_path": str(report),
                "changed_files": actual,
                "test_passed": False,
                "review_approved": False,
                "error": None,
            }
        except FactoryCancelled as exc:
            return await self._cancelled("repair", exc)
        except RecoveryNeeded, WaitingForInput:
            raise
        except Exception as exc:
            await runtime.emit("repair", "failed", str(exc), attempt=repair_count)
            return {"test_repair_count": repair_count, "status": "blocked", "error": str(exc)}

    async def review(self, state: FactoryState) -> FactoryState:
        runtime = self.runtime
        attempt = state.get("review_attempt_count", 0) + 1
        await runtime.emit(
            "review", "started", "Reviewer assessing verified changes", attempt=attempt
        )
        try:
            bundle = await runtime.evidence.review_bundle(state["test_attempt_count"])
            envelope: ReviewEnvelope
            if runtime.config.review_mode != "single":
                thread_id, envelope = await run_focused_review(
                    runtime, state["request"], bundle, attempt
                )
            else:
                history = runtime.evidence.history(attempt)
                async with self.worktree.read_only("Reviewer") as check_workspace:
                    prompt = reviewer_prompt(
                        runtime.run_id,
                        state["request"],
                        runtime.workspace / state["plan_markdown_path"],
                        state["changed_files"],
                        runtime.workspace / state["test_result_path"],
                        bundle=bundle,
                        history=history,
                    )
                    existing = await runtime.store.child(runtime.run_id, "reviewer")
                    thread_id, output = await runtime.get_or_spawn(
                        "reviewer",
                        prompt,
                        runtime.config.agents.reviewer,
                        tell_existing=existing is not None,
                        step="review",
                        attempt=attempt,
                    )
                    envelope = await runtime.parse_with_correction(
                        "reviewer",
                        thread_id,
                        output,
                        ReviewEnvelope,
                        check_workspace=check_workspace,
                        step="review",
                        attempt=attempt,
                        validate=lambda review: runtime.evidence.validate_review(
                            review, bundle, attempt
                        ),
                    )
            history = runtime.evidence.validate_review(envelope, bundle, attempt)
            runtime.artifacts.write_json(f"review/findings-{attempt:02d}.json", history)
            path = runtime.artifacts.write_json(f"review/attempt-{attempt:02d}.json", envelope)
            await runtime.emit(
                "review",
                "completed" if envelope.approved else "failed",
                "review approved" if envelope.approved else "review requested revision",
                attempt=attempt,
            )
            return {
                "reviewer_thread_id": thread_id,
                "review_attempt_count": attempt,
                "review_approved": envelope.approved,
                "review_path": str(path),
                "status": "blocked"
                if not envelope.approved and not envelope.blocking_findings
                else "running",
                "error": None if envelope.approved else review_findings(envelope),
            }
        except FactoryCancelled as exc:
            return await self._cancelled("review", exc)
        except RecoveryNeeded, WaitingForInput:
            raise
        except Exception as exc:
            await runtime.emit("review", "failed", str(exc), attempt=attempt)
            return {
                "review_attempt_count": attempt,
                "review_approved": False,
                "test_passed": state.get("test_passed", False)
                and not isinstance(exc, (ArtifactIntegrityError, WorktreePolicyError)),
                "status": "blocked",
                "error": str(exc),
            }

    async def revise(self, state: FactoryState) -> FactoryState:
        runtime = self.runtime
        count = state.get("review_revision_count", 0) + 1
        await runtime.emit(
            "revise", "started", "Builder addressing blocking review findings", attempt=count
        )
        try:
            review_model = (
                JudgeEnvelope if runtime.config.review_mode != "single" else ReviewEnvelope
            )
            review = review_model.model_validate_json(
                runtime.artifacts.read_text(
                    f"review/attempt-{state['review_attempt_count']:02d}.json"
                )
            )

            async def verify_reviewed_content() -> None:
                # A resumed Builder operation can already have changed the files.
                # Check its inputs only before the shared runtime first dispatches it.
                bundle = await runtime.evidence.review_bundle(state["test_attempt_count"])
                if isinstance(review, JudgeEnvelope):
                    runtime.evidence.validate_judge(review, bundle, state["review_attempt_count"])
                else:
                    runtime.evidence.validate_review(review, bundle, state["review_attempt_count"])

            thread_id, envelope, actual = await self._builder_handoff(
                revision_prompt(
                    runtime.run_id,
                    review.blocking_findings,
                    request=state["request"],
                    plan_path=runtime.workspace / state["plan_markdown_path"],
                    changed=state["changed_files"],
                ),
                tell_existing=True,
                step="revise",
                attempt=count,
                before_dispatch=verify_reviewed_content,
            )
            report = runtime.artifacts.write_json(f"build-revision-{count:02d}.json", envelope)
            await runtime.emit("revise", "completed", "review revision completed", attempt=count)
            return {
                "builder_thread_id": thread_id,
                "review_revision_count": count,
                "build_report_path": str(report),
                "changed_files": actual,
                "test_passed": False,
                "review_approved": False,
                "error": None,
            }
        except FactoryCancelled as exc:
            return await self._cancelled("revise", exc)
        except RecoveryNeeded, WaitingForInput:
            raise
        except Exception as exc:
            await runtime.emit("revise", "failed", str(exc), attempt=count)
            return {
                "review_revision_count": count,
                "test_passed": state.get("test_passed", False)
                and not isinstance(exc, (ArtifactIntegrityError, WorktreePolicyError)),
                "review_approved": False,
                "status": "blocked",
                "error": str(exc),
            }

    async def finalize(self, state: FactoryState) -> FactoryState:
        runtime = self.runtime
        await runtime.emit(
            "finalize", "started", "verifying current files against checks and review"
        )
        try:
            await runtime.verify_repository()
            await runtime.verify_handoff(state)
        except (ArtifactIntegrityError, WorktreePolicyError, ValueError, KeyError) as exc:
            return await self.failed(
                {
                    **state,
                    "status": "blocked",
                    "test_passed": False,
                    "review_approved": False,
                    "error": str(exc),
                }
            )
        await runtime.check_cancelled()
        return await runtime.finish({**state, "error": None}, "ready_for_review")

    async def _checks(self, phase: str, attempt: int) -> dict[str, Any]:
        runtime = self.runtime
        snapshot = await runtime.evidence.begin_checks(phase, attempt)
        baseline = runtime.evidence.read("baseline.json")["commands"] if phase == "tests" else []
        results = []
        for index, command in enumerate(runtime.config.quality.commands):
            await runtime.evidence.require_current(snapshot)
            result = await runtime.run_quality_command(
                command, attempt, phase=phase, content_version=snapshot["content_version"]
            )
            await runtime.evidence.require_current(snapshot)
            if baseline:
                result = {**result, "comparison": compare_check(result, baseline[index])}
            results.append(result)
        return {
            "attempt": attempt,
            "passed": all(check_passed(item) for item in results),
            "content_version": snapshot["content_version"],
            "commands": results,
        }

    async def failed(self, state: FactoryState) -> FactoryState:
        current = state.get("status", "failed")
        status = current if current in {"cancelled", "blocked", "failed"} else "failed"
        return await self.runtime.finish(state, status)

    async def _cancelled(self, node: str, exc: Exception) -> FactoryState:
        runtime = self.runtime
        record = await runtime.store.get_run(runtime.run_id)
        target = str(record["stop_target"] or "cancelled") if record else "cancelled"
        await runtime.store.request_stop(runtime.run_id, target, str(exc))
        await runtime.emit(node, "cancelled", str(exc))
        return {"status": "cancelled", "error": str(exc)}

    async def _builder_handoff(
        self,
        prompt: str,
        *,
        tell_existing: bool = False,
        step: str = "build",
        attempt: int = 1,
        before_dispatch: Callable[[], Awaitable[None]] | None = None,
    ) -> tuple[str, BuildEnvelope, list[str]]:
        runtime = self.runtime
        async with self.worktree.protect_builder_changes() as check_workspace:
            thread_id, output = await runtime.get_or_spawn(
                "builder",
                prompt,
                runtime.config.agents.builder,
                tell_existing=tell_existing,
                step=step,
                attempt=attempt,
                before_dispatch=before_dispatch,
            )
            envelope = await runtime.parse_with_correction(
                "builder",
                thread_id,
                output,
                BuildEnvelope,
                check_workspace=check_workspace,
                step=step,
                attempt=attempt,
            )
            if envelope.status == "blocked":
                detail = "; ".join(envelope.blockers) or envelope.summary
                raise ValueError(detail)
            actual = await self.worktree.validate_builder_report(envelope.changed_files)
        return thread_id, envelope, actual
