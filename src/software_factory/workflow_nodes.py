from __future__ import annotations

import json
from typing import Any, Literal

from software_factory.contracts import BuildEnvelope, PlanEnvelope, ReviewEnvelope
from software_factory.git import changed_files, repository_root
from software_factory.prompts import (
    builder_prompt,
    planner_prompt,
    repair_prompt,
    reviewer_prompt,
    revision_prompt,
)
from software_factory.reporting import final_report, plan_markdown, review_findings, run_summary
from software_factory.runtime import FactoryCancelled, FactoryRuntime
from software_factory.state import FactoryState
from software_factory.worktree_policy import WorktreePolicy


class WorkflowNodes:
    """Execute workflow phases while graph.py owns only routing and topology."""

    def __init__(self, runtime: FactoryRuntime) -> None:
        self.runtime = runtime
        self.worktree = WorktreePolicy(
            runtime.workspace, runtime.config.safety.protected_paths
        )

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
            runtime.artifacts.write_text("request.md", state["request"].rstrip() + "\n")
            await runtime.store.set_status(runtime.run_id, "running")
            await runtime.emit("bootstrap", "completed", "repository and policy validated")
            return {"status": "running", "baseline_files": baseline, "error": None}
        except Exception as exc:
            await runtime.emit("bootstrap", "failed", str(exc))
            return {"status": "failed", "error": str(exc)}

    async def plan(self, state: FactoryState) -> FactoryState:
        runtime = self.runtime
        await runtime.emit("plan", "started", "Planner analyzing request")
        try:
            async with self.worktree.read_only("Planner"):
                thread_id, output = await runtime.get_or_spawn(
                    "planner",
                    planner_prompt(runtime.run_id, state["request"], runtime.workspace),
                    runtime.config.agents.planner,
                )
                envelope = await runtime.parse_with_correction(
                    "planner", thread_id, output, PlanEnvelope
                )
                if envelope.status == "blocked" or not envelope.acceptance_criteria:
                    raise ValueError(envelope.summary or "Planner blocked")
            json_path = runtime.artifacts.write_json("plan.json", envelope)
            markdown_path = runtime.artifacts.write_text("plan.md", plan_markdown(envelope))
            await runtime.emit("plan", "completed", "plan and acceptance criteria validated")
            return {
                "planner_thread_id": thread_id,
                "plan_json_path": str(json_path.relative_to(runtime.workspace)),
                "plan_markdown_path": str(markdown_path.relative_to(runtime.workspace)),
                "error": None,
            }
        except FactoryCancelled as exc:
            return await self._cancelled("plan", exc)
        except Exception as exc:
            await runtime.emit("plan", "failed", str(exc))
            return {"status": "blocked", "error": str(exc)}

    async def build(self, state: FactoryState) -> FactoryState:
        runtime = self.runtime
        await runtime.emit("build", "started", "Builder implementing validated plan")
        try:
            plan_path = runtime.workspace / state["plan_markdown_path"]
            thread_id, envelope, actual = await self._builder_handoff(
                builder_prompt(runtime.run_id, state["request"], plan_path)
            )
            report = runtime.artifacts.write_json("build-report.json", envelope)
            runtime.artifacts.write_json("changes.json", {"changed_files": actual})
            await runtime.emit("build", "completed", f"Builder changed {len(actual)} file(s)")
            return {
                "builder_thread_id": thread_id,
                "build_report_path": str(report.relative_to(runtime.workspace)),
                "changed_files": actual,
                "error": None,
            }
        except FactoryCancelled as exc:
            return await self._cancelled("build", exc)
        except Exception as exc:
            await runtime.emit("build", "failed", str(exc))
            return {"status": "blocked", "error": str(exc)}

    async def test(self, state: FactoryState) -> FactoryState:
        runtime = self.runtime
        attempt = state.get("test_attempt_count", 0) + 1
        await runtime.emit("test", "started", "running trusted quality commands", attempt=attempt)
        results: list[dict[str, Any]] = []
        try:
            for command in runtime.config.quality.commands:
                results.append(await runtime.run_quality_command(command, attempt))
            passed = all(result["exit_code"] == 0 and not result["timed_out"] for result in results)
            payload = {"attempt": attempt, "passed": passed, "commands": results}
            path = runtime.artifacts.write_json(f"tests/attempt-{attempt:02d}.json", payload)
            await runtime.emit(
                "test",
                "completed" if passed else "failed",
                "quality commands passed" if passed else "quality commands failed",
                attempt=attempt,
                metadata={"result_path": str(path.relative_to(runtime.workspace))},
            )
            return {
                "test_attempt_count": attempt,
                "test_passed": passed,
                "test_result_path": str(path.relative_to(runtime.workspace)),
                "error": None if passed else "configured quality commands failed",
            }
        except FactoryCancelled as exc:
            return await self._cancelled("test", exc)
        except Exception as exc:
            await runtime.emit("test", "failed", str(exc), attempt=attempt)
            return {
                "test_attempt_count": attempt,
                "test_passed": False,
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
            result_path = runtime.workspace / state["test_result_path"]
            result = json.loads(result_path.read_text(encoding="utf-8"))
            logs = [item["log_path"] for item in result["commands"]]
            thread_id, envelope, actual = await self._builder_handoff(
                repair_prompt(runtime.run_id, state["test_attempt_count"], logs),
                tell_existing=True,
            )
            runtime.artifacts.write_json(f"build-repair-{repair_count:02d}.json", envelope)
            await runtime.emit(
                "repair", "completed", "Builder repair completed", attempt=repair_count
            )
            return {
                "builder_thread_id": thread_id,
                "test_repair_count": repair_count,
                "changed_files": actual,
                "test_passed": False,
                "error": None,
            }
        except FactoryCancelled as exc:
            return await self._cancelled("repair", exc)
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
            async with self.worktree.read_only("Reviewer"):
                prompt = reviewer_prompt(
                    runtime.run_id,
                    state["request"],
                    runtime.workspace / state["plan_markdown_path"],
                    state["changed_files"],
                    runtime.workspace / state["test_result_path"],
                )
                existing = await runtime.store.child(runtime.run_id, "reviewer")
                thread_id, output = await runtime.get_or_spawn(
                    "reviewer",
                    prompt,
                    runtime.config.agents.reviewer,
                    tell_existing=existing is not None,
                )
                envelope = await runtime.parse_with_correction(
                    "reviewer", thread_id, output, ReviewEnvelope
                )
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
                "review_path": str(path.relative_to(runtime.workspace)),
                "error": None if envelope.approved else review_findings(envelope),
            }
        except FactoryCancelled as exc:
            return await self._cancelled("review", exc)
        except Exception as exc:
            await runtime.emit("review", "failed", str(exc), attempt=attempt)
            return {"review_attempt_count": attempt, "status": "blocked", "error": str(exc)}

    async def revise(self, state: FactoryState) -> FactoryState:
        runtime = self.runtime
        count = state.get("review_revision_count", 0) + 1
        await runtime.emit(
            "revise", "started", "Builder addressing blocking review findings", attempt=count
        )
        try:
            review = ReviewEnvelope.model_validate_json(
                (runtime.workspace / state["review_path"]).read_text(encoding="utf-8")
            )
            thread_id, envelope, actual = await self._builder_handoff(
                revision_prompt(runtime.run_id, review.blocking_findings),
                tell_existing=True,
            )
            runtime.artifacts.write_json(f"build-revision-{count:02d}.json", envelope)
            await runtime.emit("revise", "completed", "review revision completed", attempt=count)
            return {
                "builder_thread_id": thread_id,
                "review_revision_count": count,
                "changed_files": actual,
                "test_passed": False,
                "review_approved": False,
                "error": None,
            }
        except FactoryCancelled as exc:
            return await self._cancelled("revise", exc)
        except Exception as exc:
            await runtime.emit("revise", "failed", str(exc), attempt=count)
            return {"review_revision_count": count, "status": "blocked", "error": str(exc)}

    async def finalize(self, state: FactoryState) -> FactoryState:
        runtime = self.runtime
        report = final_report(state, "ACCEPTED")
        path = runtime.artifacts.write_text("final-report.md", report)
        runtime.artifacts.write_json("run-summary.json", run_summary(state, "accepted"))
        runtime.context.thread_storage.mkdir(parents=True, exist_ok=True)
        storage_report = (
            runtime.context.thread_storage / f"factory-{runtime.run_id}-final-report.md"
        )
        storage_report.write_text(report, encoding="utf-8")
        await runtime.store.set_status(runtime.run_id, "accepted")
        await runtime.emit("finalize", "completed", "factory run accepted")
        return {
            "status": "accepted",
            "final_report_path": str(path.relative_to(runtime.workspace)),
            "error": None,
        }

    async def failed(self, state: FactoryState) -> FactoryState:
        runtime = self.runtime
        current = state.get("status")
        if current == "cancelled":
            outcome = "CANCELLED"
            status: Literal["blocked", "cancelled", "failed"] = "cancelled"
        elif current == "blocked":
            outcome = "BLOCKED"
            status = "blocked"
        else:
            outcome = "FAILED"
            status = "failed"
        report = final_report(state, outcome)
        path = runtime.artifacts.write_text("final-report.md", report)
        runtime.artifacts.write_json("run-summary.json", run_summary(state, status))
        await runtime.store.set_status(runtime.run_id, status, state.get("error"))
        await runtime.emit(
            "failed",
            "cancelled" if status == "cancelled" else "failed",
            state.get("error") or outcome,
        )
        return {"status": status, "final_report_path": str(path.relative_to(runtime.workspace))}

    async def _cancelled(self, node: str, exc: Exception) -> FactoryState:
        runtime = self.runtime
        await runtime.store.set_status(runtime.run_id, "cancelled", str(exc))
        await runtime.emit(node, "cancelled", str(exc))
        return {"status": "cancelled", "error": str(exc)}

    async def _builder_handoff(
        self, prompt: str, *, tell_existing: bool = False
    ) -> tuple[str, BuildEnvelope, list[str]]:
        runtime = self.runtime
        async with self.worktree.protect_builder_changes():
            thread_id, output = await runtime.get_or_spawn(
                "builder",
                prompt,
                runtime.config.agents.builder,
                tell_existing=tell_existing,
            )
            envelope = await runtime.parse_with_correction(
                "builder", thread_id, output, BuildEnvelope
            )
            if envelope.status == "blocked":
                detail = "; ".join(envelope.blockers) or envelope.summary
                raise ValueError(detail)
            actual = await self.worktree.validate_builder_report(envelope.changed_files)
        return thread_id, envelope, actual
