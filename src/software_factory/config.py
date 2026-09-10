from __future__ import annotations

import shlex
from pathlib import Path, PurePosixPath
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AgentConfig(ConfigModel):
    provider: str = Field(min_length=1)
    model: str | None = None
    reasoning_level: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    permission_mode: Literal["accept-edits", "auto", "full"] = "auto"


class AgentsConfig(ConfigModel):
    planner: AgentConfig
    builder: AgentConfig
    reviewer: AgentConfig

    @model_validator(mode="after")
    def restricted_permissions(self) -> AgentsConfig:
        elevated = [
            role
            for role, agent in (
                ("planner", self.planner),
                ("builder", self.builder),
                ("reviewer", self.reviewer),
            )
            if agent.permission_mode == "full"
        ]
        if elevated:
            raise ValueError(
                "full permission mode is not allowed for factory agents: " + ", ".join(elevated)
            )
        return self


class QualityCommand(ConfigModel):
    name: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    argv: list[str] = Field(min_length=1)
    timeout_seconds: int = Field(default=900, ge=1, le=7200)
    repeat_on_recovery: bool = False

    @field_validator("argv")
    @classmethod
    def valid_argv(cls, value: list[str]) -> list[str]:
        if any(not item or "\x00" in item for item in value):
            raise ValueError("argv entries must be non-empty and contain no NUL bytes")
        executable = Path(value[0]).name.casefold()
        shells = {
            "bash",
            "cmd",
            "cmd.exe",
            "dash",
            "fish",
            "ksh",
            "powershell",
            "pwsh",
            "sh",
            "tcsh",
            "zsh",
        }
        if executable in shells or executable == "git":
            raise ValueError(
                "quality commands must be verification-only; shell interpreters and git are forbidden"
            )
        return value

    @property
    def display(self) -> str:
        return shlex.join(self.argv)


class QualityConfig(ConfigModel):
    commands: list[QualityCommand] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_names(self) -> QualityConfig:
        names = [command.name for command in self.commands]
        if len(names) != len(set(names)):
            raise ValueError("quality command names must be unique")
        return self


class LimitsConfig(ConfigModel):
    max_test_repairs: int = Field(default=2, ge=0, le=10)
    max_review_revisions: int = Field(default=1, ge=0, le=10)
    total_run_timeout_seconds: int = Field(default=7200, ge=60, le=86400)
    step_timeout_seconds: int = Field(default=900, ge=1, le=7200)
    connection_retries: int = Field(default=2, ge=0, le=10)


class SafetyConfig(ConfigModel):
    protected_paths: list[str] = Field(default_factory=list)
    auto_commit: Literal[False] = False
    allow_push: Literal[False] = False

    @field_validator("protected_paths")
    @classmethod
    def relative_safe_paths(cls, value: list[str]) -> list[str]:
        for item in value:
            path = PurePosixPath(item)
            if not item or path.is_absolute() or ".." in path.parts:
                raise ValueError(f"protected path must be repository-relative: {item!r}")
        return value


class FactoryConfig(ConfigModel):
    version: Literal[1]
    workflow: Literal["simple-sdlc"]
    agents: AgentsConfig
    quality: QualityConfig
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)


CONFIG_PATH = Path(".factory/factory.yaml")


def load_config(workspace: Path, config_path: Path | None = None) -> FactoryConfig:
    path = config_path or workspace / CONFIG_PATH
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"factory configuration not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"factory configuration must be a mapping: {path}")
    return FactoryConfig.model_validate(raw)


def default_config() -> str:
    return """version: 1
workflow: simple-sdlc

agents:
  planner:
    provider: codex
    reasoning_level: high
    permission_mode: auto
  builder:
    provider: codex
    reasoning_level: high
    permission_mode: accept-edits
  reviewer:
    provider: codex
    reasoning_level: high
    permission_mode: auto

quality:
  commands:
    - name: tests
      argv: [\"uv\", \"run\", \"pytest\", \"-q\"]
      timeout_seconds: 900

limits:
  max_test_repairs: 2
  max_review_revisions: 1
  total_run_timeout_seconds: 7200

safety:
  protected_paths:
    - .factory/factory.yaml
    - .bb/
    - .github/workflows/
  auto_commit: false
  allow_push: false
"""
