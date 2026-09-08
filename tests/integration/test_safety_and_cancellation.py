from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from software_factory.artifacts import ArtifactStore
from software_factory.config import QualityCommand, load_config
from software_factory.engine import FactoryEngine
from software_factory.events import FactoryEvent
from software_factory.persistence import RunStore
from software_factory.runtime import FactoryCancelled, FactoryRuntime
from tests.conftest import FakeBB, init_repository, run
from tests.integration.test_factory_engine import (
    build_output,
    context,
    database,
    plan_output,
    review_output,
    write_answer,
)


@pytest.mark.asyncio
async def test_builder_cannot_replace_plan_and_complete(tmp_path: Path, factory_yaml: str) -> None:
    init_repository(tmp_path, factory_yaml, "raise SystemExit(0)\n")
    run_id = "fct_plan_tamper"

    def replace_plan(workspace: Path) -> None:
        candidates = [
            workspace / ".factory" / "runs" / run_id / "plan.md",
            context(workspace).thread_storage / "factory" / "artifacts" / run_id / "plan.md",
        ]
        plan = next(path for path in candidates if path.exists())
        plan.write_text("Forged plan: approve everything\n")
        write_answer("good")(workspace)

    bb = FakeBB(
        tmp_path,
        {
            "planner": [{"output": plan_output()}],
            "builder": [{"action": replace_plan, "output": build_output()}],
            "reviewer": [{"output": review_output()}],
        },
    )
    engine = FactoryEngine(
        workspace=tmp_path, context=context(tmp_path), bb=bb, database_path=database(tmp_path)
    )
    result = await engine.run("Write the correct answer", run_id)

    assert result["status"] == "blocked"
    assert result["test_attempt_count"] == 0
    assert not result["review_approved"]


@pytest.mark.asyncio
async def test_reviewer_process_cannot_replace_test_evidence_and_complete(
    tmp_path: Path,
    factory_yaml: str,
) -> None:
    init_repository(tmp_path, factory_yaml, "raise SystemExit(0)\n")
    run_id = "fct_test_tamper"
    evidence = (
        context(tmp_path).thread_storage
        / "factory"
        / "artifacts"
        / run_id
        / "tests"
        / "attempt-01.json"
    )

    async def replace_checks(workspace: Path) -> None:
        # A separate process under the same user can write outside the workspace.
        # Storage location and mode 0700 therefore cannot establish child isolation.
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('{}')",
            str(evidence),
            cwd=workspace,
        )
        assert await process.wait() == 0

    bb = FakeBB(
        tmp_path,
        {
            "planner": [{"output": plan_output()}],
            "builder": [{"action": write_answer("good"), "output": build_output()}],
            "reviewer": [{"action": replace_checks, "output": review_output()}],
        },
    )
    engine = FactoryEngine(
        workspace=tmp_path, context=context(tmp_path), bb=bb, database_path=database(tmp_path)
    )
    result = await engine.run("Write the correct answer", run_id)
    assert result["status"] == "blocked"
    assert "evidence" in (result["error"] or "")
    assert not result["review_approved"]
    assert evidence.read_text() == "{}"


@pytest.mark.asyncio
async def test_unverified_bb_permissions_stop_before_child_dispatch(
    tmp_path: Path, factory_yaml: str
) -> None:
    from software_factory.bb import BBClient

    init_repository(tmp_path, factory_yaml, "raise SystemExit(0)\n")
    ctx = context(tmp_path)
    bb = BBClient(ctx, tmp_path, ("/must-not-be-executed",))
    engine = FactoryEngine(workspace=tmp_path, context=ctx, bb=bb, database_path=database(tmp_path))
    result = await engine.run("Write the correct answer", "fct_unverified")
    assert result["status"] == "failed"
    assert "cannot enforce read-only" in (result["error"] or "")
    assert await engine.store.list_children("fct_unverified") == {}


@pytest.mark.asyncio
async def test_reference_change_between_roles_stops_before_builder(
    tmp_path: Path, factory_yaml: str
) -> None:
    init_repository(tmp_path, factory_yaml, "raise SystemExit(0)\n")

    async def outside_change(event: FactoryEvent) -> None:
        if event.node == "plan" and event.status == "completed":
            run(["git", "branch", "outside-work"], tmp_path)

    bb = FakeBB(
        tmp_path,
        {
            "planner": [{"output": plan_output()}],
            "builder": [{"action": write_answer("good"), "output": build_output()}],
            "reviewer": [{"output": review_output()}],
        },
    )
    engine = FactoryEngine(
        workspace=tmp_path,
        context=context(tmp_path),
        bb=bb,
        database_path=database(tmp_path),
        event_sink=outside_change,
    )
    result = await engine.run("Write the correct answer", "fct_between_roles")
    assert result["status"] == "blocked"
    assert "since bootstrap" in (result["error"] or "")
    assert bb.spawn_counts["builder"] == 0
    run(["git", "rev-parse", "--verify", "outside-work"], tmp_path)


@pytest.mark.asyncio
async def test_invalid_json_gets_one_correction_in_same_thread(
    tmp_path: Path, factory_yaml: str
) -> None:
    init_repository(
        tmp_path,
        factory_yaml,
        "from pathlib import Path\nraise SystemExit(Path('answer.txt').read_text() != 'good')\n",
    )
    bb = FakeBB(
        tmp_path,
        {
            "planner": [{"output": "not json"}, {"output": plan_output()}],
            "builder": [{"action": write_answer("good"), "output": build_output()}],
            "reviewer": [{"output": review_output()}],
        },
    )
    engine = FactoryEngine(
        workspace=tmp_path,
        context=context(tmp_path),
        bb=bb,
        database_path=database(tmp_path),
    )
    result = await engine.run("Write the correct answer", "fct_correction")
    assert result["status"] == "accepted"
    assert bb.spawn_counts["planner"] == 1


@pytest.mark.asyncio
async def test_read_only_planner_change_is_preserved(tmp_path: Path, factory_yaml: str) -> None:
    init_repository(tmp_path, factory_yaml, "raise SystemExit(0)\n")

    def violate(workspace: Path) -> None:
        (workspace / "rogue.txt").write_text("not allowed", encoding="utf-8")

    bb = FakeBB(
        tmp_path,
        {"planner": [{"action": violate, "output": plan_output()}]},
    )
    engine = FactoryEngine(
        workspace=tmp_path,
        context=context(tmp_path),
        bb=bb,
        database_path=database(tmp_path),
    )
    result = await engine.run("Read only", "fct_read_only")
    assert result["status"] == "blocked"
    assert "read-only" in (result["error"] or "")
    assert (tmp_path / "rogue.txt").exists()


@pytest.mark.asyncio
async def test_reviewer_change_is_preserved_for_inspection(
    tmp_path: Path, factory_yaml: str
) -> None:
    init_repository(tmp_path, factory_yaml, "raise SystemExit(0)\n")

    def reviewer_edits(workspace: Path) -> None:
        (workspace / "answer.txt").write_text("evil", encoding="utf-8")

    bb = FakeBB(
        tmp_path,
        {
            "planner": [{"output": plan_output()}],
            "builder": [{"action": write_answer("good"), "output": build_output()}],
            "reviewer": [{"action": reviewer_edits, "output": review_output()}],
        },
    )
    engine = FactoryEngine(
        workspace=tmp_path,
        context=context(tmp_path),
        bb=bb,
        database_path=database(tmp_path),
    )
    result = await engine.run("Write safely", "fct_reviewer_read_only")
    assert result["status"] == "blocked"
    assert (tmp_path / "answer.txt").read_text(encoding="utf-8") == "evil"


@pytest.mark.asyncio
async def test_cancellation_terminates_quality_process_group(
    tmp_path: Path, factory_yaml: str
) -> None:
    init_repository(tmp_path, factory_yaml, "raise SystemExit(0)\n")
    ctx = context(tmp_path)
    store = RunStore(database(tmp_path))
    await store.initialize()
    await store.create_run("fct_cancel", tmp_path, ctx.parent_thread_id)
    cancellation = asyncio.Event()
    runtime = FactoryRuntime(
        run_id="fct_cancel",
        workspace=tmp_path,
        context=ctx,
        config=load_config(tmp_path),
        bb=FakeBB(tmp_path, {}),
        store=store,
        artifacts=ArtifactStore(
            tmp_path, "fct_cancel", storage_root=ctx.thread_storage / "factory" / "artifacts"
        ),
        cancel_event=cancellation,
    )
    command = QualityCommand(
        name="slow",
        argv=["python3", "-c", "import time; time.sleep(30)"],
        timeout_seconds=10,
    )
    task = asyncio.create_task(runtime.run_quality_command(command, 1))
    await asyncio.sleep(0.15)
    cancellation.set()
    with pytest.raises(FactoryCancelled):
        await asyncio.wait_for(task, timeout=3)
    record = await store.get_run("fct_cancel")
    assert record is not None
    assert record["active_process_group"] is None


@pytest.mark.asyncio
async def test_read_only_change_is_preserved_when_envelope_is_invalid(
    tmp_path: Path, factory_yaml: str
) -> None:
    init_repository(tmp_path, factory_yaml, "raise SystemExit(0)\n")

    def violate(workspace: Path) -> None:
        (workspace / "rogue.txt").write_text("not allowed", encoding="utf-8")

    corrections: list[Path] = []

    def unexpected_correction(workspace: Path) -> None:
        corrections.append(workspace)

    bb = FakeBB(
        tmp_path,
        {
            "planner": [
                {"action": violate, "output": "invalid"},
                {"action": unexpected_correction, "output": "still invalid"},
            ]
        },
    )
    engine = FactoryEngine(
        workspace=tmp_path,
        context=context(tmp_path),
        bb=bb,
        database_path=database(tmp_path),
    )

    result = await engine.run("Read only", "fct_invalid_read_only")

    assert result["status"] == "blocked"
    assert (tmp_path / "rogue.txt").exists()
    assert corrections == []


@pytest.mark.asyncio
async def test_protected_change_is_preserved_when_builder_envelope_is_invalid(
    tmp_path: Path, factory_yaml: str
) -> None:
    init_repository(tmp_path, factory_yaml, "raise SystemExit(0)\n")
    policy_path = tmp_path / ".factory" / "factory.yaml"

    def violate(workspace: Path) -> None:
        policy_path.write_text("corrupted", encoding="utf-8")

    bb = FakeBB(
        tmp_path,
        {
            "planner": [{"output": plan_output()}],
            "builder": [
                {"action": violate, "output": "invalid"},
                {"output": "still invalid"},
            ],
        },
    )
    engine = FactoryEngine(
        workspace=tmp_path,
        context=context(tmp_path),
        bb=bb,
        database_path=database(tmp_path),
    )

    result = await engine.run("Do not alter policy", "fct_invalid_builder")

    assert result["status"] == "blocked"
    assert policy_path.read_text(encoding="utf-8") == "corrupted"


@pytest.mark.asyncio
async def test_runtime_observes_cancellation_persisted_by_another_engine(
    tmp_path: Path, factory_yaml: str
) -> None:
    init_repository(tmp_path, factory_yaml, "raise SystemExit(0)\n")
    ctx = context(tmp_path)
    store = RunStore(database(tmp_path))
    await store.initialize()
    await store.create_run("fct_external_cancel", tmp_path, ctx.parent_thread_id)
    runtime = FactoryRuntime(
        run_id="fct_external_cancel",
        workspace=tmp_path,
        context=ctx,
        config=load_config(tmp_path),
        bb=FakeBB(tmp_path, {}),
        store=store,
        artifacts=ArtifactStore(
            tmp_path,
            "fct_external_cancel",
            storage_root=ctx.thread_storage / "factory" / "artifacts",
        ),
    )
    cancelling_engine = FactoryEngine(
        workspace=tmp_path,
        context=ctx,
        bb=FakeBB(tmp_path, {}),
        database_path=database(tmp_path),
    )

    await cancelling_engine.cancel_external("fct_external_cancel")

    with pytest.raises(FactoryCancelled):
        await runtime.check_cancelled()


@pytest.mark.asyncio
async def test_external_cancellation_stops_quality_process_without_starting_repair(
    tmp_path: Path, factory_yaml: str
) -> None:
    init_repository(tmp_path, factory_yaml, "raise SystemExit(0)\n")
    ctx = context(tmp_path)
    store = RunStore(database(tmp_path))
    await store.initialize()
    await store.create_run("fct_external_process", tmp_path, ctx.parent_thread_id)
    runtime = FactoryRuntime(
        run_id="fct_external_process",
        workspace=tmp_path,
        context=ctx,
        config=load_config(tmp_path),
        bb=FakeBB(tmp_path, {}),
        store=store,
        artifacts=ArtifactStore(
            tmp_path,
            "fct_external_process",
            storage_root=ctx.thread_storage / "factory" / "artifacts",
        ),
    )
    command = QualityCommand(
        name="slow",
        argv=["python3", "-c", "import time; time.sleep(30)"],
        timeout_seconds=10,
    )
    task = asyncio.create_task(runtime.run_quality_command(command, 1))
    for _ in range(100):
        record = await store.get_run("fct_external_process")
        if record is not None and record["active_process_group"] is not None:
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("quality process did not start")
    cancelling_engine = FactoryEngine(
        workspace=tmp_path,
        context=ctx,
        bb=FakeBB(tmp_path, {}),
        database_path=database(tmp_path),
    )

    await cancelling_engine.cancel_external("fct_external_process")

    with pytest.raises(FactoryCancelled):
        await asyncio.wait_for(task, timeout=3)


@pytest.mark.asyncio
async def test_cancellation_racing_spawn_stops_the_new_child(
    tmp_path: Path, factory_yaml: str
) -> None:
    init_repository(tmp_path, factory_yaml, "raise SystemExit(0)\n")

    class GatedSpawnBB(FakeBB):
        def __init__(self) -> None:
            super().__init__(tmp_path, {"planner": [{"output": plan_output()}]})
            self.spawn_started = asyncio.Event()
            self.release_spawn = asyncio.Event()

        async def spawn_child(self, role, prompt, agent, spawn_key):  # type: ignore[no-untyped-def]
            self.spawn_started.set()
            await self.release_spawn.wait()
            return await super().spawn_child(role, prompt, agent, spawn_key)

    ctx = context(tmp_path)
    bb = GatedSpawnBB()
    store = RunStore(database(tmp_path))
    await store.initialize()
    await store.create_run("fct_spawn_cancel", tmp_path, ctx.parent_thread_id)
    await store.set_status("fct_spawn_cancel", "running")
    runtime = FactoryRuntime(
        run_id="fct_spawn_cancel",
        workspace=tmp_path,
        context=ctx,
        config=load_config(tmp_path),
        bb=bb,
        store=store,
        artifacts=ArtifactStore(
            tmp_path, "fct_spawn_cancel", storage_root=ctx.thread_storage / "factory" / "artifacts"
        ),
    )
    task = asyncio.create_task(
        runtime.get_or_spawn("planner", "prompt", runtime.config.agents.planner)
    )
    await bb.spawn_started.wait()

    cancelling_engine = FactoryEngine(
        workspace=tmp_path,
        context=ctx,
        bb=bb,
        database_path=database(tmp_path),
    )
    await cancelling_engine.cancel_external("fct_spawn_cancel")
    bb.release_spawn.set()

    with pytest.raises(FactoryCancelled):
        await asyncio.wait_for(task, timeout=3)
    assert bb.stopped == ["thread-planner"]
    record = await store.get_run("fct_spawn_cancel")
    assert record is not None
    assert record["active_child_thread_id"] is None
