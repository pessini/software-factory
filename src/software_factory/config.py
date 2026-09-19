from __future__ import annotations

import re
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
    reasoning_level: str = Field(default="high", min_length=1)
    permission_mode: Literal["accept-edits", "auto", "full"] = "auto"


ReviewMode = Literal["single", "focused", "risk_based"]
Step = Literal[
    "plan",
    "build",
    "repair",
    "review",
    "revision",
    "request_review",
    "code_review",
    "security_review",
    "documentation_review",
    "judge",
]
STEP_ROLES: dict[Step, str] = {
    "plan": "planner",
    "build": "builder",
    "repair": "builder",
    "review": "reviewer",
    "revision": "builder",
    "request_review": "request_reviewer",
    "code_review": "code_reviewer",
    "security_review": "security_reviewer",
    "documentation_review": "documentation_reviewer",
    "judge": "judge",
}


def review_steps(mode: ReviewMode) -> dict[Step, str]:
    return {
        step: role
        for step, role in STEP_ROLES.items()
        if (
            (step != "review" and (mode == "risk_based" or step != "documentation_review"))
            if mode != "single"
            else step in {"plan", "build", "repair", "review", "revision"}
        )
    }


class ModelChoice(ConfigModel):
    provider: str | None = Field(default=None, min_length=1)
    model: str | None = Field(default=None, min_length=1)
    reasoning_level: str | None = Field(default=None, min_length=1)


class Selection(ConfigModel):
    shared: ModelChoice = Field(default_factory=ModelChoice)
    steps: dict[Step, ModelChoice] = Field(default_factory=dict)
    preset: str | None = None


class ModelOption(ConfigModel):
    provider: str
    model: str
    name: str
    efforts: list[str]
    default_effort: str
    is_default: bool = False
    permission_modes: list[str]


class SettingsSnapshot(ConfigModel):
    version: Literal[1] = 1
    prompt_version: str = "1"
    config_version: int
    requested: Selection
    effective: dict[Step, AgentConfig]
    catalog: list[ModelOption]


class AgentsConfig(ConfigModel):
    planner: AgentConfig
    builder: AgentConfig
    reviewer: AgentConfig

    def for_role(self, role: str) -> AgentConfig:
        if role in {
            "request_reviewer",
            "code_reviewer",
            "security_reviewer",
            "documentation_reviewer",
            "judge",
        }:
            return self.reviewer.model_copy(update={"permission_mode": "auto"})
        return {"planner": self.planner, "builder": self.builder, "reviewer": self.reviewer}[role]

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
    overall_run_timeout_seconds: int = Field(default=604800, ge=60, le=2592000)
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


class ReviewPolicy(ConfigModel):
    trivial_max_lines: int = Field(default=10, ge=0)
    normal_max_lines: int = Field(default=100, ge=1)
    normal_max_files: int = Field(default=20, ge=1)
    sensitive_paths: list[str] = Field(default_factory=list)
    documentation_paths: list[str] = Field(
        default_factory=lambda: ["*.md", "*.rst", "*.txt", "*.adoc", "LICENSE*"]
    )
    generated_paths: list[str] = Field(default_factory=lambda: ["*.map", "*.snap", "*.generated.*"])

    @field_validator("sensitive_paths", "documentation_paths", "generated_paths")
    @classmethod
    def relative_patterns(cls, value: list[str]) -> list[str]:
        for pattern in value:
            if (
                not pattern.strip()
                or pattern != pattern.strip()
                or "\\" in pattern
                or "\x00" in pattern
                or PurePosixPath(pattern).is_absolute()
                or ".." in PurePosixPath(pattern).parts
            ):
                raise ValueError("review patterns must be repository-relative globs")
        return value

    @model_validator(mode="after")
    def ordered_thresholds(self) -> ReviewPolicy:
        if self.trivial_max_lines > self.normal_max_lines:
            raise ValueError("trivial_max_lines cannot exceed normal_max_lines")
        return self


class DeliveryPolicy(ConfigModel):
    mode: Literal["manual", "pull_request"] = "manual"
    repository: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$"
    )
    base_branch: str = "main"
    work_type: Literal["feat", "fix", "chore", "docs", "refactor", "test"] = "feat"
    issue: int | None = Field(default=None, ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=120)
    review_fixes: bool = False

    @field_validator("base_branch")
    @classmethod
    def safe_branch(cls, value: str) -> str:
        if (
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", value)
            or any(part in {"", ".", ".."} or part.endswith(".lock") for part in value.split("/"))
            or ".." in value
            or value.endswith(".")
        ):
            raise ValueError("base_branch must be a Git branch name")
        return value

    @field_validator("title")
    @classmethod
    def descriptive_title(cls, value: str | None) -> str | None:
        if value is not None and (
            value != value.strip()
            or any(ord(char) < 32 for char in value)
            or "codex" in value.casefold()
        ):
            raise ValueError(
                "use a single-line title that describes the change without naming the agent"
            )
        return value

    @model_validator(mode="after")
    def authorized_target(self) -> DeliveryPolicy:
        if self.mode == "pull_request" and (not self.repository or not self.title):
            raise ValueError("pull_request delivery requires repository and title")
        if self.review_fixes and self.mode != "pull_request":
            raise ValueError("review_fixes requires pull_request delivery")
        return self


class WorkerConnection(ConfigModel):
    runtime: Literal["codex"] = "codex"
    model_connection: Literal["native"] = "native"
    codex_version: Literal["0.154.0"] = "0.154.0"


class FactoryConfig(ConfigModel):
    version: Literal[1, 2]
    workflow: Literal["simple-sdlc"]
    # Omitted in Phase 1 configurations, including saved active runs.
    review_mode: ReviewMode = "single"
    review_policy: ReviewPolicy = Field(default_factory=ReviewPolicy)
    agents: AgentsConfig
    quality: QualityConfig
    settings: Selection = Field(default_factory=Selection)
    presets: dict[str, Selection] = Field(default_factory=dict)
    resolved_settings: SettingsSnapshot | None = None
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    delivery: DeliveryPolicy = Field(default_factory=DeliveryPolicy)
    worker_connection: WorkerConnection | None = None

    @model_validator(mode="after")
    def connection_version(self) -> FactoryConfig:
        if self.version == 1 and self.worker_connection is not None:
            raise ValueError("worker_connection requires configuration version 2")
        if self.version == 2 and self.worker_connection is None:
            raise ValueError("configuration version 2 requires an explicit worker_connection")
        return self


def upgrade_config(config: FactoryConfig) -> FactoryConfig:
    """Convert a copy; retain the original policy and all saved authorization."""
    if config.version == 2:
        return config.model_copy(deep=True)
    return FactoryConfig.model_validate(
        {
            **config.model_dump(mode="json"),
            "version": 2,
            "worker_connection": WorkerConnection().model_dump(),
        }
    )


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
    return """version: 2
worker_connection:
  runtime: codex
  model_connection: native
  codex_version: '0.154.0'
workflow: simple-sdlc
review_mode: focused
# To select reviewers by size and sensitive paths, use review_mode: risk_based.
# See docs/risk-review.md for threshold boundaries and project-specific patterns.
delivery:
  mode: manual
# See docs/pr-delivery.md to authorize publication and selected review fixes.

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
      argv: [\"uv\", \"run\", \"--no-sync\", \"python\", \"-B\", \"-m\", \"pytest\", \"-q\", \"-p\", \"no:cacheprovider\"]
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
