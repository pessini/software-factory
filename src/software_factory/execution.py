"""Execution boundary owned by Factory, independent of application protocols."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from software_factory.config import AgentConfig, ModelOption
from software_factory.persistence import RunStore


class ExecutionError(RuntimeError):
    def __init__(self, message: str, kind: str = "provider") -> None:
        super().__init__(message)
        self.kind = kind


class RecoveryNeeded(RuntimeError):
    pass


class WaitingForInput(RuntimeError):
    pass


@dataclass(frozen=True)
class ExecutionResult:
    execution_id: str
    status: Literal["running", "completed", "waiting_for_input", "failed", "stopped"]
    output: str | None = None
    reported_choice: AgentConfig | None = None
    interaction_id: str | None = None
    usage: dict[str, int] | None = None
    connection_evidence: dict[str, Any] | None = None


@dataclass(frozen=True)
class WorkerState:
    status: str
    stopped: bool


class ExecutionManager(Protocol):
    def bind_run(self, store: RunStore, run_id: str) -> None: ...
    async def model_catalog(self) -> list[ModelOption]: ...
    async def validate_workspace(self, workspace: Path) -> None: ...
    async def check_protection(self, controller_root: Path) -> None: ...
    async def start_worker(
        self, role: str, prompt: str, agent: AgentConfig, spawn_key: str
    ) -> str: ...
    async def find_worker(self, role: str, spawn_key: str) -> str | None: ...
    async def wait(self, worker_id: str) -> None: ...
    async def output(self, worker_id: str) -> str: ...
    async def continue_worker(self, worker_id: str, message: str) -> str | None: ...
    async def stop(self, worker_id: str) -> None: ...
    async def inspect_worker(self, worker_id: str) -> WorkerState: ...
    async def reconcile_operation(
        self, worker_id: str, operation_id: str
    ) -> ExecutionResult | None: ...
    async def execution(self, worker_id: str, execution_id: str) -> ExecutionResult: ...
