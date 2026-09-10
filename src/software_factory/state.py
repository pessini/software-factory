from typing import Literal, TypedDict

RunStatus = Literal[
    "pending",
    "running",
    "waiting_for_input",
    "recovery_needed",
    "stopping",
    "accepted",
    "failed",
    "blocked",
    "cancelled",
]

FINAL_STATUSES = frozenset({"accepted", "failed", "blocked", "cancelled"})
TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"running", "stopping", "recovery_needed", "failed", "blocked"}),
    "running": frozenset(
        {"waiting_for_input", "recovery_needed", "stopping", "accepted", "failed", "blocked"}
    ),
    "waiting_for_input": frozenset({"running", "recovery_needed", "stopping"}),
    "recovery_needed": frozenset({"running", "waiting_for_input", "stopping"}),
    "stopping": frozenset({"recovery_needed", "cancelled", "failed", "blocked"}),
    **{status: frozenset() for status in FINAL_STATUSES},
}


class FactoryState(TypedDict, total=False):
    run_id: str
    request: str
    status: RunStatus
    bb_parent_thread_id: str
    bb_project_id: str
    bb_environment_id: str
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
