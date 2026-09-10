from __future__ import annotations

import json
from pathlib import Path

from software_factory.contracts import BuildEnvelope, PlanEnvelope, ReviewEnvelope


def planner_prompt(run_id: str, request: str, workspace: Path) -> str:
    schema = json.dumps(PlanEnvelope.model_json_schema(), indent=2)
    return f"""You are the Planner for software factory run {run_id}.
Work read-only in {workspace}. Analyze the repository and the request below. Do not edit files.
Return exactly one JSON object matching the schema, with observable acceptance criteria.
Do not wrap it in prose or Markdown fences. Advisory commands are never executed by the factory.

REQUEST:
{request}

SCHEMA:
{schema}
"""


def builder_prompt(run_id: str, request: str, plan_path: Path) -> str:
    schema = json.dumps(BuildEnvelope.model_json_schema(), indent=2)
    return f"""You are the Builder for software factory run {run_id}.
Implement the request using the validated plan at {plan_path}. You may edit application and test files.
Do not edit .factory/factory.yaml, .bb, CI/deployment controls, or any protected path.
Do not commit or push. At the end return exactly one JSON object matching the schema.
changed_files must list every repository file you changed and no unchanged files.

REQUEST:
{request}

SCHEMA:
{schema}
"""


def repair_prompt(
    run_id: str,
    attempt: int,
    log_paths: list[str],
    *,
    request: str,
    plan_path: Path,
    changed: list[str],
) -> str:
    schema = json.dumps(BuildEnvelope.model_json_schema(), indent=2)
    return f"""Repair software factory run {run_id} after deterministic test attempt {attempt} failed.
Read the exact logs at: {", ".join(log_paths)}. Diagnose and fix only the implementation.
Do not alter factory policy, quality commands, CI/deployment controls, or protected paths.
Original request: {json.dumps(request)}
Validated plan: {plan_path}
Current implementation files: {json.dumps(changed)}
Keep the original scope and criteria. You may be starting a new conversation.
Do not commit or push. Return exactly one JSON object matching this schema:
{schema}
"""


def reviewer_prompt(
    run_id: str,
    request: str,
    plan_path: Path,
    changed: list[str],
    test_result_path: Path,
) -> str:
    schema = json.dumps(ReviewEnvelope.model_json_schema(), indent=2)
    return f"""You are the read-only Reviewer for software factory run {run_id}.
Do not edit files. Compare the original request, plan, current diff, and deterministic test results.
Approve only when the requirements and acceptance criteria are satisfied without blocking defects.
Return exactly one JSON object matching the schema.

REQUEST:
{request}
PLAN: {plan_path}
CHANGED FILES: {json.dumps(changed)}
TEST RESULT: {test_result_path}

SCHEMA:
{schema}
"""


def revision_prompt(
    run_id: str, findings: list[str], *, request: str, plan_path: Path, changed: list[str]
) -> str:
    schema = json.dumps(BuildEnvelope.model_json_schema(), indent=2)
    return f"""Revise software factory run {run_id} for these blocking review findings:
{json.dumps(findings, indent=2)}
Original request: {json.dumps(request)}
Validated plan: {plan_path}
Current implementation files: {json.dumps(changed)}
Keep the original scope and criteria. You may be starting a new conversation.
Do not commit, push, or edit protected paths. Return exactly one JSON object matching this schema:
{schema}
"""
