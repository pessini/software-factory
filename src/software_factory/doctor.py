from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from software_factory.bb import BBContext
from software_factory.bb.client import resolve_bb_command
from software_factory.config import load_config
from software_factory.engine import checkpoint_path


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def run_doctor(workspace: Path, context: BBContext | None) -> list[Check]:
    checks = [
        Check("Python", sys.version_info >= (3, 14), sys.version.split()[0]),
        Check("Git", shutil.which("git") is not None, shutil.which("git") or "not found"),
    ]
    command = resolve_bb_command()
    executable = shutil.which(command[0])
    checks.append(
        Check(
            "BB CLI",
            executable is not None,
            " ".join(command) if executable else f"not found: {command[0]}",
        )
    )
    git_marker = workspace / ".git"
    checks.append(Check("Workspace", git_marker.exists(), str(workspace)))
    checks.append(
        Check(
            "Child isolation",
            False,
            "Live BB execution is disabled until an adapter enforces read-only roles and controller storage protection",
        )
    )
    try:
        config = load_config(workspace)
        detail = f"v{config.version}, {len(config.quality.commands)} quality command(s)"
        checks.append(Check("Policy", True, detail))
    except Exception as exc:
        checks.append(Check("Policy", False, str(exc)))
    if context is None:
        checks.append(
            Check("BB context", False, "BB_THREAD_ID/PROJECT_ID/ENVIRONMENT_ID/STORAGE missing")
        )
    else:
        checks.append(Check("BB context", True, context.parent_thread_id))
        path = checkpoint_path(context)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            checks.append(Check("Persistence", os.access(path.parent, os.W_OK), str(path)))
        except OSError as exc:
            checks.append(Check("Persistence", False, str(exc)))
    return checks
