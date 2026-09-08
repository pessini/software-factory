from pathlib import Path

import pytest

from software_factory.git import protected_changes
from software_factory.worktree_policy import WorktreePolicy, WorktreePolicyError
from tests.conftest import run


@pytest.mark.asyncio
async def test_ignored_secret_and_cache_never_enter_git_objects(
    tmp_path: Path, factory_yaml: str
) -> None:
    import asyncio

    from tests.conftest import init_repository

    init_repository(tmp_path, factory_yaml, "raise SystemExit(0)\n")
    with (tmp_path / ".gitignore").open("a") as handle:
        handle.write(".env\n.venv/\n")
    (tmp_path / ".env").write_text("DUMMY_SECRET=issue-2-regression\n")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "cache").write_text("dummy dependency cache\n")
    blobs = [
        run_output(["git", "hash-object", str(path)], tmp_path).strip()
        for path in [tmp_path / ".env", tmp_path / ".venv" / "cache"]
    ]

    async with WorktreePolicy(tmp_path, []).read_only("Planner"):
        pass

    for blob in blobs:
        process = await asyncio.create_subprocess_exec(
            "git", "cat-file", "-e", blob, cwd=tmp_path, stderr=asyncio.subprocess.DEVNULL
        )
        assert await process.wait() != 0, "protection stored ignored content in Git"


def test_protected_path_matching_handles_files_and_directories() -> None:
    assert protected_changes(
        ["src/app.py", ".factory/factory.yaml", ".github/workflows/ci.yml"],
        [".factory/factory.yaml", ".github/workflows/"],
    ) == [".factory/factory.yaml", ".github/workflows/ci.yml"]


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["Planner", "Builder", "Reviewer"])
@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
async def test_index_flag_change_stops_role_without_restoration(
    tmp_path: Path,
    factory_yaml: str,
    role: str,
    flag: str,
) -> None:
    from tests.conftest import init_repository

    init_repository(tmp_path, factory_yaml, "raise SystemExit(0)\n")
    policy = WorktreePolicy(tmp_path, [])
    guard = policy.protect_builder_changes() if role == "Builder" else policy.read_only(role)
    with pytest.raises(WorktreePolicyError):
        async with guard:
            run(["git", "update-index", flag, "quality_check.py"], tmp_path)
    prefix = "h " if flag == "--assume-unchanged" else "S "
    assert run_output(["git", "ls-files", "-v", "quality_check.py"], tmp_path).startswith(prefix)


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["Planner", "Builder", "Reviewer"])
async def test_other_worktree_commit_and_new_branch_survive_role_check(
    tmp_path: Path, factory_yaml: str, role: str
) -> None:
    from tests.conftest import init_repository

    init_repository(tmp_path, factory_yaml, "raise SystemExit(0)\n")
    other = tmp_path.parent / f"{tmp_path.name}-other"
    run(["git", "worktree", "add", "-b", "other-work", str(other)], tmp_path)
    policy = WorktreePolicy(tmp_path, [])
    guard = policy.protect_builder_changes() if role == "Builder" else policy.read_only(role)
    with pytest.raises(WorktreePolicyError):
        async with guard:
            (other / "outside.txt").write_text("outside work\n", encoding="utf-8")
            run(["git", "add", "."], other)
            run(
                [
                    "git",
                    "-c",
                    "user.name=Tests",
                    "-c",
                    "user.email=test@example.invalid",
                    "commit",
                    "-qm",
                    "Keep outside work",
                ],
                other,
            )
            outside_commit = run_output(["git", "rev-parse", "HEAD"], other).strip()
            run(["git", "branch", "outside-branch"], other)

    assert run_output(["git", "rev-parse", "other-work"], tmp_path).strip() == outside_commit
    assert run_output(["git", "rev-parse", "outside-branch"], tmp_path).strip() == outside_commit
    assert (other / "outside.txt").read_text() == "outside work\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["Planner", "Builder", "Reviewer"])
async def test_violation_preserves_existing_staged_and_unstaged_work(
    tmp_path: Path, role: str
) -> None:
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
    policy = WorktreePolicy(tmp_path, [])
    guard = policy.protect_builder_changes() if role == "Builder" else policy.read_only(role)
    with pytest.raises(WorktreePolicyError):
        async with guard:
            (tmp_path / ".factory").mkdir()
            (tmp_path / ".factory" / "rogue.txt").write_text("unexpected")

    assert target.read_text(encoding="utf-8") == "unstaged"
    staged = run_output(["git", "show", ":answer.txt"], tmp_path)
    assert staged == "staged"


@pytest.mark.asyncio
async def test_read_only_policy_preserves_ignored_file_violations(tmp_path: Path) -> None:
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

    assert ignored.read_text(encoding="utf-8") == "after\n"
    assert rogue.exists()


@pytest.mark.asyncio
async def test_read_only_policy_preserves_unexpected_commit(tmp_path: Path) -> None:
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

    assert run_output(["git", "rev-parse", "HEAD"], tmp_path).strip() != original_head
    assert target.read_text(encoding="utf-8") == "committed\n"


@pytest.mark.asyncio
async def test_builder_policy_preserves_ignored_protected_changes(tmp_path: Path) -> None:
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

    assert protected.exists()


@pytest.mark.asyncio
async def test_builder_policy_preserves_unexpected_commit_and_files(tmp_path: Path) -> None:
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

    assert run_output(["git", "rev-parse", "HEAD"], tmp_path).strip() != original_head
    assert target.read_text(encoding="utf-8") == "builder edit\n"


def run_output(command: list[str], cwd: Path) -> str:
    import subprocess

    return subprocess.run(command, cwd=cwd, check=True, capture_output=True, text=True).stdout
