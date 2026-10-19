from typing import Any, Literal, TypedDict

RunStatus = Literal[
    "pending",
    "running",
    "waiting_for_input",
    "recovery_needed",
    "stopping",
    "accepted",
    "ready_for_review",
    "planned",
    "failed",
    "blocked",
    "cancelled",
]

# "planned": a planning revision finished with an immutable plan awaiting human approval.
FINAL_STATUSES = frozenset(
    {"accepted", "ready_for_review", "planned", "failed", "blocked", "cancelled"}
)
TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset(
        {"running", "waiting_for_input", "stopping", "recovery_needed", "failed", "blocked"}
    ),
    "running": frozenset(
        {
            "waiting_for_input",
            "recovery_needed",
            "stopping",
            "accepted",
            "ready_for_review",
            "planned",
            "failed",
            "blocked",
        }
    ),
    "waiting_for_input": frozenset({"running", "recovery_needed", "stopping"}),
    "recovery_needed": frozenset({"running", "waiting_for_input", "stopping"}),
    "stopping": frozenset({"recovery_needed", "cancelled", "failed", "blocked"}),
    **{status: frozenset() for status in FINAL_STATUSES},
}


class FactoryState(TypedDict, total=False):
    evidence_version: int
    evidence_status: str
    run_id: str
    request: str
    status: RunStatus
    context_version: int
    session_id: str
    workspace_id: str
    owner_uid: int
    checkpoint_thread_id: str
    workspace_path: str
    storage_path: str
    planner_thread_id: str
    builder_thread_id: str
    reviewer_thread_id: str
    plan_json_path: str
    plan_markdown_path: str
    build_report_path: str
    test_result_path: str
    review_path: str
    final_report_path: str
    test_repair_count: int
    max_test_repairs: int
    test_attempt_count: int
    test_passed: bool
    review_revision_count: int
    max_review_revisions: int
    review_attempt_count: int
    review_approved: bool
    changed_files: list[str]
    baseline_files: list[str]
    active_child_thread_id: str | None
    error: str | None
    interactions: list[dict[str, Any]]
    baseline_result_path: str
    content_version: str
    handoff: dict[str, Any]
    execution_summary: dict[str, Any]
    review_quality: dict[str, Any]
    delivery: dict[str, Any]
    required_criteria: list[dict[str, str]]
    workflow: str
    planning: dict[str, Any]
