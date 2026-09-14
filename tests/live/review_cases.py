"""Live review experiments with independent expected results, not scripted replies."""

import json
import subprocess

FEATURE = (
    "Add is_even(value: int) -> bool to demo_target. Return True for even positive, "
    "negative and zero integers and False for odd integers. Export it from "
    "demo_target.__init__. Add behavior tests. Preserve greeting and factory policy. "
    "Keep the complete change below 100 added plus removed lines. Do not commit or push."
)


def documentation_case(lines):
    content = "# Greeting examples\n" + "".join(
        f"- greeting('Name{i}') returns 'Hello, Name{i}!'\n" for i in range(1, lines)
    )
    return {
        "mode": "risk_based",
        "level": "trivial" if lines <= 10 else "normal" if lines <= 100 else "full",
        "reviewers": ["request", "documentation"]
        if lines <= 10
        else ["request", "code", "documentation"]
        if lines <= 100
        else ["request", "code", "security", "documentation"],
        "lines": lines,
        "content": content,
        "request": (
            "Create only docs/greetings.md with the exact content below, including its final "
            "newline. These are examples of the existing greeting function. Do not change "
            "source, tests, or other files. Existing tests and direct inspection are sufficient. "
            "This is a documentation-size acceptance experiment in a disposable clone. "
            "Do not commit or push.\n\n" + content
        ),
    }


REVIEW_CASES = {
    "focused_revision": {
        "mode": "focused",
        "reviewers": ["request", "code", "security"],
        "revision": True,
        "request": FEATURE
        + (
            " This is a deliberate review acceptance exercise in a disposable clone. "
            "On the FIRST Builder turn implement and test the function in core.py, "
            "importing directly from core in tests, but omit the package export. "
            "Review must report the missing required export as a blocking defect. "
            "Only on the controller's Revision turn add the export and test it. "
            "Final completion still requires the public package export."
        ),
    },
    **{f"risk_docs_{lines}": documentation_case(lines) for lines in (10, 11, 100, 101)},
    "risk_normal": {
        "mode": "risk_based",
        "level": "normal",
        "reviewers": ["request", "code"],
        "request": FEATURE,
    },
    "risk_sensitive": {
        "mode": "risk_based",
        "level": "full",
        "reviewers": ["request", "code", "security"],
        "request": (
            "Add src/demo_target/auth.py with may_view(*, is_admin: bool) -> bool. "
            "For this demo access rule, return True for True and False for False. "
            "Import it directly from demo_target.auth in tests for both cases. "
            "Do not change package exports, greeting, documentation, or factory policy. "
            "Keep the complete change below 100 added plus removed lines. "
            "Do not commit or push."
        ),
    },
}


def review_measurements(state, artifacts):
    if "review_quality" in state:
        return state["review_quality"]
    # Fixed focused mode saves the same review files but no risk selection/report.
    rounds = []
    for path in sorted(artifacts.root.glob("review/judge-*.json")):
        attempt = int(path.stem.removeprefix("judge-"))

        def read(name, attempt=attempt):
            return json.loads(artifacts.read_text(f"review/{name}-{attempt:02d}.json"))

        reviews = {role: read(role) for role in ("request", "code", "security")}
        judge, decision, history = read("judge"), read("attempt"), read("findings")
        rounds.append(
            {
                "attempt": attempt,
                "content_version": judge["content_version"],
                "complete": judge == decision
                and all(r["content_version"] == judge["content_version"] for r in reviews.values()),
                "selection": {"required_reviewers": list(reviews)},
                "missing_reviewers": [],
                "findings": [
                    {"reviewer": role, "finding": finding}
                    for role, review in reviews.items()
                    for finding in review["findings"]
                ],
                "retained_blocking_findings": [
                    f for f in judge["findings"] if f["severity"] in {"critical", "warning"}
                ],
                "assessments": judge["assessments"],
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
            step: value
            for step, value in state["execution_summary"]["steps"].items()
            if step.endswith("_review") or step == "judge"
        },
        "source": "focused review artifacts; no risk policy selected",
    }


def verify_review_case(workspace, state, case, quality):
    rounds = quality["rounds"]
    assert rounds and all(item["complete"] for item in rounds)
    for item in rounds:
        selection = item["selection"]
        assert set(selection["required_reviewers"]) == set(case["reviewers"])
        assert not item["missing_reviewers"]
        if case["mode"] == "risk_based":
            assert selection["policy"]["trivial_max_lines"] == 10
            assert selection["policy"]["normal_max_lines"] == 100
            assert selection["policy"]["normal_max_files"] == 20
        if "level" in case:
            assert selection["level"] == case["level"]
        if "lines" in case:
            assert selection["changed_lines"] == case["lines"]
            assert selection["changed_file_count"] == 1
        if case is REVIEW_CASES["risk_sensitive"]:
            assert "src/demo_target/auth.py" in selection["sensitive_files"]
            assert selection["changed_lines"] < 100
    assert not rounds[-1]["retained_blocking_findings"]
    assert state["handoff"]["judge"]["approved"]
    if case.get("revision"):
        assert state["review_revision_count"] >= 1
        assert rounds[0]["retained_blocking_findings"]
        assert rounds[-1]["resolved_findings"]
    if "content" in case:
        assert state["changed_files"] == ["docs/greetings.md"]
        assert (workspace / "docs/greetings.md").read_text() == case["content"]
    else:
        code = (
            "from demo_target.auth import may_view; "
            "assert may_view(is_admin=True) is True; assert may_view(is_admin=False) is False"
            if case is REVIEW_CASES["risk_sensitive"]
            else "from demo_target import is_even; "
            "assert all(is_even(n) is (n % 2 == 0) for n in range(-20, 21))"
        )
        subprocess.run(
            [str(workspace / ".venv/bin/python"), "-B", "-c", code], cwd=workspace, check=True
        )
