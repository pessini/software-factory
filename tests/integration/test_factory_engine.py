from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest
from langgraph.errors import NodeCancelledError

from software_factory.bb import BBContext
from software_factory.engine import FactoryEngine
from software_factory.git import changed_files
from software_factory.persistence import RunStore
from software_factory.runtime import FactoryRuntime
from tests.conftest import FakeBB, init_repository


def plan_output() -> str:
    return json.dumps(
        {
            "status": "ready",
            "summary": "Write the answer",
            "acceptance_criteria": ["answer.txt contains good"],
            "implementation_steps": ["Create answer.txt"],
        }
    )


def build_output() -> str:
    return json.dumps(
        {
            "status": "completed",
            "summary": "Implemented",
            "changed_files": ["answer.txt"],
        }
    )


def review_output(approved: bool = True) -> str:
    return json.dumps(
        {
            "approved": approved,
            "summary": "Looks good" if approved else "Needs revision",
            "blocking_findings": [] if approved else ["answer must be good"],
        }
    )


def write_answer(value: str):
    def action(workspace: Path) -> None:
        (workspace / "answer.txt").write_text(value, encoding="utf-8")

    return action


def context(tmp_path: Path) -> BBContext:
    storage = tmp_path.parent / f"{tmp_path.name}-thread-storage"
    return BBContext("parent-1", "project-1", "environment-1", storage)


def database(tmp_path: Path) -> Path:
    return context(tmp_path).thread_storage / "factory" / "checkpoints.sqlite"


@pytest.mark.asyncio
async def test_clean_success_path(tmp_path: Path, factory_yaml: str) -> None:
    init_repository(
        tmp_path,
        factory_yaml,
        "from pathlib import Path\nraise SystemExit(Path('answer.txt').read_text() != 'good')\n",
    )
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
    )
    result = await engine.run("Write the correct answer", "fct_success")
    assert result["status"] == "accepted"
    assert result["test_attempt_count"] == 1
    assert result["review_approved"] is True
    assert bb.spawn_counts == {"planner": 1, "builder": 1, "reviewer": 1}
    assert (tmp_path / result["final_report_path"]).exists()


@pytest.mark.asyncio
async def test_failed_test_repairs_in_same_builder_thread(
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
            "planner": [{"output": plan_output()}],
            "builder": [
                {"action": write_answer("bad"), "output": build_output()},
                {"action": write_answer("good"), "output": build_output()},
            ],
            "reviewer": [{"output": review_output()}],
        },
    )
    engine = FactoryEngine(
        workspace=tmp_path,
        context=context(tmp_path),
        bb=bb,
        database_path=database(tmp_path),
    )
    result = await engine.run("Write the correct answer", "fct_repair")
    assert result["status"] == "accepted"
    assert result["test_attempt_count"] == 2
    assert result["test_repair_count"] == 1
    assert bb.spawn_counts["builder"] == 1


@pytest.mark.asyncio
async def test_spawn_is_durable_before_wait(tmp_path: Path, factory_yaml: str) -> None:
    init_repository(tmp_path, factory_yaml, "raise SystemExit(0)\n")
    bb = FakeBB(tmp_path, {"planner": [{"output": plan_output()}]})
    bb.wait_error = RuntimeError("simulated process crash")
    ctx = context(tmp_path)
    store = RunStore(database(tmp_path))
    await store.initialize()
    await store.create_run("fct_crash", tmp_path, ctx.parent_thread_id)
    from software_factory.artifacts import ArtifactStore
    from software_factory.config import load_config

    runtime = FactoryRuntime(
        run_id="fct_crash",
        workspace=tmp_path,
        context=ctx,
        config=load_config(tmp_path),
        bb=bb,
        store=store,
        artifacts=ArtifactStore(
            tmp_path, "fct_crash", storage_root=ctx.thread_storage / "factory" / "artifacts"
        ),
    )
    with pytest.raises(RuntimeError, match="simulated process crash"):
        await runtime.get_or_spawn("planner", "prompt", runtime.config.agents.planner)
    assert await store.child("fct_crash", "planner") == "thread-planner"
    thread_id, _ = await runtime.get_or_spawn("planner", "prompt", runtime.config.agents.planner)
    assert thread_id == "thread-planner"
    assert bb.spawn_counts["planner"] == 1


@pytest.mark.asyncio
async def test_spawn_is_reconciled_when_process_crashes_before_id_is_saved(
    tmp_path: Path, factory_yaml: str
) -> None:
    init_repository(tmp_path, factory_yaml, "raise SystemExit(0)\n")
    bb = FakeBB(tmp_path, {"planner": [{"output": plan_output()}]})
    bb.spawn_error_after_create = RuntimeError("simulated crash after remote spawn")
    ctx = context(tmp_path)
    store = RunStore(database(tmp_path))
    await store.initialize()
    await store.create_run("fct_spawn_window", tmp_path, ctx.parent_thread_id)
    from software_factory.artifacts import ArtifactStore
    from software_factory.config import load_config

    runtime = FactoryRuntime(
        run_id="fct_spawn_window",
        workspace=tmp_path,
        context=ctx,
        config=load_config(tmp_path),
        bb=bb,
        store=store,
        artifacts=ArtifactStore(
            tmp_path, "fct_spawn_window", storage_root=ctx.thread_storage / "factory" / "artifacts"
        ),
    )
    with pytest.raises(RuntimeError, match="simulated crash after remote spawn"):
        await runtime.get_or_spawn("planner", "prompt", runtime.config.agents.planner)

    thread_id, _ = await runtime.get_or_spawn("planner", "prompt", runtime.config.agents.planner)

    assert thread_id == "thread-planner"
    assert bb.spawn_counts["planner"] == 1


@pytest.mark.asyncio
async def test_graph_resume_reattaches_after_process_interruption(
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
            "planner": [{"output": plan_output()}],
            "builder": [{"action": write_answer("good"), "output": build_output()}],
            "reviewer": [{"output": review_output()}],
        },
    )
    bb.wait_error = asyncio.CancelledError()
    engine = FactoryEngine(
        workspace=tmp_path,
        context=context(tmp_path),
        bb=bb,
        database_path=database(tmp_path),
    )
    with pytest.raises(NodeCancelledError):
        await engine.run("Write the correct answer", "fct_resume")
    result = await engine.resume("fct_resume")
    assert result["status"] == "accepted"
    assert bb.spawn_counts["planner"] == 1


@pytest.mark.asyncio
async def test_protected_change_is_preserved_and_fails_run(
    tmp_path: Path, factory_yaml: str
) -> None:
    init_repository(tmp_path, factory_yaml, "raise SystemExit(0)\n")

    def violate(workspace: Path) -> None:
        (workspace / ".factory" / "factory.yaml").write_text("corrupted", encoding="utf-8")
        (workspace / "answer.txt").write_text("good", encoding="utf-8")

    output = json.dumps(
        {
            "status": "completed",
            "summary": "Implemented",
            "changed_files": [".factory/factory.yaml", "answer.txt"],
        }
    )
    bb = FakeBB(
        tmp_path,
        {
            "planner": [{"output": plan_output()}],
            "builder": [{"action": violate, "output": output}],
        },
    )
    engine = FactoryEngine(
        workspace=tmp_path,
        context=context(tmp_path),
        bb=bb,
        database_path=database(tmp_path),
    )
    result = await engine.run("Write the correct answer", "fct_protected")
    assert result["status"] == "blocked"
    assert "protected paths" in (result["error"] or "")
    assert ".factory/factory.yaml" in await changed_files(tmp_path)


@pytest.mark.asyncio
async def test_planner_blocked_status_is_terminal(tmp_path: Path, factory_yaml: str) -> None:
    init_repository(tmp_path, factory_yaml, "raise SystemExit(0)\n")
    blocked = json.dumps(
        {
            "status": "blocked",
            "summary": "request needs clarification",
            "acceptance_criteria": [],
            "implementation_steps": [],
        }
    )
    engine = FactoryEngine(
        workspace=tmp_path,
        context=context(tmp_path),
        bb=FakeBB(tmp_path, {"planner": [{"output": blocked}]}),
        database_path=database(tmp_path),
    )

    result = await engine.run("Ambiguous request", "fct_blocked")

    assert result["status"] == "blocked"


@pytest.mark.asyncio
async def test_resume_honors_persisted_run_deadline(tmp_path: Path, factory_yaml: str) -> None:
    from software_factory.artifacts import ArtifactStore
    from software_factory.config import load_config

    init_repository(tmp_path, factory_yaml, "raise SystemExit(0)\n")
    ctx = context(tmp_path)
    artifacts = ArtifactStore(
        tmp_path, "fct_expired", storage_root=ctx.thread_storage / "factory" / "artifacts"
    )
    artifacts.write_json("config.json", load_config(tmp_path))
    store = RunStore(database(tmp_path))
    await store.initialize()
    await store.create_run(
        "fct_expired",
        tmp_path,
        ctx.parent_thread_id,
        deadline_at=time.time() - 1,
    )
    engine = FactoryEngine(
        workspace=tmp_path,
        context=ctx,
        bb=FakeBB(tmp_path, {}),
        database_path=database(tmp_path),
    )

    with pytest.raises(TimeoutError, match="total run timeout exceeded"):
        await engine.resume("fct_expired")

    record = await store.get_run("fct_expired")
    assert record is not None
    assert record["status"] == "failed"
