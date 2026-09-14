from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from contextlib import suppress
from pathlib import Path
from typing import IO
from uuid import uuid4
from weakref import WeakKeyDictionary

from software_factory.persistence import RunStore


class CleanupUncertain(RuntimeError):
    pass


# The child cannot execute a command before its process group is saved. If the
# controller dies before opening the gate, EOF makes the child exit without work.
_GATE = "import os,sys; fd=int(sys.argv[1]); ready=os.read(fd,1); os.close(fd); ready==b'1' or sys.exit(125); os.execvp(sys.argv[2],sys.argv[2:])"
_identities: WeakKeyDictionary[asyncio.subprocess.Process, str] = WeakKeyDictionary()


async def process_identity(pid: int) -> str | None:
    process = await asyncio.create_subprocess_exec(
        "ps",
        "-p",
        str(pid),
        "-o",
        "lstart=",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    output, error = await asyncio.wait_for(process.communicate(), 5)
    if process.returncode and (process.returncode != 1 or error.strip() or output.strip()):
        raise CleanupUncertain(f"cannot inspect process {pid}: {error.decode(errors='replace')}")
    return output.decode().strip() or None


async def group_alive(group: int) -> bool:
    process = await asyncio.create_subprocess_exec(
        "ps",
        "-axo",
        "pgid=,stat=",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    output, error = await asyncio.wait_for(process.communicate(), 5)
    if process.returncode:
        raise CleanupUncertain(f"cannot inspect process groups: {error.decode(errors='replace')}")
    for line in output.decode().splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[0] == str(group) and not fields[1].startswith("Z"):
            return True
    return False


async def stop_group(group: int, identity: str, grace: float = 0.5) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        actual = await process_identity(group)
        if actual is not None and actual != identity:
            raise CleanupUncertain(
                f"process identity changed for group {group}; refusing to signal it"
            )
        if not await group_alive(group):
            return
        with suppress(ProcessLookupError):
            os.killpg(group, sig)
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            if not await group_alive(group):
                return
            await asyncio.sleep(0.05)
    if await group_alive(group):
        raise CleanupUncertain(f"exit of process group {group} could not be confirmed")


async def start_process(
    argv: list[str],
    workspace: Path,
    *,
    store: RunStore | None = None,
    run_id: str = "",
    kind: str = "bb",
    deadline_at: float | None = None,
    stdout: int | IO[bytes] = asyncio.subprocess.PIPE,
    stderr: int = asyncio.subprocess.PIPE,
    stdin: int | None = None,
) -> tuple[asyncio.subprocess.Process, str]:
    reader, writer = os.pipe()
    process: asyncio.subprocess.Process | None = None
    process_id = uuid4().hex
    try:
        task = asyncio.create_task(
            asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                _GATE,
                str(reader),
                *argv,
                cwd=workspace,
                stdout=stdout,
                stderr=stderr,
                stdin=stdin,
                limit=8 * 1024 * 1024,
                start_new_session=True,
                pass_fds=(reader,),
            )
        )
        try:
            process = await asyncio.shield(task)
        except asyncio.CancelledError:
            process = await task
            raise
        identity = await process_identity(process.pid)
        if identity is None:
            raise CleanupUncertain("command process exited before registration")
        _identities[process] = identity
        if store is not None:
            await store.register_process(run_id, process_id, kind, process.pid, identity)
            record = await store.get_run(run_id)
            if (
                record is None
                or (record["stop_target"] and kind != "bb_control")
                or (kind == "delivery" and record.get("delivery_cancel_requested"))
            ):
                raise CleanupUncertain("run stopped before command dispatch")
        if deadline_at is not None and time.time() >= deadline_at:
            raise TimeoutError("command time limit exceeded before dispatch")
        os.write(writer, b"1")
        return process, process_id
    except BaseException:
        # Every failure here precedes dispatch. Close the gate so the child
        # exits without executing; inspection failure does not authorize signals.
        os.close(writer)
        writer = -1
        if process is not None:
            await process.wait()
            _identities.pop(process, None)
            if store is not None:
                await store.clear_process(process_id)
        raise
    finally:
        os.close(reader)
        if writer >= 0:
            os.close(writer)


async def finish_process(
    process: asyncio.subprocess.Process, process_id: str, store: RunStore | None = None
) -> None:
    identity = _identities.get(process)
    if identity is None:
        raise CleanupUncertain("command process has no saved identity")
    await stop_group(process.pid, identity)
    await process.wait()
    _identities.pop(process, None)
    if store is not None:
        await store.clear_process(process_id)
