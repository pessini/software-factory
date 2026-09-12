"""Report saved review results and keep user feedback separate from decisions."""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import Field

from software_factory.artifacts import ArtifactStore
from software_factory.contracts.models import Contract, Identifier, Reviewer, Text


class ReviewFeedback(Contract):
    attempt: int = Field(ge=1)
    reviewer: Reviewer
    finding_id: Identifier
    verdict: Literal["false_positive", "useful"]
    reason: Text


def review_quality(artifacts: ArtifactStore, execution: dict[str, Any]) -> dict[str, Any]:
    """Read complete and partial rounds without inferring approval from absence."""
    artifacts.verify()
    rounds = []
    for path in sorted(artifacts.root.glob("review/selection-*.json")):
        selection = json.loads(artifacts.read_text(path.relative_to(artifacts.root).as_posix()))
        attempt = selection.pop("attempt")
        reviews: dict[str, Any] = {}
        for reviewer in selection["required_reviewers"]:
            review_path = f"review/{reviewer}-{attempt:02d}.json"
            if artifacts.path(review_path).exists():
                review = json.loads(artifacts.read_text(review_path))
                if review["content_version"] != selection["content_version"]:
                    raise ValueError("review quality found a stale reviewer record")
                reviews[reviewer] = review
        judge = None
        history = {}
        judge_path = f"review/judge-{attempt:02d}.json"
        if artifacts.path(judge_path).exists():
            judge = json.loads(artifacts.read_text(judge_path))
            if judge["content_version"] != selection["content_version"]:
                raise ValueError("review quality found a stale judge record")
        history_path = f"review/findings-{attempt:02d}.json"
        history_saved = artifacts.path(history_path).exists()
        if history_saved:
            history = json.loads(artifacts.read_text(history_path))
        decision_path = f"review/attempt-{attempt:02d}.json"
        decision = (
            json.loads(artifacts.read_text(decision_path))
            if artifacts.path(decision_path).exists()
            else None
        )
        complete = (
            judge is not None
            and decision == judge
            and history_saved
            and set(reviews) == set(selection["required_reviewers"])
        )
        rounds.append(
            {
                "attempt": attempt,
                "content_version": selection["content_version"],
                "selection": selection,
                "complete": complete,
                "missing_reviewers": [
                    r for r in selection["required_reviewers"] if r not in reviews
                ],
                "findings": [
                    {"reviewer": reviewer, "finding": finding}
                    for reviewer, review in reviews.items()
                    for finding in review["findings"]
                ],
                "retained_blocking_findings": [
                    finding
                    for finding in (judge or {}).get("findings", [])
                    if finding["severity"] in {"critical", "warning"}
                ],
                "assessments": (judge or {}).get("assessments", []),
                "resolved_findings": [
                    {"finding_id": key, **item}
                    for key, item in history.items()
                    if item["status"] == "fixed"
                ],
            }
        )
    return {
        "version": 1,
        "rounds": rounds,
        "execution": {
            step: data
            for step, data in execution.get("steps", {}).items()
            if step.endswith("_review") or step == "judge"
        },
        "model_fallback": "disabled",
    }


def with_feedback(quality: dict[str, Any], feedback: list[dict[str, Any]]) -> dict[str, Any]:
    latest = {(item["attempt"], item["reviewer"], item["finding_id"]): item for item in feedback}
    counts = {"false_positive": 0, "useful": 0}
    for item in latest.values():
        counts[item["verdict"]] += 1
    return {**quality, "feedback": feedback, "feedback_counts": counts}
