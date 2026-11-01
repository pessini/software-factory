"""JSON command used by the BB panel; settings remain in controller storage."""

from __future__ import annotations

from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import Field

from software_factory.config import ConfigModel, FactoryConfig, ModelOption, Selection
from software_factory.context import FactoryContext
from software_factory.control import FactoryControl
from software_factory.engine import checkpoint_path
from software_factory.execution import ExecutionManager
from software_factory.persistence import RunStore
from software_factory.session_settings import SessionSettings
from software_factory.settings import change_selection


class PanelRequest(ConfigModel):
    action: Literal["read", "change"]
    config_id: str | None = None
    value: str | None = Field(default=None, max_length=10000)
    interface_reference: str | None = Field(default=None, min_length=1, max_length=1000)


async def panel_request(
    workspace: Path,
    context: FactoryContext,
    request: PanelRequest,
    *,
    execution: ExecutionManager | None = None,
    session_id: str | None = None,
) -> dict[str, object]:
    store = RunStore(checkpoint_path(context))
    await store.initialize()
    if request.interface_reference:
        session_id = await store.interface_session("bb", request.interface_reference, workspace)
    session = (
        await store.session(session_id) if session_id else await store.latest_session(workspace)
    )
    if session is None:
        session_id = f"factory_session_{uuid4().hex}"
        await store.save_session(session_id, workspace)
    else:
        session_id = str(session["session_id"])
    control = FactoryControl(
        workspace, storage_root=context.storage_root, session_id=session_id, execution=execution
    )
    saved = await control.settings()
    selection = Selection.model_validate(saved["selection"])
    if request.action == "change":
        if request.config_id is None or request.value is None:
            raise ValueError("a settings change requires config_id and value")
        selection = change_selection(selection, request.config_id, request.value)
        saved = await control.settings(selection=selection)
    config = FactoryConfig.model_validate(saved["configuration"])
    catalog = (
        [ModelOption.model_validate(item) for item in await control.models()]
        if control.remote
        else await control.execution.model_catalog()
    )
    options = SessionSettings.controls(config, selection, catalog)
    await SessionSettings(store, control.execution).cache_options(
        session_id, config, selection, options
    )
    return {
        "sessionId": session_id,
        "configOptions": [option.model_dump(mode="json", by_alias=True) for option in options],
    }
