"""Local Factory identity and storage, independent of client conversations."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from platformdirs import user_state_path


class RunContext(Protocol):
    @property
    def session_id(self) -> str: ...
    @property
    def storage_root(self) -> Path: ...
    @property
    def legacy(self) -> bool: ...


@dataclass(frozen=True)
class FactoryContext:
    session_id: str
    storage_root: Path
    workspace_id: str
    owner_uid: int
    legacy: bool = False

    @classmethod
    def open(
        cls, workspace: Path, *, storage_root: Path | None = None, session_id: str | None = None
    ) -> FactoryContext:
        workspace = workspace.expanduser().resolve(strict=True)
        root = (
            storage_root
            or Path(os.environ.get("FACTORY_HOME", user_state_path("software-factory")))
        ).expanduser()
        if root.is_symlink():
            raise ValueError("Factory storage cannot be a symbolic link")
        root = root.resolve()
        if root.is_relative_to(workspace):
            raise ValueError("Factory storage must be outside the target workspace")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077:
            raise ValueError(
                "Factory storage must be private to the current local user (mode 0700)"
            )
        identity = hashlib.sha256(os.fsencode(workspace)).hexdigest()
        return cls(
            session_id or f"session_{identity[:24]}", root, f"workspace_{identity}", os.getuid()
        )


def checkpoint_path(context: RunContext) -> Path:
    return context.storage_root / "factory" / "checkpoints.sqlite"
