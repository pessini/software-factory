from __future__ import annotations

import asyncio
import json
import os
import signal
import tempfile
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from software_factory.artifacts import ArtifactStore
from software_factory.bb import BBClientProtocol, BBContext
from software_factory.config import AgentConfig, FactoryConfig, QualityCommand
from software_factory.events import EventSink, FactoryEvent, null_event_sink
from software_factory.persistence import RunStore
from software_factory.worktree_policy import WorktreePolicy


class FactoryCancelled(RuntimeError):
    pass


class FactoryRuntime:
    def __init__(
        self,
        *,
        run_id: str,
        workspace: Path,
        context: BBContext,
        config: FactoryConfig,
        bb: BBClientProtocol,
        store: RunStore,
        artifacts: ArtifactStore,
        event_sink: EventSink = null_event_sink,
        cancel_event: asyncio.Event | None = None,
    ) -> None:
        self.run_id = run_id
        self.workspace = workspace
        self.context = context
        self.config = config
        self.bb = bb
        self.store = store
        self.artifacts = artifacts
        self.event_sink = event_sink
        self.cancel_event = cancel_event or asyncio.Event()
        self.worktree = WorktreePolicy(workspace, config.safety.protected_paths)

    async def verify_repository(self) -> None:
        self.artifacts.verify()
        if self.artifacts.path("protection.json").exists():
            record = json.loads(self.artifacts.read_text("protection.json"))
            await self.worktree.verify_baseline(record["repository_state"])

    async def emit(
        self,
        node: str,
        status: str,
        message: str,
        *,
        attempt: int = 1,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        event = FactoryEvent(
            run_id=self.run_id,
            node=node,
            status=status,  # type: ignore[arg-type]
            message=message,
            attempt=attempt,
            metadata=metadata or {},
        )
        event = await self.store.append_event(event)
        await self.event_sink(event)

    async def check_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise FactoryCancelled("factory run cancelled")
        record = await self.store.get_run(self.run_id)
        if record is not None and record.get("status") == "cancelled":
            self.cancel_event.set()
            raise FactoryCancelled(str(record.get("error") or "factory run cancelled"))

    async def get_or_spawn(
        self, role: str, prompt: str, agent: AgentConfig, *, tell_existing: bool = False
    ) -> tuple[str, str]:
        await self.verify_repository()
        await self.bb.check_protection(self.artifacts.root)
        await self.check_cancelled()
        thread_id = await self.store.child(self.run_id, role)
        if thread_id is None:
            spawn_key = await self.store.prepare_child_spawn(self.run_id, role)
            thread_id = await self.bb.find_child(role, spawn_key)
            reconciled = thread_id is not None
            if thread_id is None:
                thread_id = await self.bb.spawn_child(role, prompt, agent, spawn_key)
            # This write is intentionally before wait/output: it is the side-effect checkpoint.
            await self._activate_child(role, thread_id)
            await self.emit(
                role,
                "info",
                f"{'reconciled' if reconciled else 'spawned'} {role} BB thread",
                metadata={"thread_id": thread_id, "spawn_key": spawn_key},
            )
        elif tell_existing:
            await self.bb.tell(thread_id, prompt)
            await self._activate_child(role, thread_id)
        else:
            await self._activate_child(role, thread_id)
            await self.emit(
                role, "info", f"reattached to {role} BB thread", metadata={"thread_id": thread_id}
            )
        await self.check_cancelled()
        await self.bb.wait(thread_id)
        await self.check_cancelled()
        output = await self.bb.output(thread_id)
        await self.store.clear_active_child(self.run_id)
        self.artifacts.verify()
        return thread_id, output

    async def run_quality_command(self, command: QualityCommand, attempt: int) -> dict[str, Any]:
        await self.check_cancelled()
        relative = f"tests/attempt-{attempt:02d}-{command.name}.log"
        await self.verify_repository()
        started = time.monotonic()
        with tempfile.TemporaryFile() as log:
            process = await asyncio.create_subprocess_exec(
                *command.argv,
                cwd=self.workspace,
                stdout=log,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
            await self.store.set_process_group(self.run_id, process.pid)
            wait_task = asyncio.create_task(process.wait())
            cancel_task = asyncio.create_task(self.cancel_event.wait())
            done, _ = await asyncio.wait(
                {wait_task, cancel_task},
                timeout=command.timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            timed_out = not done
            cancelled = cancel_task in done and self.cancel_event.is_set()
            if timed_out or cancelled:
                await _terminate_process_group(process)
            return_code = await wait_task
            cancel_task.cancel()
            await asyncio.gather(cancel_task, return_exceptions=True)
            await self.store.set_process_group(self.run_id, None)
            log.seek(0)
            content = log.read().decode("utf-8", errors="replace")
        await self.check_cancelled()
        duration = time.monotonic() - started
        log_path = self.artifacts.write_text(relative, content)
        result = {
            "name": command.name,
            "argv": command.argv,
            "exit_code": return_code,
            "duration_seconds": round(duration, 3),
            "timed_out": timed_out,
            "cancelled": cancelled,
            "log_path": str(log_path),
            "output_tail": content[-8000:],
        }
        if cancelled:
            raise FactoryCancelled("factory run cancelled during tests")
        return result

    async def parse_with_correction[EnvelopeT: BaseModel](
        self,
        role: str,
        thread_id: str,
        text: str,
        model: type[EnvelopeT],
        *,
        check_workspace: Callable[[], Awaitable[None]],
    ) -> EnvelopeT:
        await check_workspace()
        try:
            return parse_envelope(text, model)
        except ValueError as first_error:
            await self.verify_repository()
            await self.bb.check_protection(self.artifacts.root)
            await self.check_cancelled()
            correction = (
                f"Your final output failed validation: {first_error}. "
                "Return only one corrected JSON object matching this schema:\n"
                f"{json.dumps(model.model_json_schema(), indent=2)}"
            )
            await self.bb.tell(thread_id, correction)
            await self._activate_child(role, thread_id)
            await self.bb.wait(thread_id)
            await self.check_cancelled()
            corrected = await self.bb.output(thread_id)
            await self.store.clear_active_child(self.run_id)
            self.artifacts.verify()
            await check_workspace()
            return parse_envelope(corrected, model)

    async def _activate_child(self, role: str, thread_id: str) -> None:
        if await self.store.save_child(self.run_id, role, thread_id):
            return
        await self.bb.stop(thread_id)
        raise FactoryCancelled(f"factory run stopped while activating {role} child")


def parse_envelope[EnvelopeT: BaseModel](text: str, model: type[EnvelopeT]) -> EnvelopeT:
    candidates = [text.strip()]
    if "```" in text:
        for block in text.split("```")[1::2]:
            candidates.append(block.removeprefix("json").strip())
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        candidates.append(text[start : end + 1])
    errors: list[str] = []
    for candidate in candidates:
        try:
            return model.model_validate(json.loads(candidate))
        except (json.JSONDecodeError, ValidationError) as exc:
            errors.append(str(exc))
    raise ValueError(
        f"invalid {model.__name__} output: {errors[-1] if errors else 'no JSON object'}"
    )


async def _terminate_process_group(process: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=3)
    except TimeoutError:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()
