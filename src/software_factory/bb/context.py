from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class BBContext:
    parent_thread_id: str
    project_id: str
    environment_id: str
    thread_storage: Path

    @property
    def session_id(self) -> str:
        return self.parent_thread_id

    @property
    def storage_root(self) -> Path:
        return self.thread_storage

    @property
    def legacy(self) -> bool:
        return True

    @classmethod
    def from_environment(cls, env: Mapping[str, str] | None = None) -> BBContext:
        source = env or os.environ
        names = ("BB_THREAD_ID", "BB_PROJECT_ID", "BB_ENVIRONMENT_ID", "BB_THREAD_STORAGE")
        missing = [name for name in names if not source.get(name)]
        if missing:
            raise ValueError(f"missing BB context: {', '.join(missing)}")
        return cls(
            parent_thread_id=source["BB_THREAD_ID"],
            project_id=source["BB_PROJECT_ID"],
            environment_id=source["BB_ENVIRONMENT_ID"],
            thread_storage=Path(source["BB_THREAD_STORAGE"]).expanduser().resolve(),
        )
