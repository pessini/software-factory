from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from software_factory.contracts import PlanEnvelope, ReviewEnvelope
from software_factory.interactions import Interaction
from software_factory.state import FactoryState


def plan_markdown(plan: PlanEnvelope) -> str:
    criteria = "\n".join(f"- {item.id}: {item.description}" for item in plan.acceptance_criteria)
    steps = "\n".join(f"{index}. {item}" for index, item in enumerate(plan.implementation_steps, 1))
    risks = "\n".join(f"- {item}" for item in plan.risks) or "- None identified"
    exclusions = "\n".join(f"- {item}" for item in plan.scope_exclusions) or "- None"
    return f"# Plan\n\n{plan.summary}\n\n## Acceptance criteria\n\n{criteria}\n\n## Steps\n\n{steps}\n\n## Test strategy\n\n{plan.test_strategy}\n\n## Scope exclusions\n\n{exclusions}\n\n## Risks\n\n{risks}\n"


def review_findings(review: ReviewEnvelope) -> str:
    return "review rejected: " + "; ".join(
        [f"{item.id}: {item.required_change}" for item in review.blocking_findings]
        or [review.summary]
    )


def run_summary(state: FactoryState, status: str) -> dict[str, Any]:
    return {
        "run_id": state["run_id"],
        "status": status,
        "test_attempts": state.get("test_attempt_count", 0),
        "review_attempts": state.get("review_attempt_count", 0),
        "changed_files": state.get("changed_files", []),
        "error": state.get("error"),
        "interactions": state.get("interactions", []),
        "content_version": state.get("content_version"),
        "handoff": state.get("handoff"),
        "execution_summary": state.get("execution_summary"),
    }


def final_report(state: FactoryState, outcome: str) -> str:
    changed = "\n".join(f"- {item}" for item in state.get("changed_files", [])) or "- None"
    error = f"\nError: {state['error']}\n" if state.get("error") else ""
    action = {
        "ready_for_review": "inspect the verified changes; commit and PR delivery require a separate action.",
        "accepted": "legacy result is unverified under Phase 1; start a new run for a verified handoff.",
        "waiting_for_input": "answer the saved question with /answer INTERACTION_ID ANSWER.",
        "recovery_needed": "inspect recorded operations and active work before resuming; the workspace remains reserved.",
        "stopping": "wait for confirmed cleanup; external work may still be active.",
    }.get(
        state.get("status", "failed"),
        "inspect the failure and preserved workspace before starting new work.",
    )
    questions = "\n\n".join(
        Interaction.model_validate(item).display()
        for item in state.get("interactions", [])
        if item["state"] == "waiting"
    )
    details = ""
    handoff = state.get("handoff")
    if handoff:
        details += f"\n## Builder change summary\n\n{handoff['change']}\n\n"
        details += (
            "The Builder wrote this summary before the controller checks and review. "
            "Their final results are recorded below.\n\n"
        )
        details += f"Tested and reviewed content version: `{handoff['content_version']}`\n\n"
        details += "## Completion criteria\n\n"
        for criterion in handoff["criteria"]:
            details += f"- {criterion['criterion_id']}: {criterion['status']} — {criterion['evidence']} (references: {', '.join(criterion['evidence_refs'])})\n"
        details += "\n## Baseline and current checks\n\n"
        for baseline, current in zip(
            handoff["baseline"]["commands"], handoff["checks"]["commands"], strict=True
        ):
            details += f"- {current['name']}: baseline exit {baseline['exit_code']}; current exit {current['exit_code']}; {current['comparison']}; {current['log_path']}\n"
        details += "\n## Findings\n\n"
        details += (
            "\n".join(
                f"- {key}: {item['status']} — {item['finding']['title']}"
                for key, item in handoff["findings"].items()
            )
            or "None."
        )
        details += (
            "\n\n## Reported manual work\n\n"
            + ("\n".join(f"- {item}" for item in handoff["manual_work"]) or "None reported.")
            + "\n"
        )
    if state.get("execution_summary"):
        details += (
            "\n## Execution evidence\n\n```json\n"
            + json.dumps(state["execution_summary"], indent=2)
            + "\n```\n"
        )
    return f"""# Factory run {state["run_id"]}: {outcome}

Plan: {"validated" if state.get("plan_json_path") else "not completed"}
Build: {"completed" if state.get("build_report_path") else "not completed"}
Tests: {"passed" if state.get("test_passed") else "not passed"} on attempt {state.get("test_attempt_count", 0)}
Review: {"approved" if state.get("review_approved") else "not approved"} on attempt {state.get("review_attempt_count", 0)}
{error}
{questions}
## Changed files

{changed}
{details}

No commit or push was performed.
Next action: {action}
"""


def execution_summary(
    events: list[dict[str, Any]], operations: list[dict[str, Any]], choices: dict[str, Any]
) -> dict[str, Any]:
    steps = {}
    for step, choice in choices.items():
        node = "revise" if step == "revision" else step
        relevant = [item for item in events if item["node"] == node]
        reported = [
            item["metadata"] for item in relevant if item["message"] == "Execution model choice"
        ]
        starts = [item for item in relevant if item["status"] == "started"]
        ends = [item for item in relevant if item["status"] in {"completed", "failed"}]
        duration = (
            (
                datetime.fromisoformat(ends[-1]["timestamp"])
                - datetime.fromisoformat(starts[0]["timestamp"])
            ).total_seconds()
            if starts and ends
            else None
        )
        attempts = [item for item in operations if item["step"] == node and item["dispatch_count"]]
        steps[step] = {
            "requested": choice,
            "executions": reported,
            "effective_settings": "unverified"
            if not reported or any(item["verification"] != "verified" for item in reported)
            else "verified",
            "duration_seconds": duration,
            "attempt_count": max((item["attempt"] for item in attempts), default=0),
            "dispatch_count": sum(item["dispatch_count"] for item in attempts),
            "provider_usage": [item["usage"] for item in reported if item.get("usage") is not None]
            or "unavailable",
        }
    return {"steps": steps}
