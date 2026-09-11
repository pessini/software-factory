import pytest
from pydantic import ValidationError

from software_factory.contracts import PlanEnvelope, ReviewEnvelope
from software_factory.runtime import parse_envelope


def test_extracts_json_from_fenced_agent_output() -> None:
    result = parse_envelope(
        """Here is the result:\n```json
        {"version":2,"status":"ready","summary":"Plan",
        "acceptance_criteria":[{"id":"criterion-001","description":"It works"}],
        "implementation_steps":["Build it"],"test_strategy":"Check the observable result"}
        ```""",
        PlanEnvelope,
    )
    assert result.status == "ready"
    assert result.acceptance_criteria[0].id == "criterion-001"
    assert result.acceptance_criteria[0].description == "It works"


def test_contract_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        PlanEnvelope.model_validate(
            {
                "version": 2,
                "status": "ready",
                "summary": "Plan",
                "acceptance_criteria": [{"id": "criterion-001", "description": "It works"}],
                "implementation_steps": ["Build it"],
                "test_strategy": "Check behavior",
                "dangerous_command": "anything",
            }
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"approved": False},
        {"criteria": []},
        {
            "criteria": [
                {
                    "criterion_id": "criterion-001",
                    "status": "not_verified",
                    "evidence": "No check",
                    "evidence_refs": [],
                }
            ]
        },
        {
            "criteria": [
                {
                    "criterion_id": "criterion-001",
                    "status": "satisfied",
                    "evidence": "",
                    "evidence_refs": ["source:answer.txt"],
                }
            ]
        },
        {
            "criteria": [
                {
                    "criterion_id": "criterion-001",
                    "status": "satisfied",
                    "evidence": "It works",
                    "evidence_refs": [],
                }
            ]
        },
        {"findings": ["unstructured finding"]},
        {"content_version": "not a version"},
        {"blocking_findings": ["old schema"]},
    ],
)
def test_review_contract_rejects_inconsistent_or_unsupported_decisions(changes) -> None:
    payload = {
        "version": 2,
        "content_version": "a" * 64,
        "approved": True,
        "summary": "Done",
        "findings": [],
        "criteria": [
            {
                "criterion_id": "criterion-001",
                "status": "satisfied",
                "evidence": "Inspected answer",
                "evidence_refs": ["source:answer.txt"],
            }
        ],
        **changes,
    }
    with pytest.raises(ValidationError):
        ReviewEnvelope.model_validate(payload)


@pytest.mark.parametrize(
    "severity,approved", [("critical", True), ("warning", True), ("suggestion", False)]
)
def test_severity_defines_blocking_regardless_of_confidence(severity, approved) -> None:
    import json

    from tests.integration.test_factory_engine import review_output

    payload = json.loads(review_output(False))
    payload["content_version"] = "a" * 64
    payload["approved"] = approved
    payload["criteria"][0]["status"] = "satisfied"
    payload["findings"][0]["severity"] = severity
    payload["findings"][0]["confidence"] = "low"
    with pytest.raises(ValidationError, match="blocking findings"):
        ReviewEnvelope.model_validate(payload)


def test_old_plan_and_review_records_are_explicitly_rejected() -> None:
    with pytest.raises(ValidationError):
        PlanEnvelope.model_validate(
            {
                "status": "ready",
                "summary": "Old",
                "acceptance_criteria": ["Works"],
                "implementation_steps": [],
            }
        )
    with pytest.raises(ValidationError):
        ReviewEnvelope.model_validate({"approved": True, "summary": "Old", "blocking_findings": []})
