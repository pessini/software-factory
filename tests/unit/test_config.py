from pathlib import Path

import pytest
from pydantic import ValidationError

from software_factory.config import FactoryConfig, load_config


def test_loads_strict_policy(tmp_path: Path, factory_yaml: str) -> None:
    (tmp_path / ".factory").mkdir()
    (tmp_path / ".factory" / "factory.yaml").write_text(factory_yaml, encoding="utf-8")
    config = load_config(tmp_path)
    assert config.version == 1
    assert config.quality.commands[0].argv == ["python3", "quality_check.py"]
    assert config.safety.auto_commit is False
    assert config.safety.allow_push is False


def test_rejects_shell_commands(factory_yaml: str) -> None:
    import yaml

    raw = yaml.safe_load(factory_yaml)
    raw["quality"]["commands"][0]["shell_command"] = "rm -rf something"
    with pytest.raises(ValidationError, match="shell_command"):
        FactoryConfig.model_validate(raw)


@pytest.mark.parametrize(
    "argv",
    [
        ["sh", "-c", "git push origin HEAD"],
        ["bash", "-lc", "deploy production"],
        ["git", "push", "origin", "HEAD"],
    ],
)
def test_rejects_side_effecting_quality_argv(factory_yaml: str, argv: list[str]) -> None:
    import yaml

    raw = yaml.safe_load(factory_yaml)
    raw["quality"]["commands"][0]["argv"] = argv
    with pytest.raises(ValidationError, match="quality commands must be verification-only"):
        FactoryConfig.model_validate(raw)


def test_rejects_full_agent_permissions(factory_yaml: str) -> None:
    import yaml

    raw = yaml.safe_load(factory_yaml)
    raw["agents"]["builder"]["permission_mode"] = "full"
    with pytest.raises(ValidationError, match="full permission mode is not allowed"):
        FactoryConfig.model_validate(raw)


def test_rejects_unsafe_protected_path(factory_yaml: str) -> None:
    import yaml

    raw = yaml.safe_load(factory_yaml)
    raw["safety"]["protected_paths"] = ["../outside"]
    with pytest.raises(ValidationError, match="repository-relative"):
        FactoryConfig.model_validate(raw)


def test_rejects_enabling_automatic_push(factory_yaml: str) -> None:
    import yaml

    raw = yaml.safe_load(factory_yaml)
    raw["safety"]["allow_push"] = True
    with pytest.raises(ValidationError):
        FactoryConfig.model_validate(raw)


async def test_project_preset_shared_and_step_choices_have_defined_priority(
    factory_yaml: str, tmp_path: Path
) -> None:
    import yaml

    from software_factory.config import ModelChoice, Selection
    from software_factory.session_settings import SessionSettings
    from tests.conftest import FakeBB

    raw = yaml.safe_load(factory_yaml)
    raw["settings"] = {"shared": {"model": "model-a"}}
    raw["presets"] = {"fast": {"shared": {"model": "model-b", "reasoning_level": "low"}}}
    config = FactoryConfig.model_validate(raw)
    selected = Selection(
        preset="fast", steps={"review": ModelChoice(model="model-a", reasoning_level="high")}
    )
    resolved = SessionSettings.resolve(config, selected, await FakeBB(tmp_path, {}).model_catalog())
    assert resolved.effective["build"].model == "model-b"
    assert resolved.effective["repair"].reasoning_level == "low"
    assert resolved.effective["review"].model == "model-a"
    assert resolved.effective["build"].permission_mode == "accept-edits"
    selected.shared = ModelChoice(model="model-a", reasoning_level="high")
    resolved = SessionSettings.resolve(config, selected, await FakeBB(tmp_path, {}).model_catalog())
    assert resolved.effective["plan"].model == "model-a"
