from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Identifier = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]*$")]
ContentVersion = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BlockingQuestion(Contract):
    id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    options: list[str] = Field(default_factory=list)
    # Planning interviews add a suggested answer and its reason; only a user answer decides.
    recommendation: str | None = None
    context: str | None = None


class CompletionCriterion(Contract):
    id: Identifier
    description: Text


class PlanEnvelope(Contract):
    version: Literal[2]
    status: Literal["ready", "blocked", "needs_input"]
    summary: Text
    acceptance_criteria: list[CompletionCriterion]
    implementation_steps: list[Text] = Field(max_length=12)
    scope_exclusions: list[Text] = Field(default_factory=list)
    test_strategy: Text
    risks: list[str] = Field(default_factory=list)
    advisory_test_commands: list[str] = Field(default_factory=list)
    questions: list[BlockingQuestion] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def consistent_questions(self) -> PlanEnvelope:
        if self.status == "needs_input" and not self.questions:
            raise ValueError("needs_input requires a blocking question")
        if self.status == "ready" and self.questions:
            raise ValueError("a ready plan cannot have unanswered questions")
        if len({question.id for question in self.questions}) != len(self.questions):
            raise ValueError("question IDs must be unique")
        ids = [criterion.id for criterion in self.acceptance_criteria]
        if len(ids) != len(set(ids)):
            raise ValueError("criterion IDs must be unique")
        if self.status == "ready" and not ids:
            raise ValueError("a ready plan requires completion criteria")
        return self


class BuildEnvelope(Contract):
    status: Literal["completed", "blocked"]
    summary: str = Field(min_length=1)
    changed_files: list[str]
    blockers: list[str] = Field(default_factory=list)
    manual_work: list[Text] = Field(default_factory=list)


class Finding(Contract):
    id: Identifier
    criterion_id: Identifier | None = None
    category: Literal[
        "correctness",
        "security",
        "reliability",
        "performance",
        "testing",
        "scope",
        "evidence",
        "documentation",
    ]
    severity: Literal["critical", "warning", "suggestion"]
    title: Text
    evidence: Text
    evidence_refs: list[Text] = Field(min_length=1)
    required_change: Text
    confidence: Literal["high", "medium", "low"]
    file: Text | None = None
    line: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def located_line(self) -> Finding:
        if self.line is not None and self.file is None:
            raise ValueError("a finding line requires a file")
        return self

    @property
    def blocking(self) -> bool:
        return self.severity in {"critical", "warning"}


class CriterionResult(Contract):
    criterion_id: Identifier
    status: Literal["satisfied", "unmet", "not_verified"]
    evidence: Text
    evidence_refs: list[Text]

    @model_validator(mode="after")
    def supported_result(self) -> CriterionResult:
        if self.status != "not_verified" and not self.evidence_refs:
            raise ValueError("a verified criterion result requires evidence references")
        return self


class FindingResolution(Contract):
    finding_id: Identifier
    status: Literal["unresolved", "fixed", "reopened"]
    evidence: Text
    evidence_refs: list[Text] = Field(min_length=1)


class ReviewEnvelope(Contract):
    version: Literal[2]
    content_version: ContentVersion
    approved: bool
    summary: Text
    findings: list[Finding]
    criteria: list[CriterionResult] = Field(min_length=1)
    resolutions: list[FindingResolution] = Field(default_factory=list)
    manual_work: list[Text] = Field(default_factory=list)

    @property
    def blocking_findings(self) -> list[Finding]:
        return [finding for finding in self.findings if finding.blocking]

    @model_validator(mode="after")
    def consistent_decision(self) -> ReviewEnvelope:
        incomplete = any(item.status != "satisfied" for item in self.criteria)
        if self.approved and (self.blocking_findings or incomplete):
            raise ValueError("approval cannot contain blocking findings or incomplete criteria")
        if not self.approved and not (self.blocking_findings or incomplete):
            raise ValueError("rejection requires blocking findings or incomplete criteria")
        for label, ids in (
            ("finding", [item.id for item in self.findings]),
            ("criterion", [item.criterion_id for item in self.criteria]),
            ("resolution", [item.finding_id for item in self.resolutions]),
        ):
            if len(ids) != len(set(ids)):
                raise ValueError(f"{label} IDs must be unique")
        return self


Reviewer = Literal["request", "code", "security", "documentation"]


class FocusedReviewEnvelope(Contract):
    version: Literal[1]
    reviewer: Reviewer
    content_version: ContentVersion
    summary: Text
    findings: list[Finding]
    criteria: list[CriterionResult] = Field(default_factory=list)
    manual_work: list[Text] = Field(default_factory=list)

    @model_validator(mode="after")
    def consistent_scope(self) -> FocusedReviewEnvelope:
        if self.reviewer == "request" and not self.criteria:
            raise ValueError("Request review requires criterion results")
        if self.reviewer != "request" and self.criteria:
            raise ValueError("only Request review returns criterion results")
        for ids in (
            [item.id for item in self.findings],
            [item.criterion_id for item in self.criteria],
        ):
            if len(ids) != len(set(ids)):
                raise ValueError("review IDs must be unique")
        return self


class FindingAssessment(Contract):
    reviewer: Reviewer
    finding_id: Identifier
    disposition: Literal["retained", "duplicate", "unsupported"]
    reason: Text
    evidence_refs: list[Text] = Field(min_length=1)
    retained_finding_id: Identifier | None = None

    @model_validator(mode="after")
    def consistent_target(self) -> FindingAssessment:
        if (self.disposition == "unsupported") != (self.retained_finding_id is None):
            raise ValueError(
                "retained and duplicate assessments require a final finding ID; unsupported assessments must omit it"
            )
        return self


class JudgeEnvelope(ReviewEnvelope):
    assessments: list[FindingAssessment]
