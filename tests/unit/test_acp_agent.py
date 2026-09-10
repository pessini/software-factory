from pathlib import Path

import pytest

from software_factory.acp_agent import SoftwareFactoryAgent
from software_factory.bb import BBContext
from tests.conftest import FakeBB


@pytest.mark.asyncio
async def test_acp_session_can_be_created_and_loaded(tmp_path: Path, factory_yaml: str) -> None:
    context = BBContext("parent", "project", "environment", tmp_path / "storage")
    (tmp_path / ".factory").mkdir()
    (tmp_path / ".factory/factory.yaml").write_text(factory_yaml)
    agent = SoftwareFactoryAgent(context, FakeBB(tmp_path, {}))
    initialized = await agent.initialize(protocol_version=1)
    assert initialized.agent_info is not None
    assert initialized.agent_info.name == "software-factory"
    assert initialized.agent_capabilities is not None
    assert initialized.agent_capabilities.load_session is True
    session = await agent.new_session(str(tmp_path))
    loaded = await agent.load_session(str(tmp_path), session.session_id)
    assert loaded is not None


async def test_step_choices_survive_agent_restart_and_yaml_edit(
    tmp_path: Path, factory_yaml: str
) -> None:
    from software_factory.config import ModelChoice

    (tmp_path / ".factory").mkdir()
    policy = tmp_path / ".factory/factory.yaml"
    policy.write_text(factory_yaml)
    context = BBContext("parent", "project", "environment", tmp_path / "storage")
    bb = FakeBB(tmp_path, {})
    agent = SoftwareFactoryAgent(context, bb)
    session = await agent.new_session(str(tmp_path))
    value = ModelChoice(provider="fake", model="model-b", reasoning_level="low").model_dump_json()
    updated = await agent.set_config_option(session.session_id, "repair", value)
    repair = next(o for o in updated.config_options if o.id == "repair")
    assert "model-b / low" in repair.description
    policy.write_text("invalid YAML for a new run")
    restarted = SoftwareFactoryAgent(context, bb)
    loaded = await restarted.load_session(str(tmp_path), session.session_id)
    assert loaded.config_options == updated.config_options
    assert next(o for o in loaded.config_options if o.id == "shared").current_value == "inherit"
    fresh = await restarted.new_session(str(tmp_path))
    assert next(o for o in fresh.config_options if o.id == "repair").current_value == value


async def test_unsupported_choice_does_not_replace_saved_selection(
    tmp_path: Path, factory_yaml: str
) -> None:
    from software_factory.config import ModelChoice

    (tmp_path / ".factory").mkdir()
    (tmp_path / ".factory/factory.yaml").write_text(factory_yaml)
    agent = SoftwareFactoryAgent(
        BBContext("parent", "project", "environment", tmp_path / "storage"), FakeBB(tmp_path, {})
    )
    session = await agent.new_session(str(tmp_path))
    value = ModelChoice(provider="fake", model="model-a", reasoning_level="low").model_dump_json()
    with pytest.raises(ValueError, match="does not support effort"):
        await agent.set_config_option(session.session_id, "review", value)
    reloaded = await agent.load_session(str(tmp_path), session.session_id)
    assert reloaded.config_options == session.config_options


async def test_active_run_prevents_settings_changes(tmp_path: Path, factory_yaml: str) -> None:
    from software_factory.config import ModelChoice

    (tmp_path / ".factory").mkdir()
    (tmp_path / ".factory/factory.yaml").write_text(factory_yaml)
    agent = SoftwareFactoryAgent(
        BBContext("parent", "project", "environment", tmp_path / "storage"), FakeBB(tmp_path, {})
    )
    session = await agent.new_session(str(tmp_path))
    saved = await agent.store.session(session.session_id)
    await agent.store.claim_session_run(session.session_id, "pending-run", saved["settings_json"])
    value = ModelChoice(provider="fake", model="model-b", reasoning_level="low").model_dump_json()
    with pytest.raises(ValueError, match="active"):
        await agent.set_config_option(session.session_id, "shared", value)
    await agent.store.release_unstarted_session_run(session.session_id, "pending-run")
    await agent.set_config_option(session.session_id, "shared", value)


async def test_disconnected_initial_update_releases_session_claim(
    tmp_path: Path, factory_yaml: str
) -> None:
    from acp.schema import TextContentBlock

    (tmp_path / ".factory").mkdir()
    (tmp_path / ".factory/factory.yaml").write_text(factory_yaml)
    agent = SoftwareFactoryAgent(
        BBContext("parent", "project", "environment", tmp_path / "storage"), FakeBB(tmp_path, {})
    )
    session = await agent.new_session(str(tmp_path))

    class DisconnectedClient:
        async def session_update(self, *args, **kwargs):
            raise ConnectionError("client disconnected")

    agent.on_connect(DisconnectedClient())
    with pytest.raises(ConnectionError, match="disconnected"):
        await agent.prompt(session.session_id, [TextContentBlock(type="text", text="Run checks")])
    assert (await agent.store.session(session.session_id))["run_id"] is None
    assert not agent.cancellations
    await agent.set_config_option(session.session_id, "shared", "inherit")


async def test_panel_changes_the_loaded_session(
    tmp_path: Path, factory_yaml: str, monkeypatch
) -> None:
    from software_factory.config import ModelChoice
    from software_factory.panel import PanelRequest, panel_request

    (tmp_path / ".factory").mkdir()
    (tmp_path / ".factory/factory.yaml").write_text(factory_yaml)
    context = BBContext("parent", "project", "environment", tmp_path / "storage")
    bb = FakeBB(tmp_path, {})
    monkeypatch.setattr("software_factory.panel.BBClient.from_context", lambda *_: bb)
    agent = SoftwareFactoryAgent(context, bb)
    first = await agent.new_session(str(tmp_path))
    second = await agent.new_session(str(tmp_path))
    await agent.load_session(str(tmp_path), first.session_id)
    value = ModelChoice(provider="fake", model="model-b", reasoning_level="low").model_dump_json()
    result = await panel_request(
        tmp_path, context, PanelRequest(action="change", config_id="repair", value=value)
    )
    assert result["sessionId"] == first.session_id
    _, selected = await agent.settings(tmp_path).read(first.session_id)
    assert selected.steps["repair"].model == "model-b"
    _, other = await agent.settings(tmp_path).read(second.session_id)
    assert "repair" not in other.steps


async def test_step_controls_show_models_with_the_required_permissions(
    tmp_path: Path, factory_yaml: str
) -> None:
    from software_factory.config import ModelOption, Selection, load_config
    from software_factory.session_settings import SessionSettings

    (tmp_path / ".factory").mkdir()
    (tmp_path / ".factory/factory.yaml").write_text(factory_yaml)
    config = load_config(tmp_path)
    catalog = await FakeBB(tmp_path, {}).model_catalog()
    for mode in ("auto", "accept-edits"):
        catalog.append(
            ModelOption(
                provider=mode,
                model="only",
                name=mode,
                efforts=["high"],
                default_effort="high",
                permission_modes=[mode],
            )
        )
    controls = {o.id: o for o in SessionSettings.controls(config, Selection(), catalog)}
    for step in ("plan", "review"):
        assert any("auto / auto" in o.name for o in controls[step].options)
        assert not any("accept-edits / accept-edits" in o.name for o in controls[step].options)
    for step in ("build", "repair", "revision"):
        assert any("accept-edits / accept-edits" in o.name for o in controls[step].options)
        assert not any("auto / auto" in o.name for o in controls[step].options)
    assert not any(
        "auto / auto" in o.name or "accept-edits / accept-edits" in o.name
        for o in controls["shared"].options
    )


async def test_acp_wire_protocol_saves_and_reloads_config_options(
    tmp_path: Path, factory_yaml: str
) -> None:
    import os
    import sys

    import acp

    from software_factory.config import ModelChoice

    (tmp_path / ".factory").mkdir()
    (tmp_path / ".factory/factory.yaml").write_text(factory_yaml)

    class Client:
        async def session_update(self, *args, **kwargs):
            pass

    launcher = """import asyncio, os, acp
from pathlib import Path
from software_factory.acp_agent import SoftwareFactoryAgent
from software_factory.bb import BBContext
from tests.conftest import FakeBB
root = Path(os.environ['FACTORY_TEST_WORKSPACE'])
asyncio.run(acp.run_agent(SoftwareFactoryAgent(BBContext('parent','project','environment',root/'storage'),FakeBB(root,{}))))
"""
    env = {**os.environ, "FACTORY_TEST_WORKSPACE": str(tmp_path)}
    async with acp.spawn_agent_process(Client(), sys.executable, "-c", launcher, env=env) as (
        connection,
        _,
    ):
        await connection.initialize(protocol_version=1)
        session = await connection.new_session(cwd=str(tmp_path), mcp_servers=[])
        changed = await connection.set_config_option(
            session_id=session.session_id,
            config_id="revision",
            value=ModelChoice(
                provider="fake", model="model-b", reasoning_level="low"
            ).model_dump_json(),
        )
    async with acp.spawn_agent_process(Client(), sys.executable, "-c", launcher, env=env) as (
        connection,
        _,
    ):
        await connection.initialize(protocol_version=1)
        loaded = await connection.load_session(
            cwd=str(tmp_path), session_id=session.session_id, mcp_servers=[]
        )
        assert loaded.config_options == changed.config_options
