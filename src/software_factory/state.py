from typing import Literal, TypedDict

RunStatus = Literal["pending", "running", "accepted", "failed", "blocked", "cancelled"]


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
