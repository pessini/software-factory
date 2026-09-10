from __future__ import annotations

import asyncio
import re
import subprocess
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

import pytest

from software_factory.bb.client import BBExecution, BBThreadState
from software_factory.config import AgentConfig, ModelOption


@pytest.fixture(autouse=True)
def ownership_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FACTORY_OWNERSHIP_DIR", str(tmp_path.parent / f"{tmp_path.name}-ownership"))


def run(command: list[str], cwd: Path) -> None:
    subprocess.run(command, cwd=cwd, check=True, capture_output=True, text=True)


def init_repository(path: Path, factory_yaml: str, test_script: str) -> None:
    (path / ".factory").mkdir(parents=True)
    (path / ".factory" / "factory.yaml").write_text(factory_yaml, encoding="utf-8")
    (path / "quality_check.py").write_text(test_script, encoding="utf-8")
    (path / ".gitignore").write_text(".factory/runs/\n", encoding="utf-8")
    run(["git", "init", "-q"], path)
    run(["git", "add", "."], path)
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
        path,
    )


@pytest.fixture
def factory_yaml() -> str:
    return """version: 1
workflow: simple-sdlc
agents:
  planner: {provider: fake, reasoning_level: high, permission_mode: auto}
  builder: {provider: fake, reasoning_level: high, permission_mode: accept-edits}
  reviewer: {provider: fake, reasoning_level: high, permission_mode: auto}
quality:
  commands:
    - name: tests
      argv: [\"python3\", \"quality_check.py\"]
      timeout_seconds: 10
limits:
  max_test_repairs: 2
  max_review_revisions: 1
  total_run_timeout_seconds: 60
safety:
  protected_paths: [\".factory/factory.yaml\", \".github/workflows/\"]
  auto_commit: false
  allow_push: false
"""


class FakeBB:
    async def model_catalog(self) -> list[ModelOption]:
        return [
            ModelOption(
                provider="fake",
                model="model-a",
                name="Model A",
                efforts=["high"],
                default_effort="high",
                is_default=True,
                permission_modes=["auto", "accept-edits"],
            ),
            ModelOption(
                provider="fake",
                model="model-b",
                name="Model B",
                efforts=["low", "high"],
                default_effort="low",
                permission_modes=["auto", "accept-edits"],
            ),
        ]

    async def validate_workspace(self, workspace: Path) -> None:
        if self.workspace.resolve() != workspace.resolve():
            raise ValueError("BB environment does not point to the owned workspace")

    async def check_protection(self, controller_root: Path) -> None:
        # In-process scripted test double, not an isolated or supported live provider.
        del controller_root

    def __init__(self, workspace: Path, scripts: dict[str, list[dict[str, Any]]]) -> None:
        self.workspace = workspace
        self.scripts = {role: deque(items) for role, items in scripts.items()}
        self.spawn_counts: defaultdict[str, int] = defaultdict(int)
        self.thread_roles: dict[str, str] = {}
        self.outputs: dict[str, str] = {}
        self.stopped: list[str] = []
        self.wait_error: BaseException | None = None
        self.spawn_error_after_create: BaseException | None = None
        self.remote_threads: dict[tuple[str, str], str] = {}
        self.execution_ids: dict[str, str] = {}
        self.operation_executions: dict[str, BBExecution] = {}
        self.execution_count = 0
        self.choices: list[tuple[str, AgentConfig]] = []

    async def spawn_child(self, role: str, prompt: str, agent: AgentConfig, spawn_key: str) -> str:
        self.choices.append((role, agent))
        self.spawn_counts[role] += 1
        thread_id = f"thread-{role}" + (
            f"-{self.spawn_counts[role]}" if self.spawn_counts[role] > 1 else ""
        )
        self.thread_roles[thread_id] = role
        await self._advance(thread_id)
        self._record_execution(thread_id, prompt)
        self.remote_threads[(role, spawn_key)] = thread_id
        if self.spawn_error_after_create is not None:
            error, self.spawn_error_after_create = self.spawn_error_after_create, None
            raise error
        return thread_id

    async def find_child(self, role: str, spawn_key: str) -> str | None:
        return self.remote_threads.get((role, spawn_key))

    async def wait(self, thread_id: str) -> None:
        del thread_id
        if self.wait_error is not None:
            error, self.wait_error = self.wait_error, None
            raise error

    async def output(self, thread_id: str) -> str:
        return self.outputs[thread_id]

    async def tell(self, thread_id: str, message: str) -> str:
        await self._advance(thread_id)
        self._record_execution(thread_id, message)
        return self.execution_ids[thread_id]

    async def stop(self, thread_id: str) -> None:
        self.stopped.append(thread_id)

    async def inspect_thread(self, thread_id: str) -> BBThreadState:
        return BBThreadState("stopped" if thread_id in self.stopped else "idle", True)

    def _record_execution(self, thread_id: str, prompt: str) -> None:
        self.execution_count += 1
        execution_id = f"execution-{self.execution_count}"
        self.execution_ids[thread_id] = execution_id
        marker = re.search(r"Factory operation: (\S+)", prompt)
        if marker:
            self.operation_executions[marker[1]] = BBExecution(
                execution_id, "completed", self.outputs[thread_id]
            )

    async def reconcile_operation(self, thread_id: str, operation_id: str) -> BBExecution | None:
        return self.operation_executions.get(operation_id)

    async def execution(self, thread_id: str, execution_id: str) -> BBExecution:
        current = self.execution_ids[thread_id]
        return BBExecution(current, "completed", self.outputs[thread_id])

    async def _advance(self, thread_id: str) -> None:
        role = self.thread_roles[thread_id]
        item = self.scripts[role].popleft()
        action = item.get("action")
        if action is not None:
            result = action(self.workspace)
            if asyncio.iscoroutine(result):
                await result
        self.outputs[thread_id] = str(item["output"])
