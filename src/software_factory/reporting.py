from __future__ import annotations

from typing import Any

from software_factory.contracts import PlanEnvelope, ReviewEnvelope
from software_factory.state import FactoryState


def plan_markdown(plan: PlanEnvelope) -> str:
    criteria = "\n".join(f"- {item}" for item in plan.acceptance_criteria)
    steps = "\n".join(f"{index}. {item}" for index, item in enumerate(plan.implementation_steps, 1))
    risks = "\n".join(f"- {item}" for item in plan.risks) or "- None identified"
    return f"# Plan\n\n{plan.summary}\n\n## Acceptance criteria\n\n{criteria}\n\n## Steps\n\n{steps}\n\n## Risks\n\n{risks}\n"


def review_findings(review: ReviewEnvelope) -> str:
    return "review rejected: " + "; ".join(review.blocking_findings or [review.summary])


def run_summary(state: FactoryState, status: str) -> dict[str, Any]:
    return {
        "run_id": state["run_id"],
        "status": status,
        "test_attempts": state.get("test_attempt_count", 0),
        "review_attempts": state.get("review_attempt_count", 0),
        "changed_files": state.get("changed_files", []),
        "error": state.get("error"),
    }


def final_report(state: FactoryState, outcome: str) -> str:
    changed = "\n".join(f"- {item}" for item in state.get("changed_files", [])) or "- None"
    error = f"\nError: {state['error']}\n" if state.get("error") else ""
    action = {
        "accepted": "inspect the BB diff and commit/open a PR manually.",
        "waiting_for_input": "answer the child task's question, then resume this run.",
        "recovery_needed": "inspect recorded operations and active work before resuming; the workspace remains reserved.",
        "stopping": "wait for confirmed cleanup; external work may still be active.",
    }.get(
        state.get("status", "failed"),
        "inspect the failure and preserved workspace before starting new work.",
    )
    return f"""# Factory run {state["run_id"]}: {outcome}

Plan: {"validated" if state.get("plan_json_path") else "not completed"}
Build: {"completed" if state.get("build_report_path") else "not completed"}
Tests: {"passed" if state.get("test_passed") else "not passed"} on attempt {state.get("test_attempt_count", 0)}
Review: {"approved" if state.get("review_approved") else "not approved"} on attempt {state.get("review_attempt_count", 0)}
{error}
## Changed files

{changed}

No commit or push was performed.
Next action: {action}
"""
