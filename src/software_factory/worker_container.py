"""A named Apple container owns every provider process for one request."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from software_factory.execution import ExecutionError
from software_factory.processes import finish_process, start_process

WORKER_IMAGE = "software-factory-worker:0.154.0"
OWNER_LABEL = "software-factory.operation"


async def container_command(*args: str, timeout_seconds: float = 30) -> str:
    process, identity = await start_process(["container", *args], Path("/tmp"))
    try:
        output, error = await asyncio.wait_for(process.communicate(), timeout_seconds)
        if process.returncode:
            raise ExecutionError(error.decode(errors="replace").strip(), "container")
        return output.decode()
    finally:
        await asyncio.shield(finish_process(process, identity))


async def inspect_container(name: str, operation_id: str) -> dict[str, Any] | None:
    try:
        values = json.loads(await container_command("inspect", name))
    except ExecutionError as exc:
        if "container not found:" in str(exc):
            return None
        raise
    if not isinstance(values, list) or len(values) != 1:
        raise ExecutionError("container returned an invalid identity", "protocol")
    value: dict[str, Any] = values[0]
    if (
        value.get("id") != name
        or value["configuration"].get("labels", {}).get(OWNER_LABEL) != operation_id
    ):
        raise ExecutionError(
            "container identity does not match the saved request; refusing to alter it",
            "configuration",
        )
    return value


async def stop_container(name: str, operation_id: str) -> bool:
    current = await inspect_container(name, operation_id)
    if current is None:
        return False
    if current["status"]["state"] != "stopped":
        await container_command("stop", "--time", "1", name, timeout_seconds=10)
    current = await inspect_container(name, operation_id)
    return current is not None and current["status"]["state"] == "stopped"


class ContainerProvider:
    """Use provider request IDs; never associate a task's latest text with a request."""

    def __init__(
        self,
        name: str,
        operation_id: str,
        workspace: Path,
        evidence: Path,
        provider_home: Path,
        protected: list[Path],
        *,
        read_only: bool,
        authenticate: bool = True,
    ) -> None:
        self.name, self.operation_id = name, operation_id
        self.workspace, self.evidence, self.provider_home = workspace, evidence, provider_home
        self.protected, self.read_only = protected, read_only
        self.authenticate = authenticate
        self.process: asyncio.subprocess.Process | None = None
        self.process_id = ""
        self.reader: asyncio.Task[None] | None = None
        self.error_reader: asyncio.Task[None] | None = None
        self.pending: dict[int, asyncio.Future[Any]] = {}
        self.notifications: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.sequence = 0

    async def start(self, *, before_create: Callable[[], None] | None = None) -> None:
        if await inspect_container(self.name, self.operation_id) is not None:
            if before_create is not None:
                before_create()
            raise ExecutionError(
                "this request already has a container; reconcile it before continuing", "recovery"
            )
        self.provider_home.mkdir(parents=True, exist_ok=True, mode=0o700)
        auth = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "auth.json"
        if self.authenticate and not auth.is_file():
            raise ExecutionError(
                "the supported worker requires an existing Codex file login", "configuration"
            )
        mounts: list[tuple[Path, str, bool]] = [
            (self.workspace, str(self.workspace), self.read_only),
            (self.evidence, str(self.evidence), True),
            (self.provider_home, "/factory/provider", False),
        ]
        if self.authenticate:
            mounts.append((auth, "/factory/provider/auth.json", True))
        # Apple Container's nested file volumes can hide the surrounding
        # workspace. Mount directories only; the provider's read-only file
        # rules below still deny edits, unlink, rename, and link-based writes.
        mounts.extend((path, str(path), True) for path in self.protected if path.is_dir())
        rules = {
            ":root": "read",
            str(self.workspace): "read" if self.read_only else "write",
            "/tmp": "write",
            "/factory/provider/auth.json": "deny",
        }
        # The provider creates mount placeholders for missing read-only paths.
        # On a writable shared mount those placeholders alter the host checkout.
        # Existing paths are denied here; creation of a new protected path is
        # rejected by the controller's repository check after the operation.
        rules.update({str(path): "read" for path in self.protected if path.exists()})
        table = (
            "{"
            + ",".join(
                json.dumps(path) + "=" + json.dumps(access) for path, access in rules.items()
            )
            + "}"
        )
        args = [
            "create",
            "--name",
            self.name,
            "--label",
            f"{OWNER_LABEL}={self.operation_id}",
            "--cap-drop",
            "ALL",
            "--cap-add",
            "SETUID",
            "--cap-add",
            "SETGID",
            "--cap-add",
            "SETFCAP",
            "--memory",
            "2G",
            "--workdir",
            str(self.workspace),
            "--env",
            "CODEX_HOME=/factory/provider",
            "--env",
            "UV_CACHE_DIR=/tmp/uv-cache",
            "--env",
            "PYTHONDONTWRITEBYTECODE=1",
        ]
        for source, target, read_only in mounts:
            if any(char in str(source) + target for char in (":", "\n")):
                raise ExecutionError(
                    "container mount paths cannot contain colons or newlines", "configuration"
                )
            args.extend(["--volume", f"{source}:{target}" + (":ro" if read_only else "")])
        args.extend(
            [
                WORKER_IMAGE,
                "-c",
                "permissions.factory.filesystem=" + table,
                "-c",
                "permissions.factory.network.enabled=false",
                "-c",
                'approval_policy="never"',
                "-c",
                'default_permissions="factory"',
                "-c",
                "features.multi_agent=false",
                "-c",
                "features.apps=false",
                "-c",
                'web_search="disabled"',
            ]
        )
        if before_create is not None:
            before_create()
        await container_command(*args, timeout_seconds=60)
        current = await inspect_container(self.name, self.operation_id)
        if current is None:
            raise ExecutionError("created container cannot be inspected", "recovery")
        self.process, self.process_id = await start_process(
            ["container", "start", "--attach", "--interactive", self.name],
            Path("/tmp"),
            stdin=asyncio.subprocess.PIPE,
        )
        self.reader = asyncio.create_task(self._read())
        self.error_reader = asyncio.create_task(self._drain_errors())
        result = await self.request(
            "initialize",
            {
                "clientInfo": {"name": "software-factory-worker", "version": "1"},
                "capabilities": {"experimentalApi": True},
            },
        )
        if "/0.154.0 " not in result.get("userAgent", ""):
            raise ExecutionError(
                "worker provider version does not match the tested version", "configuration"
            )

    async def request(self, method: str, params: dict[str, Any]) -> Any:
        assert self.process is not None and self.process.stdin is not None
        self.sequence += 1
        request_id = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        self.process.stdin.write(
            (json.dumps({"id": request_id, "method": method, "params": params}) + "\n").encode()
        )
        await self.process.stdin.drain()
        try:
            return await asyncio.wait_for(future, 60)
        finally:
            self.pending.pop(request_id, None)

    async def _drain_errors(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        while await self.process.stderr.read(65536):
            pass

    async def _read(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        try:
            while line := await self.process.stdout.readline():
                message = json.loads(line)
                if "method" in message:
                    await self.notifications.put(message)
                elif message.get("id") in self.pending:
                    future = self.pending[message["id"]]
                    if not future.done():
                        if "error" in message:
                            future.set_exception(ExecutionError(str(message["error"]), "provider"))
                        else:
                            future.set_result(message.get("result"))
        except Exception as exc:
            await self.notifications.put({"method": "connection/closed", "error": str(exc)})
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(ExecutionError("provider connection closed", "connection"))
            await self.notifications.put({"method": "connection/closed"})

    async def reject_request(self, request_id: Any) -> None:
        assert self.process is not None and self.process.stdin is not None
        self.process.stdin.write(
            (
                json.dumps(
                    {
                        "id": request_id,
                        "error": {
                            "code": -32601,
                            "message": "This worker does not grant additional tool permissions",
                        },
                    }
                )
                + "\n"
            ).encode()
        )
        await self.process.stdin.drain()

    async def respond(self, request_id: Any, result: dict[str, Any]) -> None:
        assert self.process is not None and self.process.stdin is not None
        self.process.stdin.write((json.dumps({"id": request_id, "result": result}) + "\n").encode())
        await self.process.stdin.drain()

    async def close(self) -> None:
        stopped = await stop_container(self.name, self.operation_id)
        if self.process is not None:
            await finish_process(self.process, self.process_id)
        if self.reader is not None:
            await self.reader
        if self.error_reader is not None:
            await self.error_reader
        if not stopped:
            raise ExecutionError("container exit cannot be confirmed", "recovery")
