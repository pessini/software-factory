"""One settings implementation for ACP clients and the BB factory panel."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from acp.schema import SessionConfigOptionSelect, SessionConfigSelectOption

from software_factory.config import (
    FactoryConfig,
    ModelChoice,
    ModelOption,
    Selection,
    SettingsSnapshot,
    Step,
    load_config,
    review_steps,
)
from software_factory.execution import ExecutionManager
from software_factory.ownership import OwnershipError, session_lease
from software_factory.persistence import RunStore
from software_factory.settings import change_selection, resolve_config


class SessionSettings:
    def __init__(self, store: RunStore, execution: ExecutionManager) -> None:
        self.store, self.execution = store, execution

    async def recover_start(self, session_id: str) -> None:
        try:
            with session_lease(self.store.path, session_id):
                session = await self.store.session(session_id)
                if session and session["run_id"]:
                    await self.store.release_unstarted_session_run(
                        session_id, str(session["run_id"])
                    )
        except OwnershipError:
            # A live prompt owns the startup window; its claim must remain.
            pass

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
        return resolve_config(config, selection, catalog)

    async def options(self, session_id: str) -> list[SessionConfigOptionSelect]:
        await self.recover_start(session_id)
        config, selection = await self.read(session_id)
        controls = self.controls(config, selection, await self.execution.model_catalog())
        await self.cache_options(session_id, config, selection, controls)
        return controls

    async def cache_options(
        self,
        session_id: str,
        config: FactoryConfig,
        selection: Selection,
        controls: list[SessionConfigOptionSelect],
    ) -> None:
        await self.store.save_session_options(
            session_id,
            json.dumps(
                {
                    "config": config.model_dump(mode="json"),
                    "selection": selection.model_dump(mode="json"),
                }
            ),
            json.dumps([control.model_dump(mode="json", by_alias=True) for control in controls]),
        )

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
        steps = review_steps(config.review_mode)
        for name in ("shared", *steps):
            roles = set(steps.values()) if name == "shared" else {steps[cast(Step, name)]}
            permissions = {config.agents.for_role(role).permission_mode for role in roles}
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
            description = "Default for all steps. Step choices take priority."
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
                    name=name.replace("_", " ").title(),
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
        await self.recover_start(session_id)
        config, selection = await self.read(session_id)
        catalog = await self.execution.model_catalog()
        selection = change_selection(selection, config_id, value)
        controls = self.controls(config, selection, catalog, strict=True)
        await self.save(session_id, config, selection)
        await self.cache_options(session_id, config, selection, controls)
        return controls
