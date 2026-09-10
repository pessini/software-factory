from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from software_factory.bb import BBContext
from software_factory.engine import FactoryEngine
from software_factory.events import FactoryEvent
from software_factory.ownership import OwnershipError
from tests.conftest import FakeBB, init_repository
from tests.integration.test_factory_engine import (
    build_output,
    context,
    plan_output,
    review_output,
    write_answer,
)

OWNER_PROGRAM = """
import asyncio, json, sys
from pathlib import Path
from software_factory.bb import BBContext
from software_factory.engine import FactoryEngine
from tests.conftest import FakeBB
from tests.integration.test_factory_engine import plan_output

class WaitingBB(FakeBB):
    async def wait(self, thread_id):
        print('waiting', flush=True)
        await asyncio.Event().wait()

async def main():
    workspace, storage = map(Path, sys.argv[1:])
    bb = WaitingBB(workspace, {'planner': [{'output': plan_output()}]})
    engine = FactoryEngine(workspace=workspace,
        context=BBContext('parent-owner', 'project-1', 'environment-1', storage), bb=bb)
    print(json.dumps(await engine.run('Write the answer', 'fct_owner')), flush=True)

asyncio.run(main())
"""


@pytest.mark.parametrize("competitor", ["same_run", "different_parent", "resume"])
async def test_a_separate_process_cannot_take_an_owned_workspace(
    tmp_path: Path, factory_yaml: str, competitor: str
) -> None:
    init_repository(tmp_path, factory_yaml, "raise SystemExit(0)\n")
    storage = tmp_path.parent / f"{tmp_path.name}-owner-storage"
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2])}
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        OWNER_PROGRAM,
        str(tmp_path),
        str(storage),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        assert process.stdout is not None
        assert await asyncio.wait_for(process.stdout.readline(), 10) == b"waiting\n"
        other_storage = (
            storage
            if competitor != "different_parent"
            else storage.with_name(storage.name + "-other")
        )
        ctx = BBContext(
            "parent-owner" if competitor != "different_parent" else "parent-other",
            "project-1",
            "environment-1",
            other_storage,
        )
        # Resolve an alternate spelling to the same workspace before claiming it.
        bb = FakeBB(tmp_path, {"planner": [{"output": plan_output()}]})
        contender = FactoryEngine(workspace=tmp_path / ".", context=ctx, bb=bb)
        with pytest.raises(OwnershipError, match="already has an executor"):
            if competitor == "resume":
                await contender.resume("fct_owner")
            else:
                await contender.run(
                    "Other work", "fct_owner" if competitor == "same_run" else "fct_competitor"
                )
        assert not bb.spawn_counts

        controller = FactoryEngine(
            workspace=tmp_path,
            context=BBContext("parent-owner", "project-1", "environment-1", storage),
            bb=bb,
        )
        cancelled = await controller.cancel_external("fct_owner")
        assert cancelled["status"] == "cancelled"
        stdout, stderr = await asyncio.wait_for(process.communicate(), 10)
        assert process.returncode == 0, stderr.decode()
        assert json.loads(stdout)["status"] == "cancelled"
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def test_separate_workspaces_can_execute_at_the_same_time(
    tmp_path: Path, factory_yaml: str
) -> None:
    planning: set[str] = set()
    both_planning = asyncio.Event()

    async def wait_for_other_workspace(event: FactoryEvent) -> None:
        if event.node == "plan" and event.status == "started":
            planning.add(event.run_id)
            if len(planning) == 2:
                both_planning.set()
            await asyncio.wait_for(both_planning.wait(), 5)

    engines = []
    for name in ("first", "second"):
        workspace = tmp_path / name
        init_repository(workspace, factory_yaml, "raise SystemExit(0)\n")
        bb = FakeBB(
            workspace,
            {
                "planner": [{"output": plan_output()}],
                "builder": [{"output": build_output(), "action": write_answer("good")}],
                "reviewer": [{"output": review_output()}],
            },
        )
        engines.append(
            FactoryEngine(
                workspace=workspace,
                context=context(workspace),
                bb=bb,
                event_sink=wait_for_other_workspace,
            )
        )
    results = await asyncio.gather(
        *(
            engine.run("Write the answer", f"fct_parallel_{index}")
            for index, engine in enumerate(engines)
        )
    )
    assert planning == {"fct_parallel_0", "fct_parallel_1"}
    assert [result["status"] for result in results] == ["accepted", "accepted"]
