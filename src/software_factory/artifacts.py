from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel


class ArtifactStore:
    def __init__(self, workspace: Path, run_id: str) -> None:
        self.root = workspace / ".factory" / "runs" / run_id
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, relative: str) -> Path:
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise ValueError(f"artifact path escapes run directory: {relative}")
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def write_text(self, relative: str, content: str) -> Path:
        destination = self.path(relative)
        _atomic_write(destination, content)
        return destination

    def write_json(self, relative: str, value: BaseModel | dict[str, Any] | list[Any]) -> Path:
        if isinstance(value, BaseModel):
            payload: Any = value.model_dump(mode="json")
        else:
            payload = value
        return self.write_text(relative, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _atomic_write(destination: Path, content: str) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
