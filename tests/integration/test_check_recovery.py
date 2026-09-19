from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from contextlib import suppress
from pathlib import Path

import pytest

from software_factory.engine import FactoryEngine
from software_factory.ownership import OwnershipError
from software_factory.persistence import RunStore
from tests.conftest import FakeBB, init_repository
from tests.integration.test_factory_engine import context, database, review_output

CONTROLLER_PROGRAM = """
import asyncio, os, sys
from pathlib import Path
from software_factory.engine import FactoryEngine
from tests.conftest import FakeBB
from tests.integration.test_factory_engine import (
    context, database, plan_output, build_output, write_answer,
)
async def main():
    workspace = Path(sys.argv[1])
    os.environ['CONTROLLER_PID'] = str(os.getpid())
    bb = FakeBB(workspace, {
        'planner': [{'output': plan_output()}],
        'builder': [{'output': build_output(), 'action': write_answer('good')}],
    })
    engine = FactoryEngine(workspace=workspace, context=context(workspace), execution=bb,
        database_path=database(workspace))
    await engine.run('Write the answer', 'fct_check_crash')
asyncio.run(main())
"""


@pytest.mark.parametrize("repeat_allowed,expired", [(False, False), (True, False), (True, True)])
async def test_check_repeats_after_controller_crash_only_under_its_saved_rule(
    tmp_path: Path, factory_yaml: str, repeat_allowed: bool, expired: bool
) -> None:
    counter = tmp_path.parent / f"{tmp_path.name}-check-count"
    check_script = f"""import os, signal, time
from pathlib import Path
if not Path("answer.txt").exists():
    raise SystemExit(0)
counter = Path({str(counter)!r})
attempt = int(counter.read_text()) + 1 if counter.exists() else 1
counter.write_text(str(attempt))
if attempt == 1:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    os.kill(int(os.environ['CONTROLLER_PID']), signal.SIGKILL)
    time.sleep(30)
"""
    config = factory_yaml.replace(
        "timeout_seconds: 10",
        f"timeout_seconds: {1 if expired else 10}\n      repeat_on_recovery: {str(repeat_allowed).lower()}",
    )
    init_repository(tmp_path, config, check_script)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        CONTROLLER_PROGRAM,
        str(tmp_path),
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2])},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    store = RunStore(database(tmp_path))
    groups: list[int] = []
    try:
        _, error = await asyncio.wait_for(process.communicate(), 10)
        assert process.returncode == -signal.SIGKILL, error.decode()
        groups = [record["process_group"] for record in await store.processes("fct_check_crash")]
        assert groups
        before = next(
            op for op in await store.operations("fct_check_crash") if op["step"] == "test:tests"
        )
        if expired:
            await asyncio.sleep(max(0, before["deadline_at"] - time.time()) + 0.05)
        contender = FactoryEngine(
            workspace=tmp_path,
            context=context(tmp_path),
            execution=FakeBB(tmp_path, {}),
            database_path=database(tmp_path).with_name("other.sqlite"),
        )
        with pytest.raises(OwnershipError, match="reserved for recovery"):
            await contender.run("Competing work", "fct_takeover")
        bb = FakeBB(tmp_path, {"reviewer": [{"output": review_output()}]})
        engine = FactoryEngine(
            workspace=tmp_path,
            context=context(tmp_path),
            execution=bb,
            database_path=database(tmp_path),
        )
        result = await engine.resume("fct_check_crash")
        expected_status = (
            "blocked" if expired else "ready_for_review" if repeat_allowed else "recovery_needed"
        )
        assert result["status"] == expected_status
        assert counter.read_text() == ("2" if repeat_allowed and not expired else "1")
        assert not bb.spawn_counts.get("planner") and not bb.spawn_counts.get("builder")
        after = next(
            op for op in await store.operations("fct_check_crash") if op["step"] == "test:tests"
        )
        assert after["deadline_at"] == before["deadline_at"]
        assert after["dispatch_count"] == (2 if repeat_allowed and not expired else 1)
        assert await store.processes("fct_check_crash") == []
        for group in groups:
            with pytest.raises(ProcessLookupError):
                os.killpg(group, 0)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        for group in groups:
            with suppress(ProcessLookupError):
                os.killpg(group, signal.SIGKILL)
