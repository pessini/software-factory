"""Validated output of the read-only Grill planning worker."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from software_factory.contracts.models import (
    BlockingQuestion,
    CompletionCriterion,
    Contract,
    Identifier,
    Text,
)


class Topic(Contract):
    """One decision area; waiting topics depend on another unsettled topic."""

    id: Identifier
    title: Text
    state: Literal["settled", "ready", "waiting"]
    depends_on: list[Identifier] = Field(default_factory=list)
    decision: str | None = None
    question_ids: list[str] = Field(default_factory=list)


class GlossaryTerm(Contract):
    term: Text
    definition: Text


class DecisionRecord(Contract):
    """A settled decision; adr=true proposes a record under docs/adr/."""

    id: Identifier
    title: Text
    decision: Text
    rationale: Text
    adr: bool = False
    sources: list[Text] = Field(default_factory=list)


class FeaturePlan(Contract):
    goal: Text
    scope: list[Text] = Field(min_length=1)
    requirements: list[Text] = Field(min_length=1)
    completion_checks: list[CompletionCriterion] = Field(min_length=1)
    assumptions: list[Text] = Field(default_factory=list)
    exclusions: list[Text] = Field(default_factory=list)
    deferred: list[Text] = Field(default_factory=list)
    sources: list[Text] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_checks(self) -> FeaturePlan:
        ids = [check.id for check in self.completion_checks]
        if len(ids) != len(set(ids)):
            raise ValueError("completion check IDs must be unique")
        return self


class GrillEnvelope(Contract):
    version: Literal[1]
    status: Literal["questions", "ready", "blocked"]
    summary: Text
    topics: list[Topic] = Field(default_factory=list)
    questions: list[BlockingQuestion] = Field(default_factory=list, max_length=5)
    glossary: list[GlossaryTerm] = Field(default_factory=list)
    decisions: list[DecisionRecord] = Field(default_factory=list)
    affected: list[Text] = Field(default_factory=list)
    plan: FeaturePlan | None = None

    @model_validator(mode="after")
    def consistent(self) -> GrillEnvelope:
        if self.status == "questions" and not self.questions:
            raise ValueError("questions status requires at least one question")
        if self.status != "questions" and self.questions:
            raise ValueError("only questions status may ask questions")
        if self.status == "ready" and self.plan is None:
            raise ValueError("a ready interview requires a plan")
        if len({question.id for question in self.questions}) != len(self.questions):
            raise ValueError("question IDs must be unique")
        if len({topic.id for topic in self.topics}) != len(self.topics):
            raise ValueError("topic IDs must be unique")
        if len({record.id for record in self.decisions}) != len(self.decisions):
            raise ValueError("decision IDs must be unique")
        return self
