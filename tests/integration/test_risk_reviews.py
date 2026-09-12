from __future__ import annotations

import asyncio
import json

import pytest
from typer.testing import CliRunner

from software_factory.bb.client import RecoveryNeeded
from software_factory.cli import app
from software_factory.engine import FactoryEngine
from tests.conftest import FakeBB, init_repository, run
from tests.integration.test_factory_engine import context, review_output
from tests.integration.test_focused_reviews import (
    assessment,
    focused_output,
    scripts,
    with_findings,
)


def risk_scripts(changes: dict[str, str | bytes | None], *, evidence_file: str):
    responses = scripts()

    def edit(workspace):
        for relative, content in changes.items():
            path = workspace / relative
            if content is None:
                path.unlink()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content if isinstance(content, bytes) else content.encode())

    responses["builder"][0] = {
        "output": json.dumps(
            {
                "status": "completed",
                "summary": "Updated the requested files",
                "changed_files": list(changes),
            }
        ),
        "action": edit,
    }
    responses["documentation_reviewer"] = [{"output": focused_output("documentation")}]
    for items in responses.values():
        for item in items:
            item["output"] = item["output"].replace("answer.txt", evidence_file)
    return responses


async def test_small_edit_inside_authentication_requires_security_review(tmp_path, factory_yaml):
    init_repository(tmp_path, factory_yaml + "\nreview_mode: risk_based\n", "raise SystemExit(0)\n")
    (tmp_path / "service.py").write_text("def authenticate(value):\n    return True\n")
    run(["git", "add", "service.py"], tmp_path)
    run(
        [
            "git",
            "-c",
            "user.name=Tests",
            "-c",
            "user.email=tests@example.invalid",
            "commit",
            "-qm",
            "Add the service fixture",
        ],
        tmp_path,
    )
    bb = FakeBB(
        tmp_path,
        risk_scripts(
            {"service.py": "def authenticate(value):\n    return False\n"},
            evidence_file="service.py",
        ),
    )
    result = await FactoryEngine(workspace=tmp_path, context=context(tmp_path), bb=bb).run(
        "Update the service result"
    )
    assert result["status"] == "ready_for_review", result.get("error")
    assert set(result["handoff"]["reviews"]) == {"request", "code", "security"}
    selection = result["handoff"]["bundle"]["review_selection"]
    assert selection["level"] == "full"
    assert selection["changed_lines"] == 2


async def test_review_round_is_incomplete_until_the_decision_and_history_are_saved(
    tmp_path, factory_yaml
):
    init_repository(tmp_path, factory_yaml + "\nreview_mode: risk_based\n", "raise SystemExit(0)\n")
    bb = FakeBB(tmp_path, risk_scripts({"value.py": "VALUE = 1\n"}, evidence_file="value.py"))

    async def interrupt(event):
        if event.node == "judge" and event.status == "completed":
            raise RecoveryNeeded("Interrupted before saving the decision and finding history")

    result = await FactoryEngine(
        workspace=tmp_path, context=context(tmp_path), bb=bb, event_sink=interrupt
    ).run("Set the value")
    assert result["status"] == "recovery_needed"
    assert not result["review_quality"]["rounds"][0]["complete"]
    resumed = await FactoryEngine(workspace=tmp_path, context=context(tmp_path), bb=bb).resume(
        result["run_id"]
    )
    assert resumed["status"] == "ready_for_review", resumed.get("error")
    assert resumed["review_quality"]["rounds"][0]["complete"]
    assert resumed["execution_summary"]["steps"]["judge"]["dispatch_count"] == 1


@pytest.mark.parametrize(
    "changes,level,reviewers,lines,files",
    [
        ({}, "trivial", {"request"}, 0, 0),
        ({"notes.md": "A line.\n" * 10}, "trivial", {"request", "documentation"}, 10, 1),
        ({"notes.md": "A line.\n" * 11}, "normal", {"request", "code", "documentation"}, 11, 1),
        ({"value.py": "VALUE = 1\n" * 100}, "normal", {"request", "code"}, 100, 1),
        ({"value.py": "VALUE = 1\n" * 101}, "full", {"request", "code", "security"}, 101, 1),
        (
            {f"value_{i}.py": "VALUE = 1\n" for i in range(20)},
            "normal",
            {"request", "code"},
            20,
            20,
        ),
        (
            {f"value_{i}.py": "VALUE = 1\n" for i in range(21)},
            "full",
            {"request", "code", "security"},
            21,
            21,
        ),
        ({"image.bin": b"\x00\x01\x02"}, "full", {"request", "code", "security"}, 0, 1),
    ],
)
async def test_policy_boundaries_select_and_save_required_reviews(
    tmp_path, factory_yaml, changes, level, reviewers, lines, files
):
    init_repository(tmp_path, factory_yaml + "\nreview_mode: risk_based\n", "raise SystemExit(0)\n")
    evidence_file = next(iter(changes), "quality_check.py")
    result = await FactoryEngine(
        workspace=tmp_path,
        context=context(tmp_path),
        bb=FakeBB(tmp_path, risk_scripts(changes, evidence_file=evidence_file)),
    ).run("Apply the requested change")
    assert result["status"] == "ready_for_review", result.get("error")
    assert set(result["handoff"]["reviews"]) == reviewers
    selection = result["handoff"]["bundle"]["review_selection"]
    assert (selection["level"], selection["changed_lines"], selection["changed_file_count"]) == (
        level,
        lines,
        files,
    )
    assert selection["content_version"] == result["content_version"]
    assert selection["reasons"]
    assert result["review_quality"]["rounds"][0]["selection"] == selection


@pytest.mark.parametrize(
    "path",
    [
        "AUTH.py",
        "authorization.py",
        "secrets.json",
        "crypto.py",
        "migrations/001.sql",
        "deploy.yaml",
        "package-lock.json",
        "nested/requirements.txt",
        ".env.example",
    ],
)
async def test_sensitive_paths_override_documentation_and_generated_patterns(
    tmp_path, factory_yaml, path
):
    init_repository(
        tmp_path,
        factory_yaml + "\nreview_mode: risk_based\nreview_policy:\n"
        "  generated_paths: ['*']\n  documentation_paths: ['*']\n",
        "raise SystemExit(0)\n",
    )
    result = await FactoryEngine(
        workspace=tmp_path,
        context=context(tmp_path),
        bb=FakeBB(tmp_path, risk_scripts({path: "value\n"}, evidence_file=path)),
    ).run("Update the sensitive file")
    assert result["status"] == "ready_for_review", result.get("error")
    selection = result["handoff"]["bundle"]["review_selection"]
    assert selection["level"] == "full"
    assert selection["sensitive_files"] == [path]
    assert selection["generated_files"] == []
    assert "security" in result["handoff"]["reviews"]


async def test_revision_reselects_reviews_and_keeps_retained_and_fixed_findings(
    tmp_path, factory_yaml
):
    init_repository(tmp_path, factory_yaml + "\nreview_mode: risk_based\n", "raise SystemExit(0)\n")
    responses = risk_scripts({"notes.md": "A note.\n"}, evidence_file="notes.md")
    following = risk_scripts(
        {"notes.md": "An updated note.\n", "auth.py": "VALUE = 1\n"}, evidence_file="notes.md"
    )
    rejected = json.loads(review_output(False).replace("answer.txt", "notes.md"))
    responses["request_reviewer"][0]["output"] = with_findings(
        responses["request_reviewer"][0]["output"], rejected["findings"][0]
    )
    responses["judge"][0]["output"] = json.dumps(
        {
            **rejected,
            "assessments": [
                {
                    **assessment("request", "retained", "finding-001"),
                    "evidence_refs": ["source:notes.md"],
                }
            ],
        }
    )
    accepted = json.loads(following["judge"][0]["output"])
    accepted["resolutions"] = [
        {
            "finding_id": "finding-001",
            "status": "fixed",
            "evidence": "Verified the required change",
            "evidence_refs": ["source:notes.md"],
        }
    ]
    following["judge"][0]["output"] = json.dumps(accepted)
    for role in ("builder", "request_reviewer", "documentation_reviewer", "judge"):
        responses[role].extend(following[role])
    bb = FakeBB(tmp_path, responses)
    result = await FactoryEngine(workspace=tmp_path, context=context(tmp_path), bb=bb).run(
        "Update the note and required behavior"
    )
    assert result["status"] == "ready_for_review", result.get("error")
    rounds = result["review_quality"]["rounds"]
    assert [item["selection"]["level"] for item in rounds] == ["trivial", "full"]
    assert rounds[0]["retained_blocking_findings"][0]["id"] == "finding-001"
    assert rounds[1]["resolved_findings"][0]["finding_id"] == "finding-001"
    assert rounds[0]["content_version"] != rounds[1]["content_version"]
    for step in ("code_review", "security_review"):
        assert result["execution_summary"]["steps"][step]["dispatch_count"] == 1
    assert result["review_quality"]["execution"]["judge"]["duration_seconds"] is not None
    assert result["review_quality"]["execution"]["judge"]["provider_usage"] == "unavailable"


async def test_rename_cannot_hide_the_old_sensitive_path(tmp_path, factory_yaml):
    init_repository(tmp_path, factory_yaml + "\nreview_mode: risk_based\n", "raise SystemExit(0)\n")
    (tmp_path / "auth.py").write_text("VALUE = 1\n")
    run(["git", "add", "auth.py"], tmp_path)
    run(
        [
            "git",
            "-c",
            "user.name=Tests",
            "-c",
            "user.email=tests@example.invalid",
            "commit",
            "-qm",
            "Add the sensitive fixture",
        ],
        tmp_path,
    )
    result = await FactoryEngine(
        workspace=tmp_path,
        context=context(tmp_path),
        bb=FakeBB(
            tmp_path,
            risk_scripts({"auth.py": None, "value.py": "VALUE = 1\n"}, evidence_file="value.py"),
        ),
    ).run("Move the source file")
    assert result["status"] == "ready_for_review", result.get("error")
    selection = result["handoff"]["bundle"]["review_selection"]
    assert selection["sensitive_files"] == ["auth.py"]
    assert selection["changed_file_count"] == 2
    assert selection["changed_lines"] == 2
    assert "security" in result["handoff"]["reviews"]


async def test_project_thresholds_apply_to_the_saved_run(tmp_path, factory_yaml):
    init_repository(
        tmp_path,
        factory_yaml + "\nreview_mode: risk_based\nreview_policy:\n"
        "  trivial_max_lines: 1\n  normal_max_lines: 5\n  normal_max_files: 2\n",
        "raise SystemExit(0)\n",
    )
    result = await FactoryEngine(
        workspace=tmp_path,
        context=context(tmp_path),
        bb=FakeBB(
            tmp_path, risk_scripts({"value.py": "VALUE = 1\n" * 6}, evidence_file="value.py")
        ),
    ).run("Update the values")
    assert result["status"] == "ready_for_review", result.get("error")
    selection = result["handoff"]["bundle"]["review_selection"]
    assert selection["level"] == "full"
    assert selection["policy"]["normal_max_lines"] == 5


async def test_feedback_cli_preserves_decisions_and_keeps_correction_history(
    tmp_path, factory_yaml
):
    init_repository(tmp_path, factory_yaml + "\nreview_mode: risk_based\n", "raise SystemExit(0)\n")
    responses = risk_scripts({"value.py": "VALUE = 1\n"}, evidence_file="value.py")
    suggestion = {
        **json.loads(review_output(False).replace("answer.txt", "value.py"))["findings"][0],
        "severity": "suggestion",
    }
    responses["code_reviewer"][0]["output"] = with_findings(
        responses["code_reviewer"][0]["output"], suggestion
    )
    responses["judge"][0]["output"] = json.dumps(
        {
            **json.loads(responses["judge"][0]["output"]),
            "findings": [suggestion],
            "assessments": [
                {
                    **assessment("code", "retained", "finding-001"),
                    "evidence_refs": ["source:value.py"],
                }
            ],
        }
    )
    engine = FactoryEngine(
        workspace=tmp_path, context=context(tmp_path), bb=FakeBB(tmp_path, responses)
    )
    result = await engine.run("Set the value")
    assert result["status"] == "ready_for_review", result.get("error")
    env = {
        "BB_THREAD_ID": "parent-1",
        "BB_PROJECT_ID": "project-1",
        "BB_ENVIRONMENT_ID": "environment-1",
        "BB_THREAD_STORAGE": str(context(tmp_path).thread_storage),
    }

    async def invoke(*args):
        return await asyncio.to_thread(
            CliRunner().invoke, app, [*args, "--workspace", str(tmp_path)], env=env
        )

    for verdict in ("false_positive", "useful"):
        reply = await invoke(
            "review-feedback",
            result["run_id"],
            "1",
            "code",
            "finding-001",
            verdict,
            "Checked the suggestion against the requested behavior",
        )
        assert reply.exit_code == 0, (reply.output, reply.exception)
    invalid = await invoke(
        "review-feedback", result["run_id"], "1", "code", "missing", "false_positive", "No finding"
    )
    assert invalid.exit_code != 0
    quality = await invoke("review-quality", result["run_id"])
    assert quality.exit_code == 0, quality.output
    payload = json.loads(quality.stdout)
    assert [item["verdict"] for item in payload["feedback"]] == ["false_positive", "useful"]
    assert payload["feedback_counts"] == {"false_positive": 0, "useful": 1}
    assert all(item["content_version"] == result["content_version"] for item in payload["feedback"])
    restored = await engine.inspect(result["run_id"])
    assert restored["handoff"] == result["handoff"]
    assert restored["status"] == "ready_for_review"


@pytest.mark.parametrize("failure", ["invalid_output", "unexpected_edit"])
async def test_required_documentation_review_cannot_be_skipped_or_edit_files(
    tmp_path, factory_yaml, failure
):
    init_repository(tmp_path, factory_yaml + "\nreview_mode: risk_based\n", "raise SystemExit(0)\n")
    responses = risk_scripts({"notes.md": "A note.\n"}, evidence_file="notes.md")
    if failure == "invalid_output":
        responses["documentation_reviewer"] = [{"output": "invalid"}] * 2
    else:
        responses["documentation_reviewer"][0]["action"] = lambda root: (
            root / "notes.md"
        ).write_text("unexpected edit")
    bb = FakeBB(tmp_path, responses)
    result = await FactoryEngine(workspace=tmp_path, context=context(tmp_path), bb=bb).run(
        "Update the note"
    )
    assert result["status"] == "blocked"
    assert not result["review_approved"]
    assert not result.get("handoff")
    assert not result["review_quality"]["rounds"][0]["complete"]
    assert "judge" not in bb.spawn_counts


async def test_generated_noise_is_separate_but_keeps_full_evidence(tmp_path, factory_yaml):
    init_repository(tmp_path, factory_yaml + "\nreview_mode: risk_based\n", "raise SystemExit(0)\n")
    changes = {"notes.md": "A change.\n", "bundle.map": "generated line\n" * 150}
    bb = FakeBB(tmp_path, risk_scripts(changes, evidence_file="notes.md"))
    result = await FactoryEngine(workspace=tmp_path, context=context(tmp_path), bb=bb).run(
        "Update the documentation and generated map"
    )
    assert result["status"] == "ready_for_review", result.get("error")
    bundle = result["handoff"]["bundle"]
    selection = bundle["review_selection"]
    assert selection["level"] == "normal"
    assert selection["changed_lines"] == 1
    assert selection["generated_files"] == ["bundle.map"]
    assert set(bundle["patches"]) == set(changes)
    assert "source:bundle.map" in bundle["evidence_refs"]
    prompt = next(prompt for role, prompt in bb.prompts if role == "code_reviewer")
    assert '"generated_patches": {"bundle.map":' in prompt
    assert '"changed_files": ["notes.md"]' in prompt


async def test_custom_sensitive_patterns_are_added_to_builtin_rules(tmp_path, factory_yaml):
    init_repository(
        tmp_path,
        factory_yaml + "\nreview_mode: risk_based\nreview_policy:\n"
        "  sensitive_paths: ['**/payments.py']\n",
        "raise SystemExit(0)\n",
    )
    result = await FactoryEngine(
        workspace=tmp_path,
        context=context(tmp_path),
        bb=FakeBB(
            tmp_path, risk_scripts({"payments.py": "VALUE = 1\n"}, evidence_file="payments.py")
        ),
    ).run("Update payments")
    assert result["status"] == "ready_for_review", result.get("error")
    assert result["handoff"]["bundle"]["review_selection"]["level"] == "full"
    assert "security" in result["handoff"]["reviews"]
