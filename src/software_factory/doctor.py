from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from software_factory.config import Selection, load_config
from software_factory.context import FactoryContext
from software_factory.engine import checkpoint_path
from software_factory.execution import ExecutionManager
from software_factory.persistence import RunStore
from software_factory.session_settings import SessionSettings
from software_factory.worker_manager import ProtectedWorkerManager


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def run_doctor(
    workspace: Path, context: FactoryContext, execution: ExecutionManager | None = None
) -> list[Check]:
    checks = [
        Check("Python", sys.version_info >= (3, 14), sys.version.split()[0]),
        Check("Git", shutil.which("git") is not None, shutil.which("git") or "not found"),
    ]
    git_marker = workspace / ".git"
    checks.append(Check("Workspace", git_marker.exists(), str(workspace)))
    config = None
    try:
        config = load_config(workspace)
        detail = f"v{config.version}, {len(config.quality.commands)} quality command(s)"
        checks.append(Check("Policy", True, detail))
        for quality in config.quality.commands:
            program = quality.argv[0]
            available = (
                (workspace / program).is_file()
                if "/" in program
                else shutil.which(program) is not None
            )
            checks.append(
                Check(
                    f"Command: {quality.name}",
                    available,
                    quality.display + "; verify that this command is read-only before use",
                )
            )
    except Exception as exc:
        checks.append(Check("Policy", False, str(exc)))
    path = checkpoint_path(context)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=path.parent) as probe:
            probe.write(b"factory storage check")
            probe.flush()
        asyncio.run(RunStore(path).initialize())
        checks.append(Check("Persistence", True, str(path)))
    except OSError as exc:
        checks.append(Check("Persistence", False, str(exc)))
    client = execution or ProtectedWorkerManager(workspace, context)

    async def connection_checks() -> None:
        try:
            await client.check_protection(context.storage_root / "factory/artifacts")
            checks.append(
                Check(
                    "Child isolation",
                    True,
                    "Protected worker prerequisites are available; live acceptance is a separate check",
                )
            )
        except Exception as exc:
            checks.append(Check("Child isolation", False, str(exc)))
        try:
            await client.validate_workspace(workspace)
            checks.append(Check("Worker workspace", True, str(workspace)))
        except Exception as exc:
            checks.append(Check("Worker workspace", False, str(exc)))
        try:
            catalog = await client.model_catalog()
            if config is not None:
                resolved = SessionSettings.resolve(config, Selection(), catalog)
                detail = "; ".join(
                    f"{step}: {a.provider}/{a.model}/{a.reasoning_level}"
                    for step, a in resolved.effective.items()
                )
            else:
                detail = f"{len(catalog)} available models; policy must be fixed before resolution"
            checks.append(Check("Provider options", config is not None, detail))
        except Exception as exc:
            checks.append(Check("Provider options", False, str(exc)))

    asyncio.run(connection_checks())
    return checks
