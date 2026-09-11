"""Saved receipts outside the provider's writable mounts."""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Literal

from pydantic import Field

from software_factory.config import AgentConfig, ConfigModel


class WorkerMessage(ConfigModel):
    factory_worker: Literal[1] = 1
    parent_thread_id: str = Field(min_length=1)
    operation_id: str = Field(pattern=r"^[A-Za-z0-9._-]+:[A-Za-z_]+:[0-9]+:[A-Za-z_]+$")


class WorkerReceipt(ConfigModel):
    version: Literal[1] = 1
    operation_id: str
    container_id: str
    request_hash: str
    requested: AgentConfig
    created_at: float = Field(default_factory=time.time)
    container_creation_requested: bool = False
    status: Literal[
        "prepared", "running", "completed", "waiting_for_input", "failed", "stopped"
    ] = "prepared"
    provider_thread_id: str | None = None
    execution_id: str | None = None
    reported: AgentConfig | None = None
    output: str | None = None
    error: str | None = None
    stopped: bool = False
    usage: dict[str, int] | None = None
    question: str | None = None
    interaction_id: str | None = None


def request_key(operation_id: str) -> str:
    return hashlib.sha256(operation_id.encode()).hexdigest()


class WorkerRecords:
    def __init__(self, storage: Path) -> None:
        self.root = storage / "factory-worker"

    @property
    def stop_requested(self) -> bool:
        return (self.root / "stop-requested").exists()

    def request_stop(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.root / "stop-requested").open("a") as handle:
            handle.flush()
            os.fsync(handle.fileno())
        directory = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def read(self, operation_id: str) -> WorkerReceipt | None:
        path = self.root / f"{request_key(operation_id)}.json"
        if path.is_symlink():
            raise ValueError("worker receipt cannot be a symbolic link")
        if not path.exists():
            return None
        receipt = WorkerReceipt.model_validate_json(path.read_text())
        if receipt.operation_id != operation_id:
            raise ValueError("worker receipt belongs to another operation")
        return receipt

    def all(self) -> list[WorkerReceipt]:
        receipts = []
        for path in self.root.glob("*.json"):
            if path.is_symlink():
                raise ValueError("worker receipt cannot be a symbolic link")
            receipt = WorkerReceipt.model_validate_json(path.read_text())
            if path.stem != request_key(receipt.operation_id):
                raise ValueError("worker receipt identity does not match its filename")
            receipts.append(receipt)
        return receipts

    def save(self, receipt: WorkerReceipt) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.root / f"{request_key(receipt.operation_id)}.json"
        previous = self.read(receipt.operation_id)
        if previous and (
            previous.request_hash != receipt.request_hash or previous.requested != receipt.requested
        ):
            raise ValueError("worker request or settings changed")
        temporary = path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            os.chmod(temporary, 0o600)
            json.dump(receipt.model_dump(mode="json"), handle)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        directory = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
