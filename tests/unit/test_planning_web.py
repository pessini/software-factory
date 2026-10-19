"""Planning contracts, handoff content, and local web service access rules."""

from pathlib import Path

import pytest
from pydantic import ValidationError
from starlette.testclient import TestClient

from software_factory.contracts.planning import GrillEnvelope
from software_factory.interactions import Interaction
from software_factory.planning import handoff_criteria, handoff_request, snapshot_hash
from software_factory.web import COOKIE, create_app

PLAN = {
    "goal": "Ship it",
    "scope": ["One file"],
    "requirements": ["Works"],
    "completion_checks": [{"id": "works", "description": "It works"}],
}


def test_grill_envelope_rules():
    with pytest.raises(ValidationError, match="requires at least one question"):
        GrillEnvelope.model_validate({"version": 1, "status": "questions", "summary": "x"})
    with pytest.raises(ValidationError, match="requires a plan"):
        GrillEnvelope.model_validate({"version": 1, "status": "ready", "summary": "x"})
    ready = GrillEnvelope.model_validate(
        {"version": 1, "status": "ready", "summary": "x", "plan": PLAN}
    )
    assert ready.plan is not None


def snapshot():
    return {
        "planning_session_id": "pln_1",
        "revision": 2,
        "request": "Ship it",
        "summary": "Summary",
        "plan": {**PLAN, "assumptions": [], "exclusions": [], "deferred": [], "sources": []},
        "decisions": [
            {
                "id": "store",
                "title": "Storage",
                "decision": "SQLite",
                "rationale": "Local",
                "adr": True,
            }
        ],
        "glossary": [{"term": "Run", "definition": "One workflow execution"}],
    }


def test_handoff_carries_decisions_glossary_and_adr_checks():
    plan = snapshot()
    digest = snapshot_hash(plan)
    request = handoff_request(plan, digest)
    assert digest in request and "Do not restart discovery" in request
    assert "CONTEXT.md" in request and "docs/adr/" in request
    assert [item["id"] for item in handoff_criteria(plan)] == [
        "works",
        "planning-glossary",
        "planning-adr-store",
    ]


def test_choice_display_keeps_exact_spacing():
    interaction = Interaction.model_validate(
        {
            "interaction_id": "fct_1:plan:1",
            "run_id": "fct_1",
            "source": "planner",
            "questions": [{"id": "prefix", "question": "Prefix?", "options": ["Item: ", "Name: "]}],
        }
    )
    assert 'Choices: "Item: "; "Name: "' in interaction.display()


class FakePlanning:
    workspace = Path("/tmp/target")
    storage_root = Path("/tmp/controller")

    def __init__(self):
        self.started = []

    async def sessions(self):
        return []

    async def start(self, request, *, request_id):
        self.started.append((request, request_id))
        return {"session_id": "pln_1"}


def client(planning=None):
    app = create_app(planning or FakePlanning(), "secret-token", 4317)
    return TestClient(app, base_url="http://127.0.0.1:4317")


def test_web_requires_token_and_sets_a_strict_cookie():
    web = client()
    assert web.get("/api/sessions").status_code == 401
    assert web.get("/").status_code == 401
    entered = web.get("/planning/pln_1?token=secret-token", follow_redirects=False)
    assert entered.status_code == 303 and entered.headers["location"] == "/planning/pln_1"
    assert "samesite=strict" in entered.headers["set-cookie"].lower()
    assert web.cookies.get(COOKIE) == "secret-token"
    assert web.get("/api/sessions").json()["workspace"] == "/tmp/target"


def test_web_rejects_foreign_hosts_origins_and_form_posts():
    planning = FakePlanning()
    web = client(planning)
    headers = {"x-factory-token": "secret-token"}
    assert web.get("/api/sessions", headers={**headers, "host": "evil.test"}).status_code == 421
    assert (
        web.post(
            "/api/sessions",
            headers={**headers, "origin": "http://evil.test"},
            json={"request": "x", "request_id": "1"},
        ).status_code
        == 403
    )
    assert web.post("/api/sessions", headers=headers, data={"request": "x"}).status_code == 415
    created = web.post(
        "/api/sessions",
        headers={**headers, "origin": "http://127.0.0.1:4317"},
        json={"request": "Plan", "request_id": "r1"},
    )
    assert created.json() == {"session_id": "pln_1"}
    assert planning.started == [("Plan", "r1")]
