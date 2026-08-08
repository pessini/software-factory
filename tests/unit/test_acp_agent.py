from pathlib import Path

import pytest

from software_factory.acp_agent import SoftwareFactoryAgent
from software_factory.bb import BBContext


@pytest.mark.asyncio
async def test_acp_session_can_be_created_and_loaded(tmp_path: Path) -> None:
    context = BBContext("parent", "project", "environment", tmp_path / "storage")
    agent = SoftwareFactoryAgent(context)
    initialized = await agent.initialize(protocol_version=1)
    assert initialized.agent_info is not None
    assert initialized.agent_info.name == "software-factory"
    assert initialized.agent_capabilities is not None
    assert initialized.agent_capabilities.load_session is True
    session = await agent.new_session(str(tmp_path))
    loaded = await agent.load_session(str(tmp_path), session.session_id)
    assert loaded is not None
