from pathlib import Path

import pytest

from software_factory.git import capture_snapshot, protected_changes, restore_snapshot
from software_factory.worktree_policy import WorktreePolicy, WorktreePolicyError
from tests.conftest import run


def test_protected_path_matching_handles_files_and_directories() -> None:
    assert protected_changes(
        ["src/app.py", ".factory/factory.yaml", ".github/workflows/ci.yml"],
        [".factory/factory.yaml", ".github/workflows/"],
    ) == [".factory/factory.yaml", ".github/workflows/ci.yml"]


@pytest.mark.asyncio
async def test_snapshot_restores_worktree_and_index_state(tmp_path: Path) -> None:
    target = tmp_path / "answer.txt"
    target.write_text("original", encoding="utf-8")
    run(["git", "init", "-q"], tmp_path)
    run(["git", "add", "answer.txt"], tmp_path)
    run(
        [
            "git",
            "-c",
            "user.name=Factory Tests",
            "-c",
            "user.email=factory@example.invalid",
            "commit",
            "-qm",
            "initial fixture",
        ],
        tmp_path,
    )
    target.write_text("staged", encoding="utf-8")
    run(["git", "add", "answer.txt"], tmp_path)
    target.write_text("unstaged", encoding="utf-8")
    snapshot = await capture_snapshot(tmp_path)
    target.write_text("reviewer edit", encoding="utf-8")
    run(["git", "add", "answer.txt"], tmp_path)

    await restore_snapshot(tmp_path, snapshot)

    assert target.read_text(encoding="utf-8") == "unstaged"
    staged = run_output(["git", "show", ":answer.txt"], tmp_path)
    assert staged == "staged"


@pytest.mark.asyncio
async def test_read_only_policy_restores_ignored_files(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("*.secret\n", encoding="utf-8")
    (tmp_path / "tracked.txt").write_text("clean\n", encoding="utf-8")
    ignored = tmp_path / "existing.secret"
    ignored.write_text("before\n", encoding="utf-8")
    run(["git", "init", "-q"], tmp_path)
    run(["git", "add", "."], tmp_path)
    run(
        [
            "git",
            "-c",
            "user.name=Factory Tests",
            "-c",
            "user.email=factory@example.invalid",
            "commit",
            "-qm",
            "initial fixture",
        ],
        tmp_path,
    )
    rogue = tmp_path / "rogue.secret"

    with pytest.raises(WorktreePolicyError, match="read-only"):
        async with WorktreePolicy(tmp_path, []).read_only("Planner"):
            ignored.write_text("after\n", encoding="utf-8")
            rogue.write_text("new\n", encoding="utf-8")

    assert ignored.read_text(encoding="utf-8") == "before\n"
    assert not rogue.exists()


@pytest.mark.asyncio
async def test_read_only_policy_restores_commits_and_head(tmp_path: Path) -> None:
    target = tmp_path / "answer.txt"
    target.write_text("before\n", encoding="utf-8")
    run(["git", "init", "-q"], tmp_path)
    run(["git", "add", "."], tmp_path)
    identity = [
        "-c",
        "user.name=Factory Tests",
        "-c",
        "user.email=factory@example.invalid",
    ]
    run(["git", *identity, "commit", "-qm", "initial fixture"], tmp_path)
    original_head = run_output(["git", "rev-parse", "HEAD"], tmp_path).strip()

    with pytest.raises(WorktreePolicyError, match="read-only"):
        async with WorktreePolicy(tmp_path, []).read_only("Planner"):
            target.write_text("committed\n", encoding="utf-8")
            run(["git", "add", "answer.txt"], tmp_path)
            run(["git", *identity, "commit", "-qm", "forbidden commit"], tmp_path)

    assert run_output(["git", "rev-parse", "HEAD"], tmp_path).strip() == original_head
    assert target.read_text(encoding="utf-8") == "before\n"


@pytest.mark.asyncio
async def test_builder_policy_restores_ignored_protected_paths(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text(".bb/\n", encoding="utf-8")
    (tmp_path / "tracked.txt").write_text("clean\n", encoding="utf-8")
    run(["git", "init", "-q"], tmp_path)
    run(["git", "add", "."], tmp_path)
    run(
        [
            "git",
            "-c",
            "user.name=Factory Tests",
            "-c",
            "user.email=factory@example.invalid",
            "commit",
            "-qm",
            "initial fixture",
        ],
        tmp_path,
    )
    protected = tmp_path / ".bb" / "settings.json"

    with pytest.raises(WorktreePolicyError, match="protected paths"):
        async with WorktreePolicy(tmp_path, [".bb/"]).protect_builder_changes():
            protected.parent.mkdir()
            protected.write_text("{}\n", encoding="utf-8")

    assert not protected.exists()


@pytest.mark.asyncio
async def test_builder_policy_restores_head_but_keeps_uncommitted_edits(tmp_path: Path) -> None:
    target = tmp_path / "answer.txt"
    target.write_text("before\n", encoding="utf-8")
    run(["git", "init", "-q"], tmp_path)
    run(["git", "add", "."], tmp_path)
    identity = [
        "-c",
        "user.name=Factory Tests",
        "-c",
        "user.email=factory@example.invalid",
    ]
    run(["git", *identity, "commit", "-qm", "initial fixture"], tmp_path)
    original_head = run_output(["git", "rev-parse", "HEAD"], tmp_path).strip()

    with pytest.raises(WorktreePolicyError, match="Git metadata"):
        async with WorktreePolicy(tmp_path, []).protect_builder_changes():
            target.write_text("builder edit\n", encoding="utf-8")
            run(["git", "add", "answer.txt"], tmp_path)
            run(["git", *identity, "commit", "-qm", "forbidden commit"], tmp_path)

    assert run_output(["git", "rev-parse", "HEAD"], tmp_path).strip() == original_head
    assert target.read_text(encoding="utf-8") == "builder edit\n"


def run_output(command: list[str], cwd: Path) -> str:
    import subprocess

    return subprocess.run(
        command, cwd=cwd, check=True, capture_output=True, text=True
    ).stdout
