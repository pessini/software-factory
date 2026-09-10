from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from software_factory.bb.context import BBContext
from software_factory.config import AgentConfig, ModelOption
from software_factory.persistence import RunStore
from software_factory.processes import finish_process, start_process


class BBError(RuntimeError):
    def __init__(self, message: str, kind: str = "provider") -> None:
        super().__init__(message)
        self.kind = kind


class RecoveryNeeded(RuntimeError):
    pass


class WaitingForInput(RuntimeError):
    pass


@dataclass(frozen=True)
class BBExecution:
    execution_id: str
    status: Literal["running", "completed", "waiting_for_input", "failed", "stopped"]
    output: str | None = None
    reported_choice: AgentConfig | None = None


@dataclass(frozen=True)
class BBThreadState:
    status: str
    stopped: bool


class BBClientProtocol(Protocol):
    async def model_catalog(self) -> list[ModelOption]: ...
    async def validate_workspace(self, workspace: Path) -> None: ...
    async def check_protection(self, controller_root: Path) -> None: ...
    async def spawn_child(
        self, role: str, prompt: str, agent: AgentConfig, spawn_key: str
    ) -> str: ...
    async def find_child(self, role: str, spawn_key: str) -> str | None: ...
    async def wait(self, thread_id: str) -> None: ...
    async def output(self, thread_id: str) -> str: ...
    async def tell(self, thread_id: str, message: str) -> str | None: ...
    async def stop(self, thread_id: str) -> None: ...
    async def inspect_thread(self, thread_id: str) -> BBThreadState: ...
    async def reconcile_operation(
        self, thread_id: str, operation_id: str
    ) -> BBExecution | None: ...
    async def execution(self, thread_id: str, execution_id: str) -> BBExecution: ...


@dataclass
class BBClient:
    context: BBContext
    workspace: Path
    executable: tuple[str, ...] = ("bb",)
    command_timeout_seconds: float = 30
    store: RunStore | None = None
    run_id: str = ""

    async def model_catalog(self) -> list[ModelOption]:
        providers = await self._run(
            "provider", "list", "--environment", self.context.environment_id
        )
        if not isinstance(providers, list):
            raise BBError("BB provider list is invalid", "protocol")
        catalog: list[ModelOption] = []
        for provider in providers:
            if not provider.get("available") or provider["id"] in {
                "acp-software-factory",
                "acp-software-factory-worker",
            }:
                continue
            modes = provider["capabilities"]["permissionModes"]
            if not {"auto", "accept-edits"}.intersection(modes):
                continue
            models = await self._run(
                "provider", "models", provider["id"], "--environment", self.context.environment_id
            )
            if not isinstance(models, list):
                raise BBError("BB model list is invalid", "protocol")
            for model in models:
                catalog.append(
                    ModelOption(
                        provider=provider["id"],
                        model=model["model"],
                        name=model["displayName"],
                        efforts=[
                            item["reasoningEffort"] for item in model["supportedReasoningEfforts"]
                        ],
                        default_effort=model["defaultReasoningEffort"],
                        is_default=model["isDefault"],
                        permission_modes=modes,
                    )
                )
        if not catalog:
            raise BBError(
                "BB returned no supported model choices for this environment", "configuration"
            )
        return catalog

    async def validate_workspace(self, workspace: Path) -> None:
        result = await self._run("environment", "show", self.context.environment_id)
        if not isinstance(result, dict) or result.get("id") != self.context.environment_id:
            raise BBError("BB returned a different environment", "configuration")
        path = result.get("path")
        if not isinstance(path, str) or Path(path).resolve() != workspace.resolve():
            raise BBError("BB environment does not point to the owned workspace", "configuration")
        if result.get("projectId") != self.context.project_id:
            raise BBError("BB environment belongs to a different project", "configuration")

    async def check_protection(self, controller_root: Path) -> None:
        raise BBError(
            "BB execution is disabled: the current adapter cannot enforce read-only "
            "Planner/Reviewer access or deny child writes to controller storage "
            f"({controller_root}). BB auto and accept-edits modes allow workspace writes. "
            "Use an adapter with tested isolation before starting a live run."
        )

    @classmethod
    def from_context(cls, context: BBContext, workspace: Path) -> BBClient:
        if os.environ.get("FACTORY_PROTECTED_WORKER") != "0":
            from software_factory.bb.protected import ProtectedBBClient

            return ProtectedBBClient(
                context=context, workspace=workspace, executable=resolve_bb_command()
            )
        return cls(context=context, workspace=workspace, executable=resolve_bb_command())

    async def _run(self, *args: str) -> Any:
        process, process_id = await start_process(
            [*self.executable, *args, "--json"],
            self.workspace,
            store=self.store,
            run_id=self.run_id,
            kind="bb" if args[:2] in {("thread", "spawn"), ("thread", "tell")} else "bb_control",
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), self.command_timeout_seconds
            )
        except TimeoutError as exc:
            raise BBError("BB command time limit exceeded", "time_limit") from exc
        finally:
            await asyncio.shield(finish_process(process, process_id, self.store))
        if process.returncode:
            detail = (
                stderr.decode(errors="replace").strip() or stdout.decode(errors="replace").strip()
            )
            lowered = detail.lower()
            kind = "provider"
            if any(
                word in lowered
                for word in ("econnrefused", "econnreset", "fetch failed", "connection", "network")
            ):
                kind = "connection"
            elif any(
                word in lowered
                for word in ("unauthorized", "credential", "api key", "invalid model", "forbidden")
            ):
                kind = "configuration"
            raise BBError(f"BB command failed ({process.returncode}): {detail}", kind)
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
        thread = result.get("thread", result) if isinstance(result, dict) else None
        thread_id = thread.get("id") if isinstance(thread, dict) else None
        if not isinstance(thread_id, str) or not thread_id:
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
        matches: set[str] = set()
        for candidate in _find_mappings(result):
            if candidate.get("title") != title:
                continue
            thread_id = _find_string(candidate, ("threadId", "thread_id", "id"))
            if thread_id:
                matches.add(thread_id)
        if len(matches) > 1:
            raise RecoveryNeeded(f"multiple children match the saved creation request: {spawn_key}")
        return next(iter(matches), None)

    async def wait(self, thread_id: str) -> None:
        await self._run("thread", "wait", thread_id, "--status", "idle", "--timeout", "20")

    async def output(self, thread_id: str) -> str:
        result = await self._run("thread", "output", thread_id)
        output = _find_string(result, ("output", "finalOutput", "final_output", "message", "text"))
        if output is None:
            raise BBError("BB output response did not include final text")
        return output

    async def tell(self, thread_id: str, message: str) -> str | None:
        result = await self._run("thread", "tell", thread_id, message, "--mode", "steer")
        # The CLI does not expose a caller-supplied idempotency key. Only the
        # documented top-level receipt can supply a request identity.
        receipt = result.get("requestId") if isinstance(result, dict) else None
        return receipt if isinstance(receipt, str) and receipt else None

    async def stop(self, thread_id: str) -> None:
        await self._run("thread", "stop", thread_id)

    async def inspect_thread(self, thread_id: str) -> BBThreadState:
        result = await self._run("thread", "show", thread_id)
        thread = result.get("thread") if isinstance(result, dict) else None
        if not isinstance(thread, dict) or thread.get("id") != thread_id:
            raise BBError("BB thread inspection response is invalid", "protocol")
        status = thread.get("status")
        if not isinstance(status, str):
            raise BBError("BB did not supply a thread status", "protocol")
        # Pending interactions and scheduled work must also be absent. An idle
        # status alone is not proof that a provider stopped.
        return BBThreadState(status, False)

    async def reconcile_operation(self, thread_id: str, operation_id: str) -> BBExecution | None:
        del thread_id, operation_id
        return None

    async def execution(self, thread_id: str, execution_id: str) -> BBExecution:
        del thread_id, execution_id
        raise RecoveryNeeded(
            "BB CLI output has no execution identity; the current adapter cannot prove which request completed"
        )


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
    packaged = Path(
        "/Applications/bb.app/Contents/Resources/app.asar.unpacked/node_modules/bb-app/dist/bb.js"
    )
    node = shutil.which("node")
    if node and packaged.is_file():
        return (node, str(packaged))
    return ("npx", "--no-install", "--package", "bb-app", "bb")
