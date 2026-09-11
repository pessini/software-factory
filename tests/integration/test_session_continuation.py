"""Session and engine behavior for issue #5, with BB replaced at its API boundary."""

import asyncio
import json
import os
import re
import sqlite3
import sys
import time

import acp
import pytest
from acp.schema import TextContentBlock
from langgraph.errors import NodeCancelledError

from software_factory.acp_agent import SoftwareFactoryAgent
from software_factory.bb.client import BBError
from software_factory.config import ModelChoice
from software_factory.engine import FactoryEngine
from tests.conftest import FakeBB, init_repository
from tests.integration.test_factory_engine import (
    build_output,
    context,
    plan_output,
    review_output,
    write_answer,
)


def question_output():
    return json.dumps(
        {
            "status": "needs_input",
            "summary": "The output format is required before implementation.",
            "acceptance_criteria": [],
            "implementation_steps": [],
            "questions": [
                {"id": "format", "question": "Which output format?", "options": ["Text", "JSON"]}
            ],
        }
    )


class RecordingBB(FakeBB):
    def __init__(self, workspace, planner=None):
        super().__init__(
            workspace,
            {
                "planner": [{"output": output} for output in (planner or [plan_output()])],
                "builder": [{"output": build_output(), "action": write_answer("good")}],
                "reviewer": [{"output": review_output()}],
            },
        )
        self.answers_sent = []

    async def tell(self, thread_id, message):
        self.answers_sent.append((thread_id, message))
        return await super().tell(thread_id, message)


class SessionClient:
    def __init__(self):
        self.updates = []

    async def session_update(self, session_id, update):
        self.updates.append(update.model_dump(mode="json", by_alias=True))

    @property
    def messages(self):
        return [
            item["content"]["text"]
            for item in self.updates
            if item["sessionUpdate"] == "agent_message_chunk"
        ]

    @property
    def run_id(self):
        return re.search(r"Run (\S+):", self.messages[-1])[1]


async def send(agent, session_id, text):
    return await agent.prompt(session_id, [TextContentBlock(type="text", text=text)])


async def test_planner_question_survives_restart_and_answer_continues_once(tmp_path, factory_yaml):
    init_repository(tmp_path, factory_yaml, "raise SystemExit(0)\n")
    bb = RecordingBB(tmp_path, [question_output(), plan_output()])
    engine = FactoryEngine(workspace=tmp_path, context=context(tmp_path), bb=bb)
    waiting = await engine.run("Write the answer", "fct_saved_question")
    assert waiting["status"] == "waiting_for_input"
    question = waiting["interactions"][0]
    assert question["questions"][0]["id"] == "format"
    assert bb.spawn_counts == {"planner": 1}

    restarted = FactoryEngine(workspace=tmp_path, context=context(tmp_path), bb=bb)
    loaded = await restarted.inspect(waiting["run_id"])
    assert loaded["interactions"] == waiting["interactions"]
    assert (await restarted.resume(waiting["run_id"]))["status"] == "waiting_for_input"
    assert not bb.answers_sent
    result = await restarted.answer(
        waiting["run_id"], question["interaction_id"], {"format": "Text"}
    )
    assert result["status"] == "accepted", result.get("error")
    assert result["run_id"] == waiting["run_id"]
    assert result["interactions"][0]["answers"] == {"format": "Text"}
    assert (tmp_path / "answer.txt").read_text() == "good"
    duplicate = await restarted.answer(
        waiting["run_id"], question["interaction_id"], {"format": "Text"}
    )
    assert duplicate == await restarted.inspect(result["run_id"])
    assert bb.spawn_counts == {"planner": 1, "builder": 1, "reviewer": 1}
    assert len(bb.answers_sent) == 1


async def test_retry_only_recovers_supported_states_and_preserves_failure_history(
    tmp_path, factory_yaml
):
    init_repository(tmp_path, factory_yaml, "raise SystemExit(0)\n")

    class OfflineBB(RecordingBB):
        offline = True

        async def execution(self, thread_id, execution_id):
            if self.offline:
                raise BBError("connection lost", "connection")
            return await super().execution(thread_id, execution_id)

    bb = OfflineBB(tmp_path)
    agent = SoftwareFactoryAgent(context(tmp_path), bb)
    client = SessionClient()
    agent.on_connect(client)
    session = await agent.new_session(str(tmp_path))
    await send(agent, session.session_id, "Write the answer")
    assert "recovery_needed" in client.messages[-1]
    run_id, failure_report = client.run_id, client.messages[-1]
    bb.offline = False
    await send(agent, session.session_id, "/retry")
    assert client.run_id == run_id and "accepted" in client.messages[-1]
    assert failure_report in client.messages
    assert bb.spawn_counts == {"planner": 1, "builder": 1, "reviewer": 1}
    with pytest.raises(ValueError, match="retry supports recovery_needed"):
        await send(agent, session.session_id, "/retry")


async def test_completed_session_replays_history_and_status_never_starts_work(
    tmp_path, factory_yaml
):
    init_repository(tmp_path, factory_yaml, "raise SystemExit(0)\n")
    bb = RecordingBB(tmp_path)
    agent = SoftwareFactoryAgent(context(tmp_path), bb)
    client = SessionClient()
    agent.on_connect(client)
    session = await agent.new_session(str(tmp_path))
    await send(agent, session.session_id, "Write the answer")
    run_id = client.run_id
    assert "accepted" in client.messages[-1]
    first_messages = client.messages[:]
    first_tools = [item for item in client.updates if item["sessionUpdate"].startswith("tool_call")]
    call_ids = [item["toolCallId"] for item in first_tools if item["sessionUpdate"] == "tool_call"]
    assert len(call_ids) == len(set(call_ids))
    final_tools = {}
    for item in first_tools:
        assert item["toolCallId"] in call_ids
        if item.get("status"):
            final_tools[item["toolCallId"]] = item["status"]
    assert set(final_tools.values()) == {"completed"}
    final_plan = [item for item in client.updates if item["sessionUpdate"] == "plan"][-1]
    assert "Repair: unused" in [entry["content"] for entry in final_plan["entries"]]

    restarted = SoftwareFactoryAgent(context(tmp_path), bb)
    replay = SessionClient()
    restarted.on_connect(replay)
    await restarted.initialize(protocol_version=1)
    loaded = await restarted.load_session(str(tmp_path), session.session_id)
    assert loaded.config_options == session.config_options
    assert replay.messages == first_messages
    assert [
        item for item in replay.updates if item["sessionUpdate"].startswith("tool_call")
    ] == first_tools
    assert any(
        item["sessionUpdate"] == "user_message_chunk"
        and item["content"]["text"] == "Write the answer"
        for item in replay.updates
    )
    assert replay.run_id == run_id

    for request in ("What is the status?", "/continue", "Please explain the result"):
        await send(restarted, session.session_id, request)
    assert bb.spawn_counts == {"planner": 1, "builder": 1, "reviewer": 1}
    assert not bb.answers_sent
    await send(restarted, session.session_id, "/new Write a second answer")
    assert replay.run_id != run_id
    assert "worktree must be clean" in replay.messages[-1]
    assert bb.spawn_counts == {"planner": 1, "builder": 1, "reviewer": 1}


async def test_waiting_session_ignores_unrelated_messages_and_cancellation_prevents_answer(
    tmp_path, factory_yaml
):
    init_repository(tmp_path, factory_yaml, "raise SystemExit(0)\n")
    bb = RecordingBB(tmp_path, [question_output(), plan_output()])
    agent = SoftwareFactoryAgent(context(tmp_path), bb)
    client = SessionClient()
    agent.on_connect(client)
    session = await agent.new_session(str(tmp_path))
    await send(agent, session.session_id, "Write the answer")
    run_id = client.run_id
    engine = FactoryEngine(workspace=tmp_path, context=context(tmp_path), bb=bb)
    question = (await engine.inspect(run_id))["interactions"][0]
    restarted = SoftwareFactoryAgent(context(tmp_path), bb)
    restarted.on_connect(client)
    await restarted.load_session(str(tmp_path), session.session_id)
    assert "Which output format?" in client.messages[-1]
    for unrelated in ("Thanks", "yes", "Text", "/continue", "/status"):
        await send(restarted, session.session_id, unrelated)
        assert (await engine.inspect(run_id))["interactions"][0]["answers"] is None
    assert bb.spawn_counts == {"planner": 1}
    await restarted.cancel(session.session_id)
    assert "cancelled" in client.messages[-1]
    stopped = await engine.inspect(run_id)
    assert stopped["status"] == "cancelled"
    assert stopped["interactions"][0]["state"] == "cancelled"
    with pytest.raises(ValueError, match="no longer waiting"):
        await send(restarted, session.session_id, f"/answer {question['interaction_id']} Text")
    assert (await engine.resume(run_id))["status"] == "cancelled"
    assert not bb.answers_sent and bb.spawn_counts == {"planner": 1}


@pytest.mark.parametrize("elapsed", [3600, 604801])
async def test_waiting_time_preserves_work_budget_but_overall_expiry_stops_run(
    tmp_path, factory_yaml, monkeypatch, elapsed
):
    init_repository(tmp_path, factory_yaml, "raise SystemExit(0)\n")
    bb = RecordingBB(tmp_path, [question_output(), plan_output()])
    engine = FactoryEngine(workspace=tmp_path, context=context(tmp_path), bb=bb)
    waiting = await engine.run("Write the answer", "fct_question_timeout")
    assert waiting["status"] == "waiting_for_input"
    future = time.time() + elapsed
    monkeypatch.setattr(time, "time", lambda: future)
    restarted = FactoryEngine(workspace=tmp_path, context=context(tmp_path), bb=bb)
    result = await restarted.answer(
        waiting["run_id"], waiting["interactions"][0]["interaction_id"], {"format": "Text"}
    )
    if elapsed == 3600:
        assert result["status"] == "accepted", result.get("error")
        assert len(bb.answers_sent) == 1
    else:
        assert result["status"] == "failed"
        assert "overall run expiry" in result["error"]
        assert not bb.answers_sent and bb.spawn_counts == {"planner": 1}


async def test_restart_during_build_reuses_run_settings_and_child_operations(
    tmp_path, factory_yaml
):
    init_repository(tmp_path, factory_yaml, "raise SystemExit(0)\n")

    class InterruptedBB(RecordingBB):
        interrupted = False

        async def spawn_child(self, role, prompt, agent, spawn_key):
            child = await super().spawn_child(role, prompt, agent, spawn_key)
            if role == "builder" and not self.interrupted:
                self.interrupted = True
                raise asyncio.CancelledError()
            return child

        async def model_catalog(self):
            if self.interrupted:
                raise BBError("model catalog offline", "connection")
            return await super().model_catalog()

    bb = InterruptedBB(tmp_path)
    agent = SoftwareFactoryAgent(context(tmp_path), bb)
    client = SessionClient()
    agent.on_connect(client)
    session = await agent.new_session(str(tmp_path))
    selected = ModelChoice(provider="fake", model="model-b", reasoning_level="low")
    options = await agent.set_config_option(session.session_id, "build", selected.model_dump_json())
    with pytest.raises(NodeCancelledError):
        await send(agent, session.session_id, "Write the answer")
    run_id = re.search(r"Starting run ([^.]+)", client.messages[0])[1]
    restarted = SoftwareFactoryAgent(context(tmp_path), bb)
    replay = SessionClient()
    restarted.on_connect(replay)
    loaded = await restarted.load_session(str(tmp_path), session.session_id)
    assert loaded.config_options == options.config_options
    assert bb.spawn_counts == {"planner": 1, "builder": 1}
    await send(restarted, session.session_id, "/continue")
    assert replay.run_id == run_id
    assert "accepted" in replay.messages[-1]
    assert bb.spawn_counts == {"planner": 1, "builder": 1, "reviewer": 1}
    assert not bb.answers_sent
    builder = next(choice for role, choice in bb.choices if role == "builder")
    assert (builder.model, builder.reasoning_level) == ("model-b", "low")


@pytest.mark.parametrize("status", ["accepted", "running"])
async def test_old_session_records_load_without_repeating_unrecorded_work(
    tmp_path, factory_yaml, status
):
    init_repository(tmp_path, factory_yaml, "raise SystemExit(0)\n")
    ctx = context(tmp_path)
    path = ctx.thread_storage / "factory/checkpoints.sqlite"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE factory_sessions (session_id TEXT PRIMARY KEY, cwd TEXT, run_id TEXT);
            CREATE TABLE factory_runs (
                run_id TEXT PRIMARY KEY, status TEXT, workspace TEXT, parent_thread_id TEXT,
                active_child_thread_id TEXT, active_process_group INTEGER, error TEXT,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
        """)
        db.execute(
            "INSERT INTO factory_sessions VALUES ('old-session', ?, 'old-run')", (str(tmp_path),)
        )
        db.execute(
            "INSERT INTO factory_runs (run_id,status,workspace,parent_thread_id) VALUES ('old-run',?,?,?)",
            (status, str(tmp_path), ctx.parent_thread_id),
        )
    bb = RecordingBB(tmp_path)
    agent = SoftwareFactoryAgent(ctx, bb)
    client = SessionClient()
    agent.on_connect(client)
    await agent.load_session(str(tmp_path), "old-session")
    assert f"Run old-run: {status}" in client.messages[-1]
    await send(agent, "old-session", "/status")
    if status == "accepted":
        await send(agent, "old-session", "/continue")
    else:
        with pytest.raises(ValueError, match="legacy run cannot resume safely"):
            await send(agent, "old-session", "/continue")
    assert not bb.spawn_counts and not bb.answers_sent


async def test_duplicate_answer_is_harmless_while_the_first_answer_is_running(
    tmp_path, factory_yaml
):
    init_repository(tmp_path, factory_yaml, "raise SystemExit(0)\n")
    entered, release = asyncio.Event(), asyncio.Event()

    class SlowBB(RecordingBB):
        async def spawn_child(self, role, prompt, agent, spawn_key):
            if role == "builder":
                entered.set()
                await release.wait()
            return await super().spawn_child(role, prompt, agent, spawn_key)

    bb = SlowBB(tmp_path, [question_output(), plan_output()])
    engine = FactoryEngine(workspace=tmp_path, context=context(tmp_path), bb=bb)
    waiting = await engine.run("Write the answer", "fct_concurrent_answer")
    interaction_id = waiting["interactions"][0]["interaction_id"]
    first = asyncio.create_task(
        engine.answer(waiting["run_id"], interaction_id, {"format": "Text"})
    )
    try:
        await asyncio.wait_for(entered.wait(), 5)
        other = FactoryEngine(workspace=tmp_path, context=context(tmp_path), bb=bb)
        duplicate = await other.answer(waiting["run_id"], interaction_id, {"format": "Text"})
        assert duplicate["run_id"] == waiting["run_id"]
        assert duplicate["interactions"][0]["answers"] == {"format": "Text"}
        with pytest.raises(ValueError, match="different saved answer"):
            await other.answer(waiting["run_id"], interaction_id, {"format": "JSON"})
    finally:
        release.set()
        result = await first
    assert result["status"] == "accepted"
    assert len(bb.answers_sent) == 1


async def test_duplicate_acp_answer_during_active_prompt_returns_saved_run(tmp_path, factory_yaml):
    init_repository(tmp_path, factory_yaml, "raise SystemExit(0)\n")
    entered, release = asyncio.Event(), asyncio.Event()

    class SlowBB(RecordingBB):
        async def spawn_child(self, role, prompt, agent, spawn_key):
            if role == "builder":
                entered.set()
                await release.wait()
            return await super().spawn_child(role, prompt, agent, spawn_key)

    bb = SlowBB(tmp_path, [question_output(), plan_output()])
    agent = SoftwareFactoryAgent(context(tmp_path), bb)
    client = SessionClient()
    agent.on_connect(client)
    session = await agent.new_session(str(tmp_path))
    await send(agent, session.session_id, "Write the answer")
    run_id = client.run_id
    engine = FactoryEngine(workspace=tmp_path, context=context(tmp_path), bb=bb)
    question = (await engine.inspect(run_id))["interactions"][0]
    text = f"/answer {question['interaction_id']} Text"
    first = asyncio.create_task(send(agent, session.session_id, text))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        second = SoftwareFactoryAgent(context(tmp_path), bb)
        duplicate_client = SessionClient()
        second.on_connect(duplicate_client)
        duplicate = await send(second, session.session_id, text)
        assert duplicate.stop_reason == "end_turn"
        assert duplicate_client.run_id == run_id
    finally:
        release.set()
        await first
    assert len(bb.answers_sent) == 1
    assert (await engine.inspect(run_id))["status"] == "accepted"


async def test_acp_process_replays_saved_history_before_load_response(tmp_path, factory_yaml):
    init_repository(tmp_path, factory_yaml, "raise SystemExit(0)\n")
    agent = SoftwareFactoryAgent(context(tmp_path), RecordingBB(tmp_path))
    original = SessionClient()
    agent.on_connect(original)
    session = await agent.new_session(str(tmp_path))
    await send(agent, session.session_id, "Write the answer")
    launcher = """import asyncio, os, acp
from pathlib import Path
from software_factory.acp_agent import SoftwareFactoryAgent
from tests.conftest import FakeBB
from tests.integration.test_factory_engine import context
root = Path(os.environ['FACTORY_REPLAY_WORKSPACE'])
asyncio.run(acp.run_agent(SoftwareFactoryAgent(context(root), FakeBB(root, {}))))
"""
    replay = SessionClient()
    async with acp.spawn_agent_process(
        replay,
        sys.executable,
        "-c",
        launcher,
        env={**os.environ, "FACTORY_REPLAY_WORKSPACE": str(tmp_path)},
    ) as (connection, _):
        await connection.initialize(protocol_version=1)
        await connection.load_session(
            cwd=str(tmp_path), session_id=session.session_id, mcp_servers=[]
        )
        assert replay.messages == original.messages
        assert replay.run_id == original.run_id
        assert any(item["sessionUpdate"] == "user_message_chunk" for item in replay.updates)
        result = await connection.prompt(
            session_id=session.session_id, prompt=[TextContentBlock(type="text", text="/continue")]
        )
        assert result.stop_reason == "end_turn"
        assert "accepted" in replay.messages[-1]


@pytest.mark.parametrize("interface", ["acp", "engine"])
async def test_simultaneous_answers_are_accepted_once(tmp_path, factory_yaml, interface):
    init_repository(tmp_path, factory_yaml, "raise SystemExit(0)\n")
    bb = RecordingBB(tmp_path, [question_output(), plan_output()])
    first = SoftwareFactoryAgent(context(tmp_path), bb)
    client = SessionClient()
    first.on_connect(client)
    session = await first.new_session(str(tmp_path))
    await send(first, session.session_id, "Write the answer")
    run_id = client.run_id
    engine = FactoryEngine(workspace=tmp_path, context=context(tmp_path), bb=bb)
    question = (await engine.inspect(run_id))["interactions"][0]
    second = SoftwareFactoryAgent(context(tmp_path), bb)
    second.on_connect(SessionClient())
    answer = f"/answer {question['interaction_id']} Text"
    if interface == "acp":
        submissions = [
            send(first, session.session_id, answer),
            send(second, session.session_id, answer),
        ]
    else:
        other = FactoryEngine(workspace=tmp_path, context=context(tmp_path), bb=bb)
        submissions = [
            owner.answer(run_id, question["interaction_id"], {"format": "Text"})
            for owner in (engine, other)
        ]
    results = await asyncio.gather(*submissions, return_exceptions=True)
    assert all(not isinstance(result, BaseException) for result in results), results
    assert (await engine.inspect(run_id))["status"] == "accepted"
    assert len(bb.answers_sent) == 1
