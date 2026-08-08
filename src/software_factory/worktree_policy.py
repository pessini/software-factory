from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from software_factory.git import (
    WorkspaceSnapshot,
    capture_paths,
    capture_snapshot,
    changed_files,
    protected_changes,
    restore_path_snapshot,
    restore_paths,
    restore_repository_metadata,
    restore_snapshot,
)


class WorktreePolicyError(RuntimeError):
    pass


class WorktreePolicy:
    """Enforce role-specific mutation rules at the Git worktree seam."""

    def __init__(self, workspace: Path, protected_paths: list[str]) -> None:
        self.workspace = workspace
        self.protected_paths = protected_paths

    @asynccontextmanager
    async def read_only(self, role: str) -> AsyncIterator[None]:
        before = await capture_snapshot(self.workspace, include_ignored=True)
        try:
            yield
        except BaseException:
            await self._restore_read_only_changes(before)
            raise
        if await self._restore_read_only_changes(before):
            raise WorktreePolicyError(
                f"{role} modified the worktree despite its read-only contract"
            )

    @asynccontextmanager
    async def protect_builder_changes(self) -> AsyncIterator[None]:
        repository_before = await capture_snapshot(self.workspace)
        protected_before = capture_paths(self.workspace, self.protected_paths)
        try:
            yield
        except BaseException:
            await restore_repository_metadata(self.workspace, repository_before)
            restore_path_snapshot(
                self.workspace, self.protected_paths, protected_before
            )
            await self._restore_protected_changes()
            raise
        metadata_changed = await restore_repository_metadata(self.workspace, repository_before)
        protected_snapshot_changed = restore_path_snapshot(
            self.workspace, self.protected_paths, protected_before
        )
        violations = await self._restore_protected_changes()
        if violations or metadata_changed or protected_snapshot_changed:
            details = violations or self.protected_paths
            raise WorktreePolicyError(
                "Builder changed protected paths or Git metadata; changes restored: "
                + ", ".join(details)
            )

    async def validate_builder_report(self, reported: list[str]) -> list[str]:
        actual = await changed_files(self.workspace)
        violations = await self._restore_protected_changes(actual)
        if violations:
            raise WorktreePolicyError(
                f"Builder changed protected paths; changes restored: {', '.join(violations)}"
            )
        if set(reported) != set(actual):
            raise WorktreePolicyError(
                "Builder report does not match Git diff: "
                f"reported={sorted(reported)!r}, actual={actual!r}"
            )
        if not actual:
            raise WorktreePolicyError("Builder completed without changing repository files")
        return actual

    async def _restore_read_only_changes(self, before: WorkspaceSnapshot) -> bool:
        if (
            await capture_snapshot(
                self.workspace, include_ignored=True, store_ignored=False
            )
            == before
        ):
            return False
        await restore_snapshot(self.workspace, before)
        return True

    async def _restore_protected_changes(self, actual: list[str] | None = None) -> list[str]:
        paths = actual if actual is not None else await changed_files(self.workspace)
        violations = protected_changes(paths, self.protected_paths)
        if violations:
            await restore_paths(self.workspace, violations)
        return violations
