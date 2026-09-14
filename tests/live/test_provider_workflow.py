"""Opt-in acceptance against local BB and its authenticated protected worker.

Each case gets a separate demo clone and BB parent. See docs/acceptance.md.
Faults are introduced at the BB API boundary; provider responses remain real.
"""

import asyncio
import hashlib
import json
import os
import platform
import shutil
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import pytest
from langgraph.errors import NodeCancelledError

from software_factory.artifacts import ArtifactStore
from software_factory.bb import BBContext
from software_factory.bb.client import BBError, resolve_bb_command
from software_factory.bb.local_api import thread_storage
from software_factory.bb.protected import ProtectedBBClient
from software_factory.config import ModelChoice, Selection
from software_factory.engine import FactoryEngine
from software_factory.evidence import RunEvidence
from software_factory.processes import finish_process, start_process
from software_factory.worker_container import WORKER_IMAGE, container_command
from software_factory.worker_records import WorkerRecords
from tests.live.review_cases import REVIEW_CASES, verify_review_case

pytestmark = pytest.mark.skipif(
    os.environ.get("FACTORY_LIVE_PROVIDER") != "1",
    reason="requires explicit opt-in, local BB, Apple Container, and a provider login",
)

FEATURE = (
    "Add is_even(value: int) -> bool to demo_target. Return True for even positive, "
    "negative and zero integers and False for odd integers. Export it from "
    "demo_target.__init__. Add behavior tests. Preserve greeting and factory policy. "
    "Do not commit or push."
)
REQUESTS = {
    "feature": FEATURE,
    "no_change": (
        "Verify the existing greeting function returns 'Hello, Ada!' for 'Ada' and "
        "'Hello, !' for an empty string. It is already implemented. Verify without "
        "changing any files; the existing tests and direct inspection are sufficient."
    ),
    "repair": FEATURE
    + (
        " This is a deliberate repair acceptance exercise in a disposable clone. "
        "On the FIRST Builder turn implement value >= 0 and value % 2 == 0, with "
        "tests for negative even numbers that fail. Return that initial build "
        "without fixing it. On the controller's Repair turn fix the negative case. "
        "The final completion criteria require correct behavior for all integers."
    ),
    "revision": FEATURE
    + (
        " This is a deliberate review acceptance exercise in a disposable clone. "
        "On the FIRST Builder turn implement and test the function in core.py, "
        "importing directly from core in tests, but omit the package export. "
        "The Reviewer must report the missing required export as a blocking defect. "
        "Only on the controller's Revision turn add the export and test it. "
        "Final completion still requires the public package export."
    ),
    "question_restart": (
        "Add format_label(name: str) -> str and export it. It must prepend an exact "
        "operator-selected prefix to name. The prefix is deliberately unspecified. "
        "Planner must ask exactly one blocking question with id 'prefix', offering "
        "'Item: ' and 'Name: ', before planning implementation. Do not guess. "
        "After the answer, implement it and test normal and empty names."
    ),
}
SCENARIOS = (
    "feature",
    "no_change",
    "repair",
    "revision",
    "restart_after_dispatch",
    "restart_after_result",
    "connection_loss",
    "question_restart",
    "cancel",
    *REVIEW_CASES,
)


async def bb_command(*args):
    process, identity = await start_process([*resolve_bb_command(), *args, "--json"], Path.cwd())
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), 60)
    finally:
        await asyncio.shield(finish_process(process, identity))
    if process.returncode:
        raise RuntimeError(stderr.decode() or stdout.decode())
    return json.loads(stdout)


def git(workspace, *args):
    return subprocess.check_output(["git", *args], cwd=workspace, text=True).strip()


def source_identity(factory):
    return {
        "factory_commit": git(factory, "rev-parse", "HEAD"),
        "factory_diff_sha256": hashlib.sha256(git(factory, "diff", "HEAD").encode()).hexdigest(),
    }


@pytest.fixture(scope="module")
def acceptance_source():
    # Record once: later cases still use the modules loaded by this test process.
    return source_identity(Path(__file__).resolve().parents[2])


class InterruptedBB(ProtectedBBClient):
    """Lose only a transport response, never substitute a provider result."""

    def __init__(self, context, workspace, scenario):
        super().__init__(context, workspace, executable=resolve_bb_command())
        self.scenario = scenario
        self.dispatched = asyncio.Event()
        self.injected = False
        self.observed = None

    async def spawn_child(self, role, prompt, agent, spawn_key):
        child = await super().spawn_child(role, prompt, agent, spawn_key)
        if not self.injected and self.scenario in {
            "restart_after_dispatch",
            "connection_loss",
            "cancel",
        }:
            records = WorkerRecords(await thread_storage(child))
            async with asyncio.timeout(90):
                while self.observed is None:
                    for receipt in records.all():
                        if receipt.execution_id and receipt.status == "running":
                            self.observed = {"thread_id": child, **receipt.model_dump(mode="json")}
                            break
                        if receipt.status in {"failed", "stopped"}:
                            raise RuntimeError(receipt.error or "provider did not start")
                    if self.observed is None:
                        await asyncio.sleep(0.1)
        self.dispatched.set()
        if not self.injected and self.scenario in {"restart_after_dispatch", "connection_loss"}:
            self.injected = True
            if self.scenario == "restart_after_dispatch":
                raise asyncio.CancelledError()
            raise BBError("acceptance: lost response after actual BB creation", "connection")
        return child


@pytest.mark.parametrize("scenario", SCENARIOS)
async def test_real_provider_workflow(tmp_path, scenario, acceptance_source):
    project = os.environ["FACTORY_ACCEPTANCE_PROJECT"]
    model = os.environ["FACTORY_ACCEPTANCE_MODEL"]
    output = Path(os.environ["FACTORY_ACCEPTANCE_OUTPUT"]).resolve()
    output.mkdir(parents=True, exist_ok=True)
    case_id = f"{scenario}-{uuid4().hex[:8]}"
    review_case = REVIEW_CASES.get(scenario)
    started_at = time.time()
    record = {
        **acceptance_source,
        "scenario": scenario,
        "result": "incomplete",
        "case_id": case_id,
        "started_at": started_at,
        "review_case": review_case,
        "platform": platform.platform(),
        "python": platform.python_version(),
        "worker_image": WORKER_IMAGE,
        "provider_cli": "0.154.0 (checked by the worker initialize response)",
        "fault_injection": "BB API boundary; provider responses are real"
        if scenario in {"restart_after_dispatch", "restart_after_result", "connection_loss"}
        else "deliberately incomplete first build"
        if scenario in {"repair", "revision", "focused_revision"}
        else None,
    }
    workspace = tmp_path / "demo"
    seed = tmp_path / "seed"
    source = Path(__file__).resolve().parents[2] / "examples" / "demo-target"
    shutil.copytree(
        source, seed, ignore=shutil.ignore_patterns(".venv", "__pycache__", ".pytest_cache")
    )
    with (seed / ".gitignore").open("a") as handle:
        handle.write("\n.env\n")
    if review_case:
        with (seed / ".factory/factory.yaml").open("a") as handle:
            handle.write(f"\nreview_mode: {review_case['mode']}\n")
    git(seed, "init", "-q")
    git(seed, "add", ".")
    git(
        seed,
        "-c",
        "user.name=Factory Acceptance",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-qm",
        "Prepare acceptance demo",
    )
    git(tmp_path, "clone", "--no-hardlinks", str(seed), str(workspace))
    await asyncio.to_thread(subprocess.run, ["uv", "sync", "--frozen"], cwd=workspace, check=True)
    starting_head = git(workspace, "rev-parse", "HEAD")
    outside = tmp_path / "outside-user-work.txt"
    outside.write_text("Unrelated user work must survive.\n")
    ignored = workspace / ".env"
    ignored.write_text("ACCEPTANCE_DUMMY=preserve\n")
    parent = await bb_command(
        "thread",
        "spawn",
        "--project",
        project,
        "--environment",
        str(workspace),
        "--provider",
        "acp-software-factory",
        "--permission-mode",
        "accept-edits",
        "--title",
        f"Issue {1 if review_case else 6} {case_id}",
        "--prompt",
        "/status",
    )
    context = BBContext(
        parent["id"], project, parent["environmentId"], await thread_storage(parent["id"])
    )
    record.update(parent=parent["id"], workspace=str(workspace), starting_commit=starting_head)
    client = InterruptedBB(context, workspace, scenario)
    selection = Selection(
        shared=ModelChoice(provider="codex", model=model, reasoning_level="low"),
        steps={
            "repair": ModelChoice(reasoning_level="medium"),
            "review": ModelChoice(reasoning_level="medium"),
            "revision": ModelChoice(reasoning_level="high"),
            **{
                step: ModelChoice(reasoning_level="medium")
                for step in (
                    "request_review",
                    "code_review",
                    "security_review",
                    "documentation_review",
                    "judge",
                )
            },
        },
    )
    record["requested"] = selection.model_dump(mode="json")
    interrupted = False

    async def events(event):
        nonlocal interrupted
        print(f"{case_id}: {event.node} {event.status}: {event.message}", flush=True)
        if (
            scenario == "restart_after_result"
            and event.node == "plan"
            and event.status == "completed"
            and not interrupted
        ):
            operation = next(
                item
                for item in await engine.store.operations(run_id)
                if item["step"] == "plan" and item["kind"] == "create"
            )
            receipts = WorkerRecords(await thread_storage(operation["thread_id"])).all()
            receipt = next(
                item for item in receipts if item.operation_id == operation["operation_id"]
            )
            client.observed = {
                "thread_id": operation["thread_id"],
                **receipt.model_dump(mode="json"),
            }
            interrupted = True
            raise asyncio.CancelledError()

    engine = FactoryEngine(workspace=workspace, context=context, bb=client, event_sink=events)
    run_id = "fct_live_" + uuid4().hex[:12]
    record["run_id"] = run_id
    task = None
    try:
        assert source_identity(Path(__file__).resolve().parents[2]) == acceptance_source, (
            "factory source changed before this live case"
        )
        record["bb_version"] = await bb_command("settings", "version")
        record["image"] = json.loads(await container_command("image", "inspect", WORKER_IMAGE))
        record["container_version"] = (await container_command("--version")).strip()
        record["catalog"] = [item.model_dump() for item in await client.model_catalog()]
        await client.check_protection(context.thread_storage / "factory" / "artifacts")
        async with asyncio.timeout(1800):
            task = asyncio.create_task(
                engine.run(
                    review_case["request"] if review_case else REQUESTS.get(scenario, FEATURE),
                    run_id,
                    selection=selection,
                )
            )
            if scenario == "cancel":
                await client.dispatched.wait()
                state = await FactoryEngine(workspace=workspace, context=context).cancel_external(
                    run_id
                )
                await task
            elif scenario.startswith("restart_"):
                with pytest.raises((NodeCancelledError, asyncio.CancelledError)):
                    await task
                engine = FactoryEngine(workspace=workspace, context=context, event_sink=events)
                state = await engine.resume(run_id)
            else:
                state = await task
            if scenario == "question_restart":
                assert state["status"] == "waiting_for_input", state.get("error")
                interaction = state["interactions"][0]
                assert interaction["questions"][0]["id"] == "prefix"
                engine = FactoryEngine(workspace=workspace, context=context, event_sink=events)
                assert (await engine.resume(run_id))["status"] == "waiting_for_input"
                state = await engine.answer(
                    run_id, interaction["interaction_id"], {"prefix": "Item: "}
                )
            record["before_interruption"] = client.observed
            record["state"] = state
            assert state["status"] == (
                "cancelled" if scenario == "cancel" else "ready_for_review"
            ), state.get("error")
            assert git(workspace, "rev-parse", "HEAD") == starting_head
            assert not git(workspace, "diff", "--cached", "--name-only")
            assert outside.read_text() == "Unrelated user work must survive.\n"
            assert ignored.read_text() == "ACCEPTANCE_DUMMY=preserve\n"
            if scenario != "cancel":
                artifacts = ArtifactStore(
                    workspace,
                    run_id,
                    storage_root=context.thread_storage / "factory" / "artifacts",
                    create=False,
                )
                current = await RunEvidence(workspace, artifacts).current()
                assert current["content_version"] == state["handoff"]["content_version"]
                assert state["content_version"] in Path(state["final_report_path"]).read_text()
                assert state["test_passed"] and state["review_approved"]
                for step in state["execution_summary"]["steps"].values():
                    if step["dispatch_count"]:
                        assert step["effective_settings"] == "verified"
            if scenario == "no_change":
                assert state["changed_files"] == []
                assert not git(workspace, "status", "--porcelain")
            if scenario == "repair":
                assert state["test_repair_count"] >= 1
            if scenario == "revision":
                assert state["review_revision_count"] >= 1
                assert any(
                    item["status"] == "fixed" for item in state["handoff"]["findings"].values()
                )
            if review_case:
                verify_review_case(workspace, state, review_case)
                record["quality"] = await engine.review_quality(run_id)
                assert record["quality"] == (
                    await FactoryEngine(workspace=workspace, context=context).review_quality(run_id)
                )
                record["artifacts_root"] = str(artifacts.root)
            operations = await engine.store.operations(run_id)
            record["operations"] = operations
            assert all(item["dispatch_count"] == 1 for item in operations)
            children = await engine.store.list_children(run_id)
            actual_children = await bb_command("thread", "list", "--parent-thread", parent["id"])
            owned_children = set(children.values()) | {
                item["thread_id"] for item in operations if item["thread_id"]
            }
            if client.observed:
                # Cancellation can precede the role binding after a real dispatch.
                owned_children.add(client.observed["thread_id"])
            assert {item["id"] for item in actual_children} == owned_children
            record["workers"] = []
            for child_id in owned_children:
                assert (await client.inspect_thread(child_id)).stopped
                receipts = WorkerRecords(await thread_storage(child_id)).all()
                record["workers"].extend(item.model_dump(mode="json") for item in receipts)
            provider_operations = [item for item in operations if item["kind"] != "check"]
            assert sorted(item["operation_id"] for item in record["workers"]) == sorted(
                item["operation_id"] for item in provider_operations
            )
            if client.observed:
                before = client.observed
                same_request = [
                    item
                    for item in record["workers"]
                    if item["operation_id"] == before["operation_id"]
                ]
                assert len(same_request) == 1
                assert same_request[0]["execution_id"] == before["execution_id"]
                assert same_request[0]["stopped"]
                if scenario != "cancel":
                    assert children["planner"] == before["thread_id"]
                assert (
                    len(
                        [
                            item
                            for item in provider_operations
                            if item["step"] == "plan" and item["kind"] == "create"
                        ]
                    )
                    == 1
                )
            assert source_identity(Path(__file__).resolve().parents[2]) == acceptance_source, (
                "factory source changed during live acceptance"
            )
            record["source_verified_at_end"] = True
            record["result"] = "passed"
    except BaseException as exc:
        record["result"] = "failed"
        record["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        if record["result"] != "passed":
            try:
                record["cleanup"] = await asyncio.wait_for(engine.cancel_external(run_id), 60)
            except Exception as exc:
                record["cleanup_error"] = str(exc)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        record["finished_at"] = time.time()
        record["duration_seconds"] = record["finished_at"] - started_at
        (output / f"{case_id}.json").write_text(json.dumps(record, indent=2, default=str))
