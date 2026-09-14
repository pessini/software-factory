from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
from langgraph.errors import NodeCancelledError

from software_factory.config import DeliveryPolicy, load_config
from software_factory.engine import FactoryEngine
from software_factory.git import git
from software_factory.ownership import RunOwnership
from software_factory.runtime import FactoryCancelled
from tests.conftest import FakeBB, init_repository, run
from tests.integration.test_factory_engine import (
    build_output,
    context,
    plan_output,
    review_output,
    write_answer,
)


@pytest.fixture(autouse=True)
def isolated_git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "user.useConfigOnly")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")
    for name in (
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
        "EMAIL",
    ):
        monkeypatch.delenv(name, raising=False)


def delivery_engine(tmp_path: Path, factory_yaml: str, **kwargs) -> FactoryEngine:
    init_repository(tmp_path, factory_yaml, "raise SystemExit(0)\n")
    run(["git", "config", "--local", "user.name", "Factory Tests"], tmp_path)
    run(["git", "config", "--local", "user.email", "factory@example.invalid"], tmp_path)
    return FactoryEngine(
        workspace=tmp_path,
        context=context(tmp_path),
        bb=FakeBB(
            tmp_path,
            {
                "planner": [{"output": plan_output()}],
                "builder": [{"action": write_answer("good"), "output": build_output()}],
                "reviewer": [{"output": review_output()}],
            },
        ),
        **kwargs,
    )


@pytest.mark.asyncio
async def test_manual_handoff_keeps_verified_files_without_publishing(
    tmp_path: Path, factory_yaml: str
) -> None:
    engine = delivery_engine(tmp_path, factory_yaml)
    head = await git(tmp_path, "rev-parse", "HEAD")
    result = await engine.run("Write the correct answer", "fct_manual")
    assert result["status"] == "ready_for_review"
    assert result["delivery"]["status"] == "manual"
    assert await git(tmp_path, "rev-parse", "HEAD") == head
    assert (tmp_path / "answer.txt").read_text() == "good"
    assert "No commit or push was performed." in Path(result["final_report_path"]).read_text()


class ControlledGitHub:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.branches: dict[str, str] = {}
        self.pulls: list[dict] = []
        self.check_state = "passed"
        self.threads: list[dict] = []
        self.summary_comments: list[dict] = []
        self.lose_response_at: str | None = None
        self.cancel_response: Callable[[], Awaitable[object]] | None = None

    async def lose_response(self, operation: str) -> None:
        if self.lose_response_at == operation:
            self.lose_response_at = None
            if self.cancel_response is not None:
                await self.cancel_response()
                raise FactoryCancelled(f"cancelled while {operation} was in flight")
            raise ConnectionError(f"{operation} completed, response lost")

    async def branch_head(self, repository: str, branch: str) -> str | None:
        return self.branches.get(branch)

    async def push(self, repository: str, branch: str, commit: str, previous: str | None) -> None:
        assert self.branches.get(branch) == previous
        self.branches[branch] = commit
        await self.lose_response("push")

    async def find_pull(self, repository: str, branch: str) -> dict | None:
        return next((p for p in self.pulls if p["branch"] == branch), None)

    async def create_pull(
        self, repository: str, branch: str, base: str, title: str, body: str
    ) -> None:
        self.pulls.append(
            {
                "number": 1,
                "url": "https://github.com/example/project/pull/1",
                "branch": branch,
                "base": base,
                "head": self.branches[branch],
                "draft": False,
                "state": "open",
                "body": body,
                "title": title,
            }
        )
        await self.lose_response("pull")

    async def pull(self, repository: str, number: int) -> dict:
        p = self.pulls[number - 1]
        return {**p, "head": self.branches[p["branch"]]}

    async def checks(self, repository: str, commit: str) -> dict:
        return {
            "commit": commit,
            "status": self.check_state,
            "checks": [{"name": "tests", "status": self.check_state}],
        }

    async def review_threads(self, repository: str, number: int) -> list[dict]:
        return json.loads(json.dumps(self.threads))

    async def reply(self, repository: str, number: int, thread: dict, body: str) -> None:
        target = next(item for item in self.threads if item["id"] == thread["id"])
        target["comments"].append({"id": "reply-1", "body": body})
        await self.lose_response("reply")

    async def resolve(self, thread_id: str) -> None:
        next(item for item in self.threads if item["id"] == thread_id)["resolved"] = True
        await self.lose_response("resolve")

    async def comments(self, repository: str, number: int) -> list[dict]:
        return self.summary_comments

    async def comment(self, repository: str, number: int, body: str) -> None:
        self.summary_comments.append({"id": "summary-1", "body": body})
        await self.lose_response("summary")


def pr_policy(**overrides) -> DeliveryPolicy:
    return DeliveryPolicy(
        mode="pull_request",
        repository="example/project",
        base_branch="main",
        issue=7,
        title="Add the correct answer",
        **overrides,
    )


@pytest.mark.asyncio
async def test_authorized_delivery_commits_verified_content_and_reuses_regular_pr(
    tmp_path: Path, factory_yaml: str
) -> None:
    github = ControlledGitHub(tmp_path)
    engine = delivery_engine(tmp_path, factory_yaml, github=github)
    github.branches["main"] = (await git(tmp_path, "rev-parse", "HEAD")).decode().strip()
    config = load_config(tmp_path)
    config.delivery = pr_policy()
    result = await engine.run("Write the correct answer", "fct_delivery", configuration=config)
    assert result["delivery"]["status"] == "delivered", result
    commit = result["delivery"]["commit"]
    assert await git(tmp_path, "show", f"{commit}:answer.txt") == b"good"
    assert await git(tmp_path, "show", "-s", "--format=%an <%ae>", commit) == (
        b"Factory Tests <factory@example.invalid>\n"
    )
    assert await git(tmp_path, "status", "--porcelain") == b""
    assert github.pulls[0]["draft"] is False
    assert "Closes issue #7" in github.pulls[0]["body"]
    assert "\nCloses #7\n" in github.pulls[0]["body"]
    assert "No commit or push was performed." not in Path(result["final_report_path"]).read_text()
    assert github.pulls[0]["branch"].startswith("feat/7-")
    resumed = await engine.deliver("fct_delivery")
    assert resumed["delivery"]["commit"] == commit
    assert len(github.pulls) == 1


@pytest.mark.asyncio
async def test_restart_reconciles_pr_created_before_response_was_lost(
    tmp_path: Path, factory_yaml: str
) -> None:
    class LostResponse(ControlledGitHub):
        async def create_pull(self, *args) -> None:
            await super().create_pull(*args)
            raise ConnectionError("PR created, response lost")

    github = LostResponse(tmp_path)
    engine = delivery_engine(tmp_path, factory_yaml, github=github)
    github.branches["main"] = (await git(tmp_path, "rev-parse", "HEAD")).decode().strip()
    config = load_config(tmp_path)
    config.delivery = pr_policy()
    result = await engine.run("Write the correct answer", "fct_lost", configuration=config)
    assert result["delivery"]["status"] == "recovery_needed"
    restarted = FactoryEngine(
        workspace=tmp_path, context=context(tmp_path), bb=engine.bb, github=github
    )
    resumed = await restarted.resume("fct_lost")
    assert resumed["delivery"]["status"] == "delivered"
    assert len(github.pulls) == 1


@pytest.mark.asyncio
async def test_later_file_change_invalidates_delivery_approval(
    tmp_path: Path, factory_yaml: str
) -> None:
    github = ControlledGitHub(tmp_path)
    engine = delivery_engine(tmp_path, factory_yaml, github=github)
    await engine.run("Write the correct answer", "fct_stale")
    (tmp_path / "answer.txt").write_text("unreviewed")
    result = await engine.deliver("fct_stale", authorization=pr_policy())
    assert result["delivery"]["status"] == "blocked"
    assert result["review_approved"] is False
    assert github.pulls == []
    assert (tmp_path / "answer.txt").read_text() == "unreviewed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ci_status, delivery_status",
    [("failed", "ci_failed"), ("pending", "ci_pending"), ("unavailable", "ci_unavailable")],
)
async def test_ci_state_is_bound_to_delivered_commit(
    tmp_path: Path, factory_yaml: str, ci_status: str, delivery_status: str
) -> None:
    github = ControlledGitHub(tmp_path)
    engine = delivery_engine(tmp_path, factory_yaml, github=github)
    github.branches["main"] = (await git(tmp_path, "rev-parse", "HEAD")).decode().strip()
    github.check_state = ci_status
    config = load_config(tmp_path)
    config.delivery = pr_policy()
    result = await engine.run("Write the correct answer", "fct_ci", configuration=config)
    assert result["delivery"]["status"] == delivery_status
    assert result["delivery"]["ci"]["commit"] == result["delivery"]["commit"]
    assert "No commit or push was performed." not in Path(result["final_report_path"]).read_text()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "lost_operation",
    [
        "push",
        "reply",
        "resolve",
        "summary",
        "parent_save",
        "child_dispatch",
        "cancel_push",
        "cancel_reply",
        "cancel_resolve",
        "cancel_summary",
    ],
)
async def test_authorized_review_fixes_recover_without_repeating_replies(
    tmp_path: Path, factory_yaml: str, lost_operation: str
) -> None:
    github = ControlledGitHub(tmp_path)
    engine = delivery_engine(tmp_path, factory_yaml, github=github)
    github.branches["main"] = (await git(tmp_path, "rev-parse", "HEAD")).decode().strip()
    config = load_config(tmp_path)
    config.delivery = pr_policy(review_fixes=True)
    initial = await engine.run("Write the correct answer", "fct_fixes", configuration=config)
    old_commit = initial["delivery"]["commit"]
    github.threads = [
        {
            "id": "thread-1",
            "body": "Handle an empty input",
            "path": "answer.txt",
            "line": 1,
            "comment_id": 10,
            "commit": old_commit,
            "resolved": False,
            "outdated": False,
            "comments": [{"id": "comment-1", "body": "Handle an empty input"}],
        }
    ]
    github.threads.append(
        {
            **github.threads[0],
            "id": "unselected-thread",
            "comment_id": 11,
            "comments": [{"id": "comment-2", "body": "Another finding"}],
            "body": "Another finding",
        }
    )
    review = json.loads(review_output())
    review["criteria"].append(
        {
            "criterion_id": "pr-fix-001",
            "status": "satisfied",
            "evidence": "Inspected the empty input handling",
            "evidence_refs": ["source:answer.txt"],
        }
    )
    bb = FakeBB(
        tmp_path,
        {
            "planner": [{"output": plan_output()}],
            "builder": [
                {"action": write_answer("good, including empty input"), "output": build_output()}
            ],
            "reviewer": [{"output": json.dumps(review)}],
        },
    )
    worker = FactoryEngine(workspace=tmp_path, context=context(tmp_path), bb=bb, github=github)
    if lost_operation == "child_dispatch":
        bb.spawn_error_after_create = asyncio.CancelledError()
        with pytest.raises(NodeCancelledError):
            await worker.fix_reviews("fct_fixes", ["thread-1"])
    elif lost_operation == "parent_save":
        # A real storage fault after publication, without replacing controller methods.
        with sqlite3.connect(worker.store.path) as connection:
            connection.execute("""CREATE TRIGGER fail_parent_result BEFORE UPDATE OF result_json ON factory_runs
                WHEN NEW.run_id = 'fct_fixes' AND json_extract(NEW.result_json, '$.delivery.review_fix.status') = 'completed'
                BEGIN SELECT RAISE(ABORT, 'simulated storage failure'); END""")
        with pytest.raises(sqlite3.IntegrityError, match="simulated storage failure"):
            await worker.fix_reviews("fct_fixes", ["thread-1"])
        with sqlite3.connect(worker.store.path) as connection:
            connection.execute("DROP TRIGGER fail_parent_result")
    else:
        github.lose_response_at = lost_operation.removeprefix("cancel_")
        if lost_operation.startswith("cancel_"):
            github.cancel_response = lambda: worker.cancel_external("fct_fixes")
        interrupted = await worker.fix_reviews("fct_fixes", ["thread-1"])
        assert interrupted["delivery"]["status"] == "recovery_needed", interrupted
    restarted = FactoryEngine(workspace=tmp_path, context=context(tmp_path), bb=bb, github=github)
    if lost_operation.startswith("cancel_"):
        operations = await worker.store.operations("fct_fixes")
        assert any(op["kind"] == "remote" and op["state"] == "dispatching" for op in operations)
        claim = RunOwnership(tmp_path, "fct_fixes", worker.database_path).claim_path
        assert claim.exists()
        resumed = await restarted.resume("fct_fixes")
        assert resumed["delivery"]["status"] == "recovery_needed"
        assert await worker.store.operations("fct_fixes") == operations
        assert claim.exists()
        return
    github.check_state = "pending"
    result = await restarted.resume("fct_fixes")
    assert result["delivery"]["status"] == "ci_pending", result
    new_commit = result["delivery"]["commit"]
    assert new_commit != old_commit
    assert await git(tmp_path, "show", f"{new_commit}:answer.txt") == b"good, including empty input"
    assert github.threads[0]["resolved"] is True
    assert github.threads[1]["resolved"] is False
    assert len(github.threads[1]["comments"]) == 1
    assert len(github.threads[0]["comments"]) == 2
    assert new_commit in github.threads[0]["comments"][1]["body"]
    assert len(github.summary_comments) == 1
    assert bb.spawn_counts == {"planner": 1, "builder": 1, "reviewer": 1}
    github.check_state = "passed"
    refreshed = await restarted.deliver("fct_fixes")
    assert refreshed["delivery"]["commit"] == new_commit
    assert refreshed["delivery"]["status"] == "delivered"


@pytest.mark.asyncio
async def test_cancel_after_push_prevents_pr_creation(tmp_path: Path, factory_yaml: str) -> None:
    class CancelAfterPush(ControlledGitHub):
        async def push(self, *args) -> None:
            await super().push(*args)
            other = FactoryEngine(
                workspace=tmp_path, context=context(tmp_path), bb=engine.bb, github=self
            )
            await other.cancel_external("fct_cancel_delivery")

    github = CancelAfterPush(tmp_path)
    engine = delivery_engine(tmp_path, factory_yaml, github=github)
    github.branches["main"] = (await git(tmp_path, "rev-parse", "HEAD")).decode().strip()
    config = load_config(tmp_path)
    config.delivery = pr_policy()
    result = await engine.run(
        "Write the correct answer", "fct_cancel_delivery", configuration=config
    )
    assert result["delivery"]["status"] == "cancelled"
    assert github.pulls == []
    assert await engine.store.processes("fct_cancel_delivery") == []
    assert not RunOwnership(
        tmp_path, "fct_cancel_delivery", engine.database_path
    ).claim_path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["push", "pull"])
async def test_cancel_during_remote_delivery_keeps_recovery_claim(
    tmp_path: Path, factory_yaml: str, operation: str
) -> None:
    github = ControlledGitHub(tmp_path)
    engine = delivery_engine(tmp_path, factory_yaml, github=github)
    github.branches["main"] = (await git(tmp_path, "rev-parse", "HEAD")).decode().strip()
    other = FactoryEngine(
        workspace=tmp_path, context=context(tmp_path), bb=engine.bb, github=github
    )
    github.lose_response_at = operation
    github.cancel_response = lambda: other.cancel_external("fct_cancel_inflight")
    config = load_config(tmp_path)
    config.delivery = pr_policy()
    result = await engine.run(
        "Write the correct answer", "fct_cancel_inflight", configuration=config
    )
    assert result["status"] == "ready_for_review"
    assert result["delivery"]["status"] == "recovery_needed"
    assert "No commit or push was performed." not in Path(result["final_report_path"]).read_text()
    operations = await engine.store.operations("fct_cancel_inflight")
    assert (
        next(op for op in operations if op["step"] == f"delivery:{operation}")["state"]
        == "dispatching"
    )
    claim = RunOwnership(tmp_path, "fct_cancel_inflight", engine.database_path).claim_path
    assert claim.exists()
    saved = await engine.inspect("fct_cancel_inflight")
    assert saved["delivery"]["status"] == "recovery_needed"
    resumed = await other.resume("fct_cancel_inflight")
    assert resumed["delivery"]["status"] == "recovery_needed"
    assert await engine.store.operations("fct_cancel_inflight") == operations
    assert len(github.pulls) == (1 if operation == "pull" else 0)
    assert claim.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["index", "branch"])
async def test_delivery_preserves_unexpected_repository_changes(
    tmp_path: Path, factory_yaml: str, change: str
) -> None:
    github = ControlledGitHub(tmp_path)
    engine = delivery_engine(tmp_path, factory_yaml, github=github)
    await engine.run("Write the correct answer", "fct_metadata")
    if change == "index":
        await git(tmp_path, "add", "answer.txt")
    else:
        await git(tmp_path, "branch", "user-work")
    from software_factory.git import capture_snapshot

    before = await capture_snapshot(tmp_path)
    result = await engine.deliver("fct_metadata", authorization=pr_policy())
    assert result["delivery"]["status"] == "blocked"
    assert await capture_snapshot(tmp_path) == before
    assert not github.pulls


@pytest.mark.asyncio
async def test_uncertain_pr_creation_without_visible_result_does_not_repeat(
    tmp_path: Path, factory_yaml: str
) -> None:
    class Uncertain(ControlledGitHub):
        requests = 0

        async def create_pull(self, *args) -> None:
            self.requests += 1
            raise ConnectionError("Cannot establish whether GitHub accepted the request")

    github = Uncertain(tmp_path)
    engine = delivery_engine(tmp_path, factory_yaml, github=github)
    github.branches["main"] = (await git(tmp_path, "rev-parse", "HEAD")).decode().strip()
    config = load_config(tmp_path)
    config.delivery = pr_policy()
    await engine.run("Write the correct answer", "fct_uncertain", configuration=config)
    result = await engine.resume("fct_uncertain")
    assert result["delivery"]["status"] == "recovery_needed"
    assert github.requests == 1


@pytest.mark.asyncio
async def test_missing_review_fix_evidence_prevents_push_and_resolution(
    tmp_path: Path, factory_yaml: str
) -> None:
    github = ControlledGitHub(tmp_path)
    engine = delivery_engine(tmp_path, factory_yaml, github=github)
    github.branches["main"] = (await git(tmp_path, "rev-parse", "HEAD")).decode().strip()
    config = load_config(tmp_path)
    config.delivery = pr_policy(review_fixes=True)
    initial = await engine.run("Write the correct answer", "fct_missing", configuration=config)
    commit = initial["delivery"]["commit"]
    github.threads = [
        {
            "id": "thread-1",
            "body": "Handle empty input",
            "path": "answer.txt",
            "line": 1,
            "comment_id": 10,
            "commit": commit,
            "resolved": False,
            "outdated": False,
            "comments": [{"id": "comment-1", "body": "Handle empty input"}],
        }
    ]
    bb = FakeBB(
        tmp_path,
        {
            "planner": [{"output": plan_output()}],
            "builder": [{"action": write_answer("still missing proof"), "output": build_output()}],
            "reviewer": [{"output": review_output()}],
        },
    )
    worker = FactoryEngine(workspace=tmp_path, context=context(tmp_path), bb=bb, github=github)
    result = await worker.fix_reviews("fct_missing", ["thread-1"])
    assert result["delivery"]["status"] == "fixes_incomplete", result
    assert github.branches[initial["delivery"]["branch"]] == commit
    assert github.threads[0]["resolved"] is False
    assert len(github.threads[0]["comments"]) == 1
    assert github.summary_comments == []


@pytest.mark.asyncio
async def test_same_authorization_reuses_an_inferred_linked_issue(
    tmp_path: Path, factory_yaml: str
) -> None:
    github = ControlledGitHub(tmp_path)
    engine = delivery_engine(tmp_path, factory_yaml, github=github)
    github.branches["main"] = (await git(tmp_path, "rev-parse", "HEAD")).decode().strip()
    config = load_config(tmp_path)
    config.delivery = pr_policy().model_copy(update={"issue": None})
    await engine.run(
        "Implement https://github.com/example/project/issues/7",
        "fct_inferred",
        configuration=config,
    )
    result = await engine.deliver("fct_inferred", authorization=config.delivery)
    assert result["delivery"]["status"] == "delivered"
    assert len(github.pulls) == 1
    assert "Closes issue #7" in github.pulls[0]["body"]
    assert "\nCloses #7\n" in github.pulls[0]["body"]


@pytest.mark.asyncio
async def test_edit_during_remote_reconciliation_prevents_push(
    tmp_path: Path, factory_yaml: str
) -> None:
    class EditDuringLookup(ControlledGitHub):
        async def branch_head(self, repository: str, branch: str) -> str | None:
            if branch != "main":
                (tmp_path / "answer.txt").write_text("unreviewed edit")
            return await super().branch_head(repository, branch)

    github = EditDuringLookup(tmp_path)
    engine = delivery_engine(tmp_path, factory_yaml, github=github)
    github.branches["main"] = (await git(tmp_path, "rev-parse", "HEAD")).decode().strip()
    config = load_config(tmp_path)
    config.delivery = pr_policy()
    result = await engine.run("Write the correct answer", "fct_network_edit", configuration=config)
    assert result["delivery"]["status"] == "blocked"
    assert result["review_approved"] is False
    assert set(github.branches) == {"main"}
    assert github.pulls == []
    assert (tmp_path / "answer.txt").read_text() == "unreviewed edit"


@pytest.mark.asyncio
async def test_later_batch_rechecks_unselected_findings_and_preserves_earlier_fix_criteria(
    tmp_path: Path, factory_yaml: str
) -> None:
    github = ControlledGitHub(tmp_path)
    engine = delivery_engine(tmp_path, factory_yaml, github=github)
    github.branches["main"] = (await git(tmp_path, "rev-parse", "HEAD")).decode().strip()
    config = load_config(tmp_path)
    config.delivery = pr_policy()
    initial = await engine.run("Write the correct answer", "fct_batches", configuration=config)
    # Explicit later authorization can add review handling for the same destination.
    await engine.deliver(
        "fct_batches", authorization=config.delivery.model_copy(update={"review_fixes": True})
    )
    github.threads = [
        {
            "id": f"thread-{n}",
            "body": f"Address case {n}",
            "path": "answer.txt",
            "line": 1,
            "comment_id": n,
            "commit": initial["delivery"]["commit"],
            "resolved": False,
            "outdated": False,
            "comments": [{"id": f"comment-{n}", "body": f"Address case {n}"}],
        }
        for n in (1, 2)
    ]
    reviews = []
    for count in (1, 2):
        review = json.loads(review_output())
        review["criteria"].extend(
            {
                "criterion_id": f"pr-fix-{n:03d}",
                "status": "satisfied",
                "evidence": f"Verified case {n}",
                "evidence_refs": ["source:answer.txt"],
            }
            for n in range(1, count + 1)
        )
        reviews.append({"output": json.dumps(review)})
    bb = FakeBB(
        tmp_path,
        {
            "planner": [{"output": plan_output()}, {"output": plan_output()}],
            "builder": [
                {"action": write_answer("good with case 1"), "output": build_output()},
                {"action": write_answer("good with cases 1 and 2"), "output": build_output()},
            ],
            "reviewer": reviews,
        },
    )
    worker = FactoryEngine(workspace=tmp_path, context=context(tmp_path), bb=bb, github=github)
    first = await worker.fix_reviews("fct_batches", ["thread-1"])
    assert first["delivery"]["status"] == "delivered", first
    second = await worker.fix_reviews("fct_batches", ["thread-2"])
    assert second["delivery"]["status"] == "delivered", second
    assert all(thread["resolved"] for thread in github.threads)
    assert len(github.summary_comments) == 2
    verified = await worker.inspect(second["delivery"]["verified_run_id"])
    assert {item["criterion_id"] for item in verified["handoff"]["criteria"]} == {
        "criterion-001",
        "pr-fix-001",
        "pr-fix-002",
    }
