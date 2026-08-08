from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlanEnvelope(Contract):
    status: Literal["ready", "blocked"]
    summary: str = Field(min_length=1)
    acceptance_criteria: list[str]
    implementation_steps: list[str]
    risks: list[str] = Field(default_factory=list)
    advisory_test_commands: list[str] = Field(default_factory=list)


class BuildEnvelope(Contract):
    status: Literal["completed", "blocked"]
    summary: str = Field(min_length=1)
    changed_files: list[str]
    blockers: list[str] = Field(default_factory=list)


class ReviewEnvelope(Contract):
    approved: bool
    summary: str = Field(min_length=1)
    blocking_findings: list[str] = Field(default_factory=list)
    non_blocking_findings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def consistent_decision(self) -> ReviewEnvelope:
        if self.approved and self.blocking_findings:
            raise ValueError("approved reviews must not contain blocking findings")
        if not self.approved and not self.blocking_findings:
            raise ValueError("rejected reviews must contain blocking findings")
        return self
