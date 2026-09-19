"""Launch a process that survives client/event-loop exit, after saving its identity."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

from software_factory.processes import process_identity

_GATE = "import os,sys; fd=int(sys.argv[1]); ready=os.read(fd,1); os.close(fd); ready==b'1' or sys.exit(125); os.execvp(sys.argv[2],sys.argv[2:])"


async def launch_detached(
    argv: list[str], cwd: Path, register: Callable[[int, str], Awaitable[None]]
) -> None:
    reader, writer = os.pipe()
    try:
        # asyncio subprocess transports terminate children when their loop is destroyed.
        process = subprocess.Popen(  # noqa: ASYNC220 - creation only; no blocking wait
            [sys.executable, "-c", _GATE, str(reader), *argv],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            pass_fds=(reader,),
            start_new_session=True,
        )
        identity = await process_identity(process.pid)
        if identity is None:
            raise RuntimeError("process exited before registration")
        await register(process.pid, identity)
        os.write(writer, b"1")
    finally:
        os.close(reader)
        os.close(writer)
