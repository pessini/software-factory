"""JSON command used by the BB panel; settings remain in controller storage."""

from __future__ import annotations

from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import Field

from software_factory.bb import BBClient, BBContext
from software_factory.config import ConfigModel
from software_factory.engine import checkpoint_path
from software_factory.persistence import RunStore
from software_factory.session_settings import SessionSettings


class PanelRequest(ConfigModel):
    action: Literal["read", "change"]
    config_id: str | None = None
    value: str | None = Field(default=None, max_length=10000)


async def panel_request(
    workspace: Path, context: BBContext, request: PanelRequest
) -> dict[str, object]:
    store = RunStore(checkpoint_path(context))
    await store.initialize()
    session = await store.latest_session(workspace)
    if session is None:
        session_id = f"factory_session_{uuid4().hex}"
        await store.save_session(session_id, workspace)
    else:
        session_id = str(session["session_id"])
    service = SessionSettings(store, BBClient.from_context(context, workspace))
    if request.action == "change":
        if request.config_id is None or request.value is None:
            raise ValueError("a settings change requires config_id and value")
        options = await service.change(session_id, request.config_id, request.value)
    else:
        options = await service.options(session_id)
    return {
        "sessionId": session_id,
        "configOptions": [option.model_dump(mode="json", by_alias=True) for option in options],
    }
