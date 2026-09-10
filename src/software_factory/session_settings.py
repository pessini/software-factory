"""One settings implementation for ACP clients and the BB factory panel."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from acp.schema import SessionConfigOptionSelect, SessionConfigSelectOption

from software_factory.bb.client import BBClientProtocol
from software_factory.config import (
    STEP_ROLES,
    FactoryConfig,
    ModelChoice,
    ModelOption,
    Selection,
    SettingsSnapshot,
    Step,
    load_config,
)
from software_factory.persistence import RunStore
from software_factory.settings import resolve_choices


class SessionSettings:
    def __init__(self, store: RunStore, bb: BBClientProtocol) -> None:
        self.store, self.bb = store, bb

    async def read(self, session_id: str) -> tuple[FactoryConfig, Selection]:
        session = await self.store.session(session_id)
        if session is None:
            raise ValueError(f"unknown ACP session: {session_id}")
        if session["settings_json"]:
            saved = json.loads(str(session["settings_json"]))
            return FactoryConfig.model_validate(saved["config"]), Selection.model_validate(
                saved["selection"]
            )
        config, selection = load_config(Path(str(session["cwd"]))), Selection()
        await self.save(session_id, config, selection)
        return config, selection

    async def save(self, session_id: str, config: FactoryConfig, selection: Selection) -> None:
        await self.store.save_session_settings(
            session_id,
            json.dumps(
                {
                    "config": config.model_dump(mode="json"),
                    "selection": selection.model_dump(mode="json"),
                }
            ),
        )

    @staticmethod
    def resolve(
        config: FactoryConfig, selection: Selection, catalog: list[ModelOption]
    ) -> SettingsSnapshot:
        return resolve_choices(
            {role: getattr(config.agents, role) for role in set(STEP_ROLES.values())},
            config.settings,
            config.presets,
            selection,
            catalog,
            config.version,
        )

    async def options(self, session_id: str) -> list[SessionConfigOptionSelect]:
        config, selection = await self.read(session_id)
        return self.controls(config, selection, await self.bb.model_catalog())

    @staticmethod
    def controls(
        config: FactoryConfig,
        selection: Selection,
        catalog: list[ModelOption],
        *,
        strict: bool = False,
    ) -> list[SessionConfigOptionSelect]:
        resolved = None
        error = ""
        try:
            resolved = SessionSettings.resolve(config, selection, catalog)
        except ValueError as exc:
            if strict:
                raise
            error = str(exc)
        controls = [
            SessionConfigOptionSelect(
                id="preset",
                name="Preset",
                type="select",
                current_value=selection.preset or "inherit",
                options=[SessionConfigSelectOption(value="inherit", name="Project default")]
                + [SessionConfigSelectOption(value=name, name=name) for name in config.presets],
            )
        ]
        for name in ("shared", *STEP_ROLES):
            roles = set(STEP_ROLES.values()) if name == "shared" else {STEP_ROLES[cast(Step, name)]}
            permissions = {getattr(config.agents, role).permission_mode for role in roles}
            options = [SessionConfigSelectOption(value="inherit", name="Use inherited settings")]
            for model in catalog:
                if not permissions.issubset(model.permission_modes):
                    continue
                for effort in model.efforts:
                    option_choice = ModelChoice(
                        provider=model.provider, model=model.model, reasoning_level=effort
                    )
                    options.append(
                        SessionConfigSelectOption(
                            value=option_choice.model_dump_json(),
                            name=f"{model.provider} / {model.name} / {effort}",
                        )
                    )
            choice = (
                selection.shared
                if name == "shared"
                else selection.steps.get(cast(Step, name), ModelChoice())
            )
            description = "Default for all five steps. Step choices take priority."
            if name != "shared" and resolved is not None:
                agent = resolved.effective[cast(Step, name)]
                description = (
                    f"Resolved: {agent.provider} / {agent.model} / {agent.reasoning_level}"
                )
            if name != "shared" and resolved is None:
                description = "Cannot start: " + error
            controls.append(
                SessionConfigOptionSelect(
                    id=name,
                    name=name.title(),
                    type="select",
                    description=description,
                    current_value=choice.model_dump_json()
                    if choice != ModelChoice()
                    else "inherit",
                    options=options,
                )
            )
        return controls

    async def change(
        self, session_id: str, config_id: str, value: str
    ) -> list[SessionConfigOptionSelect]:
        config, selection = await self.read(session_id)
        catalog = await self.bb.model_catalog()
        if config_id == "preset":
            selection.preset = None if value == "inherit" else value
        elif config_id == "shared" or config_id in STEP_ROLES:
            choice = ModelChoice() if value == "inherit" else ModelChoice.model_validate_json(value)
            if config_id == "shared":
                selection.shared = choice
            else:
                selection.steps[cast(Step, config_id)] = choice
        else:
            raise ValueError(f"unknown setting: {config_id}")
        controls = self.controls(config, selection, catalog, strict=True)
        await self.save(session_id, config, selection)
        return controls
