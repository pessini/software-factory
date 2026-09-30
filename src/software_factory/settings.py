"""Model choices resolved against the execution environment's provider catalog."""

from __future__ import annotations

from typing import cast

from software_factory.config import (
    STEP_ROLES,
    AgentConfig,
    FactoryConfig,
    ModelChoice,
    ModelOption,
    ReviewMode,
    Selection,
    SettingsSnapshot,
    Step,
    review_steps,
)


def change_selection(selection: Selection, config_id: str, value: str) -> Selection:
    updated = selection.model_copy(deep=True)
    if config_id == "preset":
        updated.preset = None if value == "inherit" else value
    elif config_id == "shared" or config_id in STEP_ROLES:
        choice = ModelChoice() if value == "inherit" else ModelChoice.model_validate_json(value)
        if config_id == "shared":
            updated.shared = choice
        else:
            updated.steps[cast(Step, config_id)] = choice
    else:
        raise ValueError(f"unknown setting: {config_id}")
    return updated


def resolve_choices(
    roles: dict[str, AgentConfig],
    project: Selection,
    presets: dict[str, Selection],
    selected: Selection,
    catalog: list[ModelOption],
    config_version: int,
    review_mode: ReviewMode = "single",
) -> SettingsSnapshot:
    preset_name = selected.preset or project.preset
    if preset_name and preset_name not in presets:
        raise ValueError(f"unknown settings preset: {preset_name}")
    preset = presets[preset_name] if preset_name else Selection()
    effective: dict[Step, AgentConfig] = {}
    for step, role in review_steps(review_mode).items():
        values = roles[role].model_dump()
        # Each layer overrides lower-priority layers. Within a layer a step
        # choice overrides that layer's shared default.
        for layer in (project, preset, selected):
            for choice in (layer.shared, layer.steps.get(step, ModelChoice())):
                update = choice.model_dump(exclude_none=True)
                if update.get("provider", values["provider"]) != values["provider"]:
                    values["model"] = None
                    values["reasoning_level"] = None
                elif update.get("model", values["model"]) != values["model"]:
                    values["reasoning_level"] = None
                values.update(update)
        options = [o for o in catalog if o.provider == values["provider"]]
        matches = (
            [o for o in options if o.model == values["model"]]
            if values["model"]
            else [o for o in options if o.is_default]
        )
        if len(matches) != 1:
            raise ValueError(
                f"{step}: provider {values['provider']!r} has no unique supported model {values['model']!r}; choose an available model"
            )
        model = matches[0]
        effort = values["reasoning_level"] or model.default_effort
        if effort not in model.efforts:
            raise ValueError(
                f"{step}: {model.provider}/{model.model} does not support effort {effort!r}; supported: {', '.join(model.efforts)}"
            )
        if values["permission_mode"] not in model.permission_modes:
            raise ValueError(
                f"{step}: {model.provider} does not support required permission mode {values['permission_mode']}"
            )
        values.update(model=model.model, reasoning_level=effort)
        effective[step] = AgentConfig.model_validate(values)
    return SettingsSnapshot(
        config_version=config_version, requested=selected, effective=effective, catalog=catalog
    )


def resolve_config(
    config: FactoryConfig, selection: Selection, catalog: list[ModelOption]
) -> SettingsSnapshot:
    return resolve_choices(
        {
            role: config.agents.for_role(role)
            for role in set(review_steps(config.review_mode).values())
        },
        config.settings,
        config.presets,
        selection,
        catalog,
        config.version,
        config.review_mode,
    )
