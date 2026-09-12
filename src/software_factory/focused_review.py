"""Run required focused reviews through the shared operation and evidence controls."""

from __future__ import annotations

from functools import partial
from typing import Any

from software_factory.contracts.models import FocusedReviewEnvelope, JudgeEnvelope
from software_factory.prompts import focused_review_prompt, judge_prompt
from software_factory.runtime import FactoryRuntime


async def run_focused_review(
    runtime: FactoryRuntime, request: str, bundle: dict[str, Any], attempt: int
) -> tuple[str, JudgeEnvelope]:
    history = runtime.evidence.history(attempt)
    snapshot = runtime.evidence.read(bundle["repository_content"])
    roles = runtime.evidence.required_reviewers(bundle)
    if runtime.config.review_mode == "risk_based":
        selection_path = f"review/selection-{attempt:02d}.json"
        selected = {"attempt": attempt, **bundle["review_selection"]}
        if runtime.artifacts.path(selection_path).exists():
            if runtime.evidence.read(selection_path) != selected:
                raise ValueError("review round selection changed during restart")
        else:
            runtime.artifacts.write_json(selection_path, selected)
            await runtime.emit(
                "review", "info", "Review level selected", attempt=attempt, metadata=selected
            )
    for role in roles:
        step = f"{role}_review"
        child_role = f"{role}_reviewer"
        await runtime.check_cancelled()
        await runtime.evidence.require_current(snapshot)
        await runtime.emit(
            step, "started", f"{role.title()} Reviewer checking changes", attempt=attempt
        )
        async with runtime.worktree.read_only(f"{role.title()} Reviewer") as check_workspace:
            thread_id, output = await runtime.get_or_spawn(
                child_role,
                focused_review_prompt(
                    runtime.run_id, role, request, runtime.artifacts.root, bundle, history
                ),
                runtime.config.agents.for_role(child_role),
                step=step,
                attempt=attempt,
                tell_existing=await runtime.store.child(runtime.run_id, child_role) is not None,
            )
            review = await runtime.parse_with_correction(
                child_role,
                thread_id,
                output,
                FocusedReviewEnvelope,
                check_workspace=check_workspace,
                step=step,
                attempt=attempt,
                validate=partial(
                    runtime.evidence.validate_focused_review, role=role, bundle=bundle
                ),
            )
        runtime.artifacts.write_json(f"review/{role}-{attempt:02d}.json", review)
        await runtime.emit(step, "completed", f"{role.title()} review saved", attempt=attempt)
    reviews = runtime.evidence.focused_reviews(bundle, attempt)
    await runtime.check_cancelled()
    await runtime.evidence.require_current(snapshot)
    await runtime.emit("judge", "started", "Judge verifying review findings", attempt=attempt)
    async with runtime.worktree.read_only("Judge") as check_workspace:
        thread_id, output = await runtime.get_or_spawn(
            "judge",
            judge_prompt(
                runtime.run_id,
                request,
                runtime.artifacts.root,
                bundle,
                history,
                {role: review.model_dump(mode="json") for role, review in reviews.items()},
            ),
            runtime.config.agents.for_role("judge"),
            step="judge",
            attempt=attempt,
            tell_existing=attempt > 1,
        )
        judge = await runtime.parse_with_correction(
            "judge",
            thread_id,
            output,
            JudgeEnvelope,
            check_workspace=check_workspace,
            step="judge",
            attempt=attempt,
            validate=lambda value: runtime.evidence.validate_judge(value, bundle, attempt),
        )
    await runtime.evidence.require_current(snapshot)
    runtime.artifacts.write_json(f"review/judge-{attempt:02d}.json", judge)
    await runtime.emit("judge", "completed", "Judge decision saved", attempt=attempt)
    return thread_id, judge
