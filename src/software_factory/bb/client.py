from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from software_factory.bb.context import BBContext
from software_factory.config import AgentConfig


class BBError(RuntimeError):
    pass


class BBClientProtocol(Protocol):
    async def check_protection(self, controller_root: Path) -> None: ...
    async def spawn_child(
        self, role: str, prompt: str, agent: AgentConfig, spawn_key: str
    ) -> str: ...
    async def find_child(self, role: str, spawn_key: str) -> str | None: ...
    async def wait(self, thread_id: str) -> None: ...
    async def output(self, thread_id: str) -> str: ...
    async def tell(self, thread_id: str, message: str) -> None: ...
    async def stop(self, thread_id: str) -> None: ...


@dataclass
class BBClient:
    context: BBContext
    workspace: Path
    executable: tuple[str, ...] = ("bb",)

    async def check_protection(self, controller_root: Path) -> None:
        raise BBError(
            "BB execution is disabled: the current adapter cannot enforce read-only "
            "Planner/Reviewer access or deny child writes to controller storage "
            f"({controller_root}). BB auto and accept-edits modes allow workspace writes. "
            "Use an adapter with tested isolation before starting a live run."
        )

    @classmethod
    def from_context(cls, context: BBContext, workspace: Path) -> BBClient:
        return cls(context=context, workspace=workspace, executable=resolve_bb_command())

    async def _run(self, *args: str) -> Any:
        process = await asyncio.create_subprocess_exec(
            *self.executable,
            *args,
            "--json",
            cwd=self.workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode:
            detail = (
                stderr.decode(errors="replace").strip() or stdout.decode(errors="replace").strip()
            )
            raise BBError(f"BB command failed ({process.returncode}): {detail}")
        try:
            return json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise BBError("BB command did not return JSON") from exc

    async def spawn_child(self, role: str, prompt: str, agent: AgentConfig, spawn_key: str) -> str:
        args = [
            "thread",
            "spawn",
            "--project",
            self.context.project_id,
            "--parent-thread",
            self.context.parent_thread_id,
            "--environment",
            self.context.environment_id,
            "--visibility",
            "visible",
            "--title",
            _child_title(role, spawn_key),
            "--prompt",
            prompt,
            "--provider",
            agent.provider,
            "--reasoning-level",
            agent.reasoning_level,
            "--permission-mode",
            agent.permission_mode,
        ]
        if agent.model:
            args.extend(("--model", agent.model))
        result = await self._run(*args)
        thread_id = _find_string(result, ("threadId", "thread_id", "id"))
        if not thread_id:
            raise BBError("BB spawn response did not include a thread ID")
        return thread_id

    async def find_child(self, role: str, spawn_key: str) -> str | None:
        result = await self._run(
            "thread",
            "list",
            "--project",
            self.context.project_id,
            "--parent-thread",
            self.context.parent_thread_id,
            "--include-hidden",
        )
        title = _child_title(role, spawn_key)
        for candidate in _find_mappings(result):
            if candidate.get("title") != title:
                continue
            thread_id = _find_string(candidate, ("threadId", "thread_id", "id"))
            if thread_id:
                return thread_id
        return None

    async def wait(self, thread_id: str) -> None:
        await self._run("thread", "wait", thread_id, "--status", "idle")

    async def output(self, thread_id: str) -> str:
        result = await self._run("thread", "output", thread_id)
        output = _find_string(result, ("output", "finalOutput", "final_output", "message", "text"))
        if output is None:
            raise BBError("BB output response did not include final text")
        return output

    async def tell(self, thread_id: str, message: str) -> None:
        await self._run("thread", "tell", thread_id, message, "--mode", "steer")

    async def stop(self, thread_id: str) -> None:
        await self._run("thread", "stop", thread_id)


def _find_string(value: Any, keys: tuple[str, ...]) -> str | None:
    if isinstance(value, dict):
        for key in keys:
            candidate = value.get(key)
            if isinstance(candidate, str):
                return candidate
        for candidate in value.values():
            found = _find_string(candidate, keys)
            if found is not None:
                return found
    elif isinstance(value, list):
        for candidate in value:
            found = _find_string(candidate, keys)
            if found is not None:
                return found
    return None


def _find_mappings(value: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        found.append(value)
        for candidate in value.values():
            found.extend(_find_mappings(candidate))
    elif isinstance(value, list):
        for candidate in value:
            found.extend(_find_mappings(candidate))
    return found


def _child_title(role: str, spawn_key: str) -> str:
    return f"{role.title()} · Software Factory · {spawn_key}"


def resolve_bb_command() -> tuple[str, ...]:
    configured = os.environ.get("FACTORY_BB_COMMAND")
    if configured:
        command = tuple(shlex.split(configured))
        if not command:
            raise ValueError("FACTORY_BB_COMMAND must not be empty")
        return command
    if shutil.which("bb"):
        return ("bb",)
    return ("npx", "--package", "bb-app", "bb")
