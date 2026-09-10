from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

from software_factory.git import (
    capture_ignored,
    capture_paths,
    capture_snapshot,
    changed_files,
    protected_changes,
)


class WorktreePolicyError(RuntimeError):
    pass


class WorktreePolicy:
    """Detect unexpected changes. Never infer ownership or restore user work."""

    def __init__(self, workspace: Path, protected_paths: list[str]) -> None:
        self.workspace = workspace
        self.protected_paths = sorted(set([".factory/", ".bb/", *protected_paths]))

    @asynccontextmanager
    async def read_only(self, role: str) -> AsyncIterator[Callable[[], Awaitable[None]]]:
        before = await capture_snapshot(self.workspace, include_ignored=True)

        async def check() -> None:
            after = await capture_snapshot(self.workspace, include_ignored=True)
            if after != before:
                paths = sorted(
                    path
                    for path in before.worktree.keys() | after.worktree.keys()
                    if before.worktree.get(path) != after.worktree.get(path)
                )
                raise WorktreePolicyError(
                    f"{role} read-only check found changed files or Git metadata; "
                    "workspace preserved for inspection: " + ", ".join(paths)
                )

        try:
            yield check
        finally:
            await check()

    @asynccontextmanager
    async def protect_builder_changes(self) -> AsyncIterator[Callable[[], Awaitable[None]]]:
        before = await capture_snapshot(self.workspace)
        protected_before = capture_paths(self.workspace, self.protected_paths)
        protected_before.update(await capture_ignored(self.workspace))

        async def check() -> None:
            after = await capture_snapshot(self.workspace)
            metadata_changed = (before.head, before.head_ref, before.refs, before.index) != (
                after.head,
                after.head_ref,
                after.refs,
                after.index,
            )
            current = capture_paths(self.workspace, self.protected_paths)
            current.update(await capture_ignored(self.workspace))
            violations = sorted(
                path
                for path in protected_before.keys() | current.keys()
                if protected_before.get(path) != current.get(path)
            )
            if violations or metadata_changed:
                raise WorktreePolicyError(
                    "Builder check found changed protected paths or Git metadata; "
                    "workspace preserved for inspection: " + ", ".join(violations)
                )

        try:
            yield check
        finally:
            await check()

    async def repository_baseline(self) -> dict[str, Any]:
        snapshot = await capture_snapshot(self.workspace)
        return {
            "head": snapshot.head,
            "head_ref": snapshot.head_ref,
            "refs": snapshot.refs,
            "index": snapshot.index.hex(),
            "protected_files": {
                path: asdict(entry)
                for path, entry in capture_paths(self.workspace, self.protected_paths).items()
            },
        }

    async def verify_baseline(self, expected: dict[str, Any]) -> None:
        if await self.repository_baseline() != expected:
            raise WorktreePolicyError(
                "Repository references, index, or protected paths changed since bootstrap; "
                "workspace preserved for inspection"
            )

    async def validate_builder_report(self, reported: list[str]) -> list[str]:
        actual = await changed_files(self.workspace)
        violations = protected_changes(actual, self.protected_paths)
        if violations:
            raise WorktreePolicyError(
                f"Builder changed protected paths; workspace preserved: {', '.join(violations)}"
            )
        if set(reported) != set(actual):
            raise WorktreePolicyError(
                "Builder report does not match Git diff: "
                f"reported={sorted(reported)!r}, actual={actual!r}"
            )
        if not actual:
            raise WorktreePolicyError("Builder completed without changing repository files")
        return actual
