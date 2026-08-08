import pytest
from pydantic import ValidationError

from software_factory.contracts import PlanEnvelope, ReviewEnvelope
from software_factory.runtime import parse_envelope


def test_extracts_json_from_fenced_agent_output() -> None:
    result = parse_envelope(
        """Here is the result:\n```json
        {"status":"ready","summary":"Plan","acceptance_criteria":["It works"],
        "implementation_steps":["Build it"]}
        ```""",
        PlanEnvelope,
    )
    assert result.status == "ready"
    assert result.acceptance_criteria == ["It works"]


def test_contract_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        PlanEnvelope.model_validate(
            {
                "status": "ready",
                "summary": "Plan",
                "acceptance_criteria": ["It works"],
                "implementation_steps": ["Build it"],
                "dangerous_command": "anything",
            }
        )


@pytest.mark.parametrize(
    ("approved", "blocking_findings"),
    [(True, ["still broken"]), (False, [])],
)
def test_review_contract_rejects_inconsistent_decisions(
    approved: bool, blocking_findings: list[str]
) -> None:
    with pytest.raises(ValidationError, match="blocking findings"):
        ReviewEnvelope.model_validate(
            {
                "approved": approved,
                "summary": "Review result",
                "blocking_findings": blocking_findings,
            }
        )
