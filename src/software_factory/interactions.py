"""The saved questions shared by Planner and provider input requests."""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel

from software_factory.contracts.models import BlockingQuestion


class Interaction(BaseModel):
    interaction_id: str
    run_id: str
    source: Literal["planner", "child"]
    operation_id: str | None = None
    questions: list[BlockingQuestion]
    answers: dict[str, str] | None = None
    state: Literal["waiting", "answered", "delivered", "cancelled"] = "waiting"

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> Interaction:
        return cls(
            **{
                key: record[key]
                for key in ("interaction_id", "run_id", "source", "operation_id", "state")
            },
            questions=json.loads(record["questions_json"]),
            answers=json.loads(record["answers_json"]) if record["answers_json"] else None,
        )

    def validate_answers(self, answers: dict[str, str]) -> None:
        if set(answers) != {question.id for question in self.questions} or any(
            not isinstance(answer, str) or not answer.strip() for answer in answers.values()
        ):
            raise ValueError("supply one non-empty answer for each question ID")

    def display(self) -> str:
        lines = [f"Input required for run {self.run_id} ({self.interaction_id}):"]
        for question in self.questions:
            lines.append(f"- {question.id}: {question.question}")
            if question.options:
                lines.append("  Choices: " + "; ".join(question.options))
        lines.append(f"Reply with /answer {self.interaction_id} ANSWER.")
        if len(self.questions) > 1:
            lines.append("Use a JSON object with one answer per question ID.")
        return "\n".join(lines)
