"""Issue 9: public control behavior with no application context."""

from pathlib import Path

import pytest

from software_factory.context import FactoryContext
from software_factory.control import FactoryControl
from software_factory.engine import FactoryEngine
from tests.conftest import FakeBB, init_repository
from tests.integration.test_factory_engine import (
    build_output,
    plan_output,
    review_output,
    write_answer,
)


async def test_saved_run_is_independent_of_interface(
    tmp_path: Path, factory_yaml: str, monkeypatch
):
    for name in ("BB_THREAD_ID", "BB_PROJECT_ID", "BB_ENVIRONMENT_ID", "BB_THREAD_STORAGE"):
        monkeypatch.delenv(name, raising=False)
    workspace = tmp_path / "target"
    workspace.mkdir()
    init_repository(
        workspace,
        factory_yaml,
        "from pathlib import Path\nassert Path('answer.txt').read_text() == 'good'\n",
    )
    context = FactoryContext.open(workspace, storage_root=tmp_path / "controller")
    execution = FakeBB(
        workspace,
        {
            "planner": [{"output": plan_output()}],
            "builder": [{"output": build_output(), "action": write_answer("good")}],
            "reviewer": [{"output": review_output()}],
        },
    )
    engine = FactoryEngine(workspace=workspace, context=context, execution=execution)
    result = await engine.run("Write the answer")
    assert result["status"] == "ready_for_review"
    reconnect = FactoryContext.open(workspace, storage_root=tmp_path / "controller")
    other_interface = FactoryEngine(workspace=workspace, context=reconnect, execution=execution)
    assert (await other_interface.inspect(result["run_id"]))["status"] == "ready_for_review"
    assert (await other_interface.resume(result["run_id"]))["status"] == "ready_for_review"
    assert execution.spawn_counts["builder"] == 1
    assert result["session_id"] == reconnect.session_id
    assert "bb_parent_thread_id" not in result


def test_controller_storage_cannot_be_inside_target(tmp_path: Path):
    with pytest.raises(ValueError, match="outside"):
        FactoryContext.open(tmp_path, storage_root=tmp_path / "records")


async def test_start_request_is_saved_once_and_status_has_no_side_effects(tmp_path, factory_yaml):
    workspace = tmp_path / "target"
    workspace.mkdir()
    init_repository(workspace, factory_yaml, "pass\n")
    launches = []

    async def launch(run_id):
        launches.append(run_id)

    control = FactoryControl(
        workspace,
        storage_root=tmp_path / "controller",
        execution=FakeBB(workspace, {}),
        launch=launch,
    )
    first = await control.start("Write the answer", request_id="request-1")
    again = await control.start("Write the answer", request_id="request-1")
    assert first["run_id"] == again["run_id"]
    assert launches == [first["run_id"]]
    status = await control.status(first["run_id"])
    assert status["status"] == "pending"
    assert status["operations"] == []
    assert launches == [first["run_id"]]
    with pytest.raises(ValueError, match="different input"):
        await control.start("A different request", request_id="request-1")


async def test_start_waits_for_in_progress_settings_refresh(tmp_path, factory_yaml):
    import asyncio

    from software_factory.config import ModelChoice, Selection

    workspace = tmp_path / "target"
    workspace.mkdir()
    init_repository(workspace, factory_yaml, "pass\n")
    catalog_entered, release_catalog = asyncio.Event(), asyncio.Event()

    class DelayedCatalog(FakeBB):
        async def model_catalog(self):
            catalog_entered.set()
            await release_catalog.wait()
            return await super().model_catalog()

    async def launch(run_id):
        pass

    control = FactoryControl(
        workspace,
        storage_root=tmp_path / "controller",
        execution=DelayedCatalog(workspace, {}),
        launch=launch,
    )
    refresh = asyncio.create_task(
        control.settings(
            selection=Selection(shared=ModelChoice(model="model-b", reasoning_level="low"))
        )
    )
    await asyncio.wait_for(catalog_entered.wait(), timeout=2)
    asyncio.get_running_loop().call_later(0.05, release_catalog.set)
    _, started = await asyncio.gather(
        refresh, control.start("Write the answer", request_id="after-settings")
    )
    saved = await control.status(started["run_id"])
    assert saved["status"] == "pending"
    settings = await control.settings(run_id=started["run_id"])
    assert settings["resolved_settings"]["effective"]["plan"]["model"] == "model-b"


async def test_bb_to_mcp_answer_preserves_run_settings_deadlines_and_dispatches(
    tmp_path, factory_yaml
):
    from software_factory.acp_agent import SoftwareFactoryAgent
    from software_factory.mcp_server import create_mcp
    from tests.integration.test_session_continuation import (
        RecordingBB,
        SessionClient,
        question_output,
        send,
    )

    workspace = tmp_path / "target"
    workspace.mkdir()
    init_repository(workspace, factory_yaml, "pass\n")
    root = tmp_path / "controller"
    execution = RecordingBB(workspace, [question_output(), plan_output()])
    control = None

    async def launch(run_id):
        await (await control.engine(run_id)).resume(run_id)

    control = FactoryControl(workspace, storage_root=root, execution=execution, launch=launch)
    agent = SoftwareFactoryAgent(control.context, execution, launch)
    client = SessionClient()
    agent.on_connect(client)
    session = await agent.new_session(str(workspace))
    await send(agent, session.session_id, "Write the answer")
    run_id = client.run_id
    before = await control.status(run_id)
    assert before["status"] == "waiting_for_input"
    settings = await control.settings(run_id=run_id)
    await agent.cancel(session.session_id)
    assert (await control.status(run_id))["status"] == "waiting_for_input"
    server = create_mcp(control)
    await server.call_tool("factory_status", {"run_id": run_id})
    question = before["interactions"][0]["interaction_id"]
    await server.call_tool(
        "factory_answer",
        {"run_id": run_id, "interaction_id": question, "answers": {"format": "Text"}},
    )
    after = await control.status(run_id, after_event=before["event_position"])
    assert after["status"] == "ready_for_review"
    deadline = after["deadline_at"]
    await create_mcp(control).call_tool(
        "factory_answer",
        {"run_id": run_id, "interaction_id": question, "answers": {"format": "Text"}},
    )
    assert (await control.status(run_id))["deadline_at"] == deadline
    assert await control.settings(run_id=run_id) == settings
    assert after["overall_expires_at"] == before["overall_expires_at"]
    assert all(event["sequence"] > before["event_position"] for event in after["events"])
    assert execution.spawn_counts == {"planner": 1, "builder": 1, "reviewer": 1}
    assert len(execution.answers_sent) == 1


async def test_other_workspace_cannot_inspect_or_cancel_a_run(tmp_path, factory_yaml):
    workspace = tmp_path / "target"
    workspace.mkdir()
    init_repository(workspace, factory_yaml, "pass\n")

    async def launch(run_id):
        pass

    control = FactoryControl(
        workspace,
        storage_root=tmp_path / "controller",
        execution=FakeBB(workspace, {}),
        launch=launch,
    )
    run_id = (await control.start("Write the answer", request_id="request-1"))["run_id"]
    other = tmp_path / "other"
    other.mkdir()
    wrong = FactoryControl(other, storage_root=tmp_path / "controller", launch=launch)
    for operation in (wrong.status, wrong.resume, wrong.cancel):
        with pytest.raises(ValueError, match="different workspace"):
            await operation(run_id)
    assert (await control.status(run_id))["status"] == "pending"


async def test_bb_acknowledges_answer_while_continuation_is_queued(tmp_path, factory_yaml):
    from software_factory.acp_agent import SoftwareFactoryAgent
    from tests.integration.test_session_continuation import (
        RecordingBB,
        SessionClient,
        question_output,
        send,
    )

    workspace = tmp_path / "target"
    workspace.mkdir()
    init_repository(workspace, factory_yaml, "pass\n")
    execution = RecordingBB(workspace, [question_output(), plan_output()])
    launches = []

    async def launch(run_id):
        launches.append(run_id)
        if len(launches) == 1:
            await (await control.engine(run_id)).resume(run_id)

    control = FactoryControl(
        workspace, storage_root=tmp_path / "controller", execution=execution, launch=launch
    )
    agent = SoftwareFactoryAgent(control.context, execution, launch)
    client = SessionClient()
    agent.on_connect(client)
    session = await agent.new_session(str(workspace))
    await send(agent, session.session_id, "Write the answer")
    question = (await control.status(client.run_id))["interactions"][0]["interaction_id"]
    await send(agent, session.session_id, f'/answer {question} {{"format":"Text"}}')
    assert "Answer saved" in client.messages[-1]
    assert "Which output format?" not in client.messages[-1]


async def test_cancellation_is_visible_before_cleanup_and_settings_are_frozen(
    tmp_path, factory_yaml
):
    from software_factory.config import ModelChoice, Selection

    workspace = tmp_path / "target"
    workspace.mkdir()
    init_repository(workspace, factory_yaml, "pass\n")
    launches = []

    async def launch(run_id):
        launches.append(run_id)

    control = FactoryControl(
        workspace,
        storage_root=tmp_path / "controller",
        execution=FakeBB(workspace, {}),
        launch=launch,
    )
    run_id = (await control.start("Write the answer", request_id="request-1"))["run_id"]
    with pytest.raises(ValueError, match="active"):
        await control.settings(
            selection=Selection(shared=ModelChoice(model="model-b", reasoning_level="low"))
        )
    stopped = await control.cancel(run_id)
    assert stopped["status"] == "stopping"
    assert launches == [run_id, run_id]
    await (await control.engine(run_id)).resume(run_id)
    assert (await control.status(run_id))["status"] == "cancelled"


async def test_concurrent_duplicate_start_from_another_interface_returns_same_run(
    tmp_path, factory_yaml
):
    import asyncio

    workspace = tmp_path / "target"
    workspace.mkdir()
    init_repository(workspace, factory_yaml, "pass\n")
    launches = []

    async def launch(run_id):
        launches.append(run_id)

    first = FactoryControl(
        workspace,
        storage_root=tmp_path / "controller",
        session_id="bb-session",
        execution=FakeBB(workspace, {}),
        launch=launch,
    )
    second = FactoryControl(
        workspace,
        storage_root=tmp_path / "controller",
        session_id="mcp-session",
        execution=FakeBB(workspace, {}),
        launch=launch,
    )
    results = await asyncio.gather(
        first.start("Write the answer", request_id="same"),
        second.start("Write the answer", request_id="same"),
    )
    assert results[0]["run_id"] == results[1]["run_id"]
    assert launches == [results[0]["run_id"]]


@pytest.mark.parametrize("same_request", [False, True])
async def test_failed_competing_start_releases_unstarted_session(
    tmp_path, factory_yaml, same_request
):
    from software_factory.ownership import OwnershipError

    workspace = tmp_path / "target"
    workspace.mkdir()
    init_repository(workspace, factory_yaml, "pass\n")

    async def launch(run_id):
        pass

    controls = [
        FactoryControl(
            workspace,
            storage_root=tmp_path / "controller",
            session_id=name,
            execution=FakeBB(workspace, {}),
            launch=launch,
        )
        for name in ("first", "second")
    ]
    first, second = controls
    run_id = (await first.start("First request", request_id="first"))["run_id"]
    with pytest.raises(OwnershipError):
        await second.start("Competing request", request_id="second")
    await first.cancel(run_id)
    await (await first.engine(run_id)).resume(run_id)
    result = await second.start(
        "Competing request" if same_request else "New request",
        request_id="second" if same_request else "third",
    )
    assert result["status"] == "pending"


async def test_empty_bb_inspect_and_system_notice_do_not_start_work(tmp_path, factory_yaml):
    from software_factory.acp_agent import SoftwareFactoryAgent
    from tests.integration.test_session_continuation import SessionClient, send

    workspace = tmp_path / "target"
    workspace.mkdir()
    init_repository(workspace, factory_yaml, "pass\n")
    launches = []

    async def launch(run_id):
        launches.append(run_id)

    context = FactoryContext.open(workspace, storage_root=tmp_path / "controller")
    agent = SoftwareFactoryAgent(context, FakeBB(workspace, {}), launch)
    agent.on_connect(SessionClient())
    session = await agent.new_session(str(workspace))
    await send(agent, session.session_id, "/inspect")
    await send(agent, session.session_id, "[bb system]\nChild task finished")
    assert launches == []
    assert (await agent.store.session(session.session_id))["run_id"] is None


async def test_bb_panel_reference_keeps_conversation_settings_separate(
    tmp_path, factory_yaml, monkeypatch
):
    from software_factory.acp_agent import SoftwareFactoryAgent
    from software_factory.config import ModelChoice
    from software_factory.panel import PanelRequest, panel_request

    workspace = tmp_path / "target"
    workspace.mkdir()
    init_repository(workspace, factory_yaml, "pass\n")
    context = FactoryContext.open(workspace, storage_root=tmp_path / "controller")
    execution = FakeBB(workspace, {})
    first = await panel_request(
        workspace,
        context,
        PanelRequest(action="read", interface_reference="bb-first"),
        execution=execution,
    )
    monkeypatch.setenv("BB_THREAD_ID", "bb-second")
    agent = SoftwareFactoryAgent(context, execution)
    second = await agent.new_session(str(workspace))
    assert first["sessionId"] != second.session_id
    await panel_request(
        workspace,
        context,
        PanelRequest(
            action="change",
            interface_reference="bb-first",
            config_id="shared",
            value=ModelChoice(model="model-b", reasoning_level="low").model_dump_json(),
        ),
        execution=execution,
    )
    reread = await panel_request(
        workspace,
        context,
        PanelRequest(action="read", interface_reference="bb-first"),
        execution=execution,
    )
    assert reread["sessionId"] == first["sessionId"]
    first_settings = await agent.control(workspace, str(first["sessionId"])).settings()
    second_settings = await agent.control(workspace, second.session_id).settings()
    assert first_settings["selection"]["shared"]["model"] == "model-b"
    assert second_settings["selection"]["shared"]["model"] is None
    monkeypatch.setenv("BB_THREAD_ID", "bb-first")
    assert (await agent.new_session(str(workspace))).session_id == first["sessionId"]


async def test_attach_replays_saved_events_once(tmp_path, factory_yaml):
    from software_factory.acp_agent import SoftwareFactoryAgent
    from tests.integration.test_session_continuation import SessionClient, send

    workspace = tmp_path / "target"
    workspace.mkdir()
    init_repository(workspace, factory_yaml, "pass\n")

    async def launch(run_id):
        pass

    execution = FakeBB(workspace, {})
    control = FactoryControl(
        workspace, storage_root=tmp_path / "controller", execution=execution, launch=launch
    )
    run_id = (await control.start("Write the answer", request_id="first"))["run_id"]
    agent = SoftwareFactoryAgent(control.context, execution, launch)
    agent.on_connect(SessionClient())
    session = await agent.new_session(str(workspace))
    await send(agent, session.session_id, "/attach " + run_id)
    assert run_id in await agent.store.session_runs(session.session_id)
    history = await agent.store.session_history(session.session_id)
    await agent._sync_events(session.session_id)
    assert await agent.store.session_history(session.session_id) == history


async def test_saved_settings_reject_altered_evidence(tmp_path, factory_yaml):
    from software_factory.artifacts import ArtifactIntegrityError

    workspace = tmp_path / "target"
    workspace.mkdir()
    init_repository(workspace, factory_yaml, "pass\n")

    async def launch(run_id):
        pass

    control = FactoryControl(
        workspace,
        storage_root=tmp_path / "controller",
        execution=FakeBB(workspace, {}),
        launch=launch,
    )
    run_id = (await control.start("Write the answer", request_id="first"))["run_id"]
    config = control.context.storage_root / "factory" / "artifacts" / run_id / "config.json"
    config.write_text(config.read_text() + "\n")
    with pytest.raises(ArtifactIntegrityError, match="replaced or corrupted"):
        await control.settings(run_id=run_id)


async def test_concurrent_resume_and_cancel_preserve_stop_before_queued_jobs(
    tmp_path, factory_yaml
):
    import asyncio

    workspace = tmp_path / "target"
    workspace.mkdir()
    init_repository(workspace, factory_yaml, "pass\n")
    jobs = []

    async def launch(run_id):
        jobs.append(run_id)

    execution = FakeBB(workspace, {})
    control = FactoryControl(
        workspace, storage_root=tmp_path / "controller", execution=execution, launch=launch
    )
    run_id = (await control.start("Write the answer", request_id="first"))["run_id"]
    await asyncio.gather(control.resume(run_id), control.cancel(run_id))
    assert (await control.status(run_id))["status"] == "stopping"
    for job in jobs:
        await (await control.engine(job)).resume(job)
    assert (await control.status(run_id))["status"] == "cancelled"
    assert execution.spawn_counts == {}
