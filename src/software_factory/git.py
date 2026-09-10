from __future__ import annotations

import asyncio
import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class WorktreeEntry:
    kind: Literal["file", "symlink", "missing", "directory"]
    digest: str | None = None
    link_target: str | None = None
    mode: int | None = None


@dataclass(frozen=True)
class WorkspaceSnapshot:
    worktree: dict[str, WorktreeEntry]
    index: bytes
    head: str
    head_ref: str | None
    refs: dict[str, str]


class GitError(RuntimeError):
    pass


async def git(workspace: Path, *args: str, check: bool = True) -> bytes:
    process = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=workspace,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
    )
    stdout, stderr = await process.communicate()
    if check and process.returncode:
        raise GitError(stderr.decode(errors="replace").strip() or "git command failed")
    return stdout


async def repository_root(workspace: Path) -> Path:
    output = await git(workspace, "rev-parse", "--show-toplevel")
    return Path(output.decode().strip()).resolve()


async def changed_files(workspace: Path) -> list[str]:
    tracked = await git(workspace, "diff", "--name-only", "-z", "HEAD")
    staged = await git(workspace, "diff", "--cached", "--name-only", "-z", "HEAD")
    untracked = await git(workspace, "ls-files", "--others", "--exclude-standard", "-z")
    return sorted(_paths(tracked) | _paths(staged) | _paths(untracked))


async def capture_snapshot(workspace: Path, *, include_ignored: bool = False) -> WorkspaceSnapshot:
    """Read hashes and Git state without storing file contents or changing Git."""
    paths = _paths(await git(workspace, "ls-files", "-z"))
    paths.update(_paths(await git(workspace, "ls-files", "--others", "--exclude-standard", "-z")))
    if include_ignored:
        paths.update(
            _paths(
                await git(
                    workspace, "ls-files", "--others", "--ignored", "--exclude-standard", "-z"
                )
            )
        )
    worktree = {
        relative: _capture_worktree_entry(workspace, relative) for relative in sorted(paths)
    }
    index = await git(workspace, "ls-files", "--stage", "-v", "-z")
    index += b"\0" + await git(workspace, "ls-files", "--stage", "-f", "-z")
    head, head_ref, refs = await repository_metadata(workspace)
    return WorkspaceSnapshot(worktree, index, head, head_ref, refs)


async def capture_ignored(workspace: Path) -> dict[str, WorktreeEntry]:
    """Hash ignored files without copying their contents or writing Git objects."""
    paths = _paths(
        await git(workspace, "ls-files", "--others", "--ignored", "--exclude-standard", "-z")
    )
    return {path: _capture_worktree_entry(workspace, path) for path in sorted(paths)}


async def repository_metadata(workspace: Path) -> tuple[str, str | None, dict[str, str]]:
    head = (await git(workspace, "rev-parse", "--verify", "HEAD")).decode("ascii").strip()
    head_ref = (await git(workspace, "symbolic-ref", "-q", "HEAD", check=False)).decode(
        "utf-8", errors="surrogateescape"
    ).strip() or None
    output = await git(workspace, "for-each-ref", "--format=%(refname)%00%(objectname)")
    refs = {}
    for line in output.splitlines():
        ref, separator, object_id = line.partition(b"\0")
        if separator:
            refs[ref.decode("utf-8", errors="surrogateescape")] = object_id.decode("ascii")
    return head, head_ref, refs


def capture_paths(workspace: Path, rules: list[str]) -> dict[str, WorktreeEntry]:
    snapshot = {}
    for rule in rules:
        relative = rule.rstrip("/") or "."
        target = _workspace_path(workspace, relative)
        snapshot[relative] = _capture_worktree_entry(workspace, relative)
        if target.is_symlink() or not target.is_dir():
            continue
        for directory, names, filenames in os.walk(target, followlinks=False):
            for name in [*names, *filenames]:
                candidate = Path(directory) / name
                child = candidate.relative_to(workspace.resolve()).as_posix()
                snapshot[child] = _capture_worktree_entry(workspace, child)
    return snapshot


def protected_changes(paths: list[str], protected: list[str]) -> list[str]:
    return sorted(
        path
        for path in set(paths)
        if any(
            rule.rstrip("/") == "."
            or path == rule.rstrip("/")
            or path.startswith(f"{rule.rstrip('/')}/")
            for rule in protected
        )
    )


def _paths(output: bytes) -> set[str]:
    return {item.decode("utf-8", errors="surrogateescape") for item in output.split(b"\0") if item}


def _capture_worktree_entry(workspace: Path, relative: str) -> WorktreeEntry:
    path = _workspace_path(workspace, relative)
    if path.is_symlink():
        return WorktreeEntry("symlink", link_target=os.readlink(path))
    if not path.exists():
        return WorktreeEntry("missing")
    mode = stat.S_IMODE(path.stat().st_mode)
    if path.is_dir():
        return WorktreeEntry("directory", mode=mode)
    if not path.is_file():
        raise GitError(f"cannot inspect non-file repository path: {relative}")
    with path.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    return WorktreeEntry("file", digest=digest, mode=mode)


def _workspace_path(workspace: Path, relative: str) -> Path:
    candidate = Path(relative)
    if not relative or candidate.is_absolute() or ".." in candidate.parts:
        raise GitError(f"refusing repository path outside workspace: {relative}")
    root = workspace.resolve()
    target = root / candidate
    if target != root and not target.parent.resolve().is_relative_to(root):
        raise GitError(f"refusing repository path through symlinked parent: {relative}")
    return target
