use crate::{Error, Result};
use serde::{Deserialize, Serialize};
use std::{
    collections::BTreeMap,
    path::{Component, Path},
};

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct AgentConfig {
    pub provider: String,
    #[serde(default, deserialize_with = "nullable_model")]
    pub model: String,
    #[serde(default = "high")]
    pub reasoning_level: String,
    #[serde(default = "auto")]
    pub permission_mode: String,
}
fn nullable_model<'de, D: serde::Deserializer<'de>>(
    deserializer: D,
) -> std::result::Result<String, D::Error> {
    Ok(Option::<String>::deserialize(deserializer)?.unwrap_or_default())
}
fn high() -> String {
    "high".into()
}
fn auto() -> String {
    "auto".into()
}
impl AgentConfig {
    pub fn validate(&self) -> Result<()> {
        if self.provider != "codex"
            || self.model.trim().is_empty()
            || !["none", "minimal", "low", "medium", "high", "xhigh"]
                .contains(&self.reasoning_level.as_str())
            || !["auto", "accept-edits"].contains(&self.permission_mode.as_str())
        {
            return Err(Error::Config(
                "workers require an explicit Codex model/effort and restricted permissions".into(),
            ));
        }
        Ok(())
    }
}
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct AgentsConfig {
    pub planner: AgentConfig,
    pub builder: AgentConfig,
    pub reviewer: AgentConfig,
}
#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq, Default)]
#[serde(rename_all = "snake_case")]
pub enum ReviewMode {
    #[default]
    Single,
    Focused,
    RiskBased,
}
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct QualityCommand {
    pub name: String,
    pub argv: Vec<String>,
    #[serde(default = "command_timeout")]
    pub timeout_seconds: u32,
    #[serde(default)]
    pub repeat_on_recovery: bool,
}
fn command_timeout() -> u32 {
    900
}
impl QualityCommand {
    pub fn validate(&self) -> Result<()> {
        let executable = self
            .argv
            .first()
            .and_then(|s| Path::new(s).file_name())
            .and_then(|s| s.to_str())
            .unwrap_or("")
            .to_ascii_lowercase();
        if self.name.is_empty()
            || !self
                .name
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"._-".contains(&b))
            || !self.name.as_bytes()[0].is_ascii_alphanumeric()
            || self.argv.is_empty()
            || self.argv.iter().any(|s| s.is_empty() || s.contains('\0'))
            || [
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
                "git",
            ]
            .contains(&executable.as_str())
            || !(1..=7200).contains(&self.timeout_seconds)
        {
            return Err(Error::Config("quality commands require unique names, reviewed verification-only argv, no shell or git, and bounded timeouts".into()));
        }
        Ok(())
    }
}
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct QualityConfig {
    pub commands: Vec<QualityCommand>,
}
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(default, deny_unknown_fields)]
pub struct LimitsConfig {
    pub max_test_repairs: u32,
    pub max_review_revisions: u32,
    pub total_run_timeout_seconds: u32,
    pub overall_run_timeout_seconds: u32,
    pub step_timeout_seconds: u32,
    pub connection_retries: u32,
}
impl Default for LimitsConfig {
    fn default() -> Self {
        Self {
            max_test_repairs: 2,
            max_review_revisions: 1,
            total_run_timeout_seconds: 7200,
            overall_run_timeout_seconds: 604800,
            step_timeout_seconds: 900,
            connection_retries: 2,
        }
    }
}
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq, Default)]
#[serde(default, deny_unknown_fields)]
pub struct SafetyConfig {
    pub protected_paths: Vec<String>,
    pub auto_commit: bool,
    pub allow_push: bool,
}
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(default, deny_unknown_fields)]
pub struct ReviewPolicy {
    pub trivial_max_lines: usize,
    pub normal_max_lines: usize,
    pub normal_max_files: usize,
    pub sensitive_paths: Vec<String>,
    pub documentation_paths: Vec<String>,
    pub generated_paths: Vec<String>,
}
impl Default for ReviewPolicy {
    fn default() -> Self {
        Self {
            trivial_max_lines: 10,
            normal_max_lines: 100,
            normal_max_files: 20,
            sensitive_paths: vec![],
            documentation_paths: ["*.md", "*.rst", "*.txt", "*.adoc", "LICENSE*"]
                .map(String::from)
                .into(),
            generated_paths: ["*.map", "*.snap", "*.generated.*"]
                .map(String::from)
                .into(),
        }
    }
}
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(default, deny_unknown_fields)]
pub struct DeliveryPolicy {
    pub mode: String,
    pub repository: Option<String>,
    pub base_branch: String,
    pub work_type: String,
    pub issue: Option<u64>,
    pub title: Option<String>,
    pub review_fixes: bool,
}
impl Default for DeliveryPolicy {
    fn default() -> Self {
        Self {
            mode: "manual".into(),
            repository: None,
            base_branch: "main".into(),
            work_type: "feat".into(),
            issue: None,
            title: None,
            review_fixes: false,
        }
    }
}
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct WorkerConnection {
    pub runtime: String,
    pub model_connection: String,
    pub codex_version: String,
}
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct SettingsSnapshot {
    pub version: u32,
    pub prompt_version: String,
    pub config_version: u32,
    pub requested: serde_json::Value,
    pub effective: BTreeMap<String, AgentConfig>,
    pub catalog: Vec<serde_json::Value>,
}
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct WorkflowConfig {
    pub version: u32,
    pub workflow: String,
    #[serde(default)]
    pub review_mode: ReviewMode,
    #[serde(default)]
    pub review_policy: ReviewPolicy,
    pub agents: AgentsConfig,
    #[serde(default)]
    pub settings: Selection,
    #[serde(default)]
    pub presets: BTreeMap<String, Selection>,
    pub quality: QualityConfig,
    #[serde(default)]
    pub limits: LimitsConfig,
    #[serde(default)]
    pub safety: SafetyConfig,
    #[serde(default)]
    pub delivery: DeliveryPolicy,
    pub worker_connection: Option<WorkerConnection>,
    pub resolved_settings: Option<SettingsSnapshot>,
}
impl WorkflowConfig {
    pub fn from_yaml(input: &str) -> Result<Self> {
        let value: Self = serde_yaml::from_str(input).map_err(|e| Error::Config(e.to_string()))?;
        value.validate()?;
        Ok(value)
    }
    pub fn agent(&self, step: &str) -> &AgentConfig {
        if let Some(agent) = self
            .resolved_settings
            .as_ref()
            .and_then(|s| s.effective.get(step))
        {
            return agent;
        }
        match step {
            "plan" => &self.agents.planner,
            "build" | "repair" | "revision" => &self.agents.builder,
            _ => &self.agents.reviewer,
        }
    }
    pub fn validate(&self) -> Result<()> {
        let invalid = |message: &str| Err(Error::Config(message.into()));
        if ![1, 2].contains(&self.version) || self.workflow != "simple-sdlc" {
            return invalid("unsupported workflow/config version");
        }
        if let Some(worker) = &self.worker_connection {
            if self.version != 2
                || worker.runtime != "codex"
                || worker.model_connection != "native"
                || worker.codex_version != "0.154.0"
            {
                return invalid("only pinned native Codex App Server 0.154.0 is supported");
            }
        } else if self.version == 2 {
            return invalid("version 2 requires worker_connection");
        }
        for agent in [
            &self.agents.planner,
            &self.agents.builder,
            &self.agents.reviewer,
        ] {
            let mut shape = agent.clone();
            if shape.model.is_empty() {
                shape.model = "unresolved".into();
            }
            shape.validate()?;
        }
        if let Some(snapshot) = &self.resolved_settings {
            if snapshot.version != 1
                || snapshot.config_version != self.version
                || snapshot.prompt_version != "1"
            {
                return invalid("incompatible settings snapshot");
            }
            for agent in snapshot.effective.values() {
                agent.validate()?;
            }
            for step in self.active_steps() {
                if !snapshot.effective.contains_key(step) {
                    return invalid("saved model snapshot is missing an active step");
                }
            }
        }
        if self.quality.commands.is_empty() {
            return invalid("at least one reviewed quality command is required");
        }
        let mut names = std::collections::BTreeSet::new();
        for command in &self.quality.commands {
            command.validate()?;
            if !names.insert(&command.name) {
                return invalid("duplicate quality command name");
            }
        }
        for selection in std::iter::once(&self.settings).chain(self.presets.values()) {
            for step in selection.steps.keys() {
                if ![
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
                .contains(&step.as_str())
                {
                    return invalid("unknown model-setting step");
                }
            }
            for choice in std::iter::once(&selection.shared).chain(selection.steps.values()) {
                if [&choice.provider, &choice.model, &choice.reasoning_level]
                    .into_iter()
                    .flatten()
                    .any(|s| s.is_empty())
                {
                    return invalid("explicit model choices cannot be empty");
                }
            }
        }
        let l = &self.limits;
        if l.max_test_repairs > 10
            || l.max_review_revisions > 10
            || l.connection_retries > 10
            || !(60..=86400).contains(&l.total_run_timeout_seconds)
            || !(60..=2592000).contains(&l.overall_run_timeout_seconds)
            || !(1..=7200).contains(&l.step_timeout_seconds)
        {
            return invalid("invalid workflow bounds");
        }
        if self.safety.auto_commit || self.safety.allow_push {
            return invalid("publication requires the separate saved delivery policy");
        }
        for path in &self.safety.protected_paths {
            safe_relative(path)?;
        }
        let p = &self.review_policy;
        if p.trivial_max_lines > p.normal_max_lines
            || p.normal_max_lines == 0
            || p.normal_max_files == 0
        {
            return invalid("invalid review thresholds");
        }
        for pattern in p
            .sensitive_paths
            .iter()
            .chain(&p.documentation_paths)
            .chain(&p.generated_paths)
        {
            safe_relative(pattern)?;
            globset::Glob::new(pattern).map_err(|e| Error::Config(e.to_string()))?;
        }
        self.delivery.validate()
    }
}
impl DeliveryPolicy {
    pub fn validate(&self) -> Result<()> {
        let valid_branch = |s: &str| {
            !s.is_empty()
                && s.as_bytes()[0].is_ascii_alphanumeric()
                && s.bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b"._/-".contains(&b))
                && !s.contains("..")
                && !s.ends_with('.')
                && s.split('/')
                    .all(|p| !p.is_empty() && p != "." && !p.ends_with(".lock"))
        };
        if !["manual", "pull_request"].contains(&self.mode.as_str())
            || !valid_branch(&self.base_branch)
            || !["feat", "fix", "chore", "docs", "refactor", "test"]
                .contains(&self.work_type.as_str())
            || self.issue == Some(0)
            || (self.review_fixes && self.mode != "pull_request")
        {
            return Err(Error::Config("invalid delivery policy".into()));
        }
        if self.mode == "pull_request" {
            let repo = self.repository.as_deref().unwrap_or("");
            let parts: Vec<_> = repo.split('/').collect();
            let title = self.title.as_deref().unwrap_or("");
            if parts.len() != 2
                || parts.iter().any(|s| {
                    s.is_empty()
                        || !s.as_bytes()[0].is_ascii_alphanumeric()
                        || !s
                            .bytes()
                            .all(|b| b.is_ascii_alphanumeric() || b"._-".contains(&b))
                })
                || title.trim() != title
                || title.is_empty()
                || title.len() > 120
                || title.chars().any(char::is_control)
                || title.to_ascii_lowercase().contains("codex")
            {
                return Err(Error::Config(
                    "pull_request delivery requires an explicit repository and descriptive title"
                        .into(),
                ));
            }
        }
        Ok(())
    }
}
pub(crate) fn safe_relative(path: &str) -> Result<()> {
    if path.trim() != path
        || path.is_empty()
        || path.contains(['\\', '\0', '\n', ','])
        || Path::new(path)
            .components()
            .any(|c| !matches!(c, Component::Normal(_)))
    {
        return Err(Error::Config(format!(
            "unsafe repository-relative path: {path:?}"
        )));
    }
    Ok(())
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq, Default)]
#[serde(default, deny_unknown_fields)]
pub struct ModelChoice {
    pub provider: Option<String>,
    pub model: Option<String>,
    pub reasoning_level: Option<String>,
}
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq, Default)]
#[serde(default, deny_unknown_fields)]
pub struct Selection {
    pub shared: ModelChoice,
    pub steps: BTreeMap<String, ModelChoice>,
    pub preset: Option<String>,
}
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ModelOption {
    pub provider: String,
    pub model: String,
    pub name: String,
    pub efforts: Vec<String>,
    pub default_effort: String,
    pub is_default: bool,
    pub permission_modes: Vec<String>,
}
impl WorkflowConfig {
    pub fn active_steps(&self) -> Vec<&'static str> {
        let mut steps = vec!["plan", "build", "repair", "revision"];
        if self.review_mode == ReviewMode::Single {
            steps.push("review");
        } else {
            steps.extend(["request_review", "code_review", "security_review", "judge"]);
            if self.review_mode == ReviewMode::RiskBased {
                steps.push("documentation_review");
            }
        }
        steps
    }
    pub fn resolve(&mut self, selected: &Selection, catalog: Vec<ModelOption>) -> Result<()> {
        if self.resolved_settings.is_some() {
            return self.validate();
        }
        let preset = selected
            .preset
            .as_ref()
            .or(self.settings.preset.as_ref())
            .map(|name| {
                self.presets
                    .get(name)
                    .ok_or_else(|| Error::Config(format!("unknown preset: {name}")))
            })
            .transpose()?
            .cloned()
            .unwrap_or_default();
        let mut effective = BTreeMap::new();
        for step in self.active_steps() {
            let mut agent = self.agent(step).clone();
            if !["plan", "build", "repair", "revision", "review"].contains(&step) {
                agent.permission_mode = "auto".into();
            }
            for layer in [&self.settings, &preset, selected] {
                for choice in [Some(&layer.shared), layer.steps.get(step)]
                    .into_iter()
                    .flatten()
                {
                    if choice
                        .provider
                        .as_ref()
                        .is_some_and(|p| p != &agent.provider)
                    {
                        agent.model.clear();
                        agent.reasoning_level.clear();
                    } else if choice.model.as_ref().is_some_and(|m| m != &agent.model) {
                        agent.reasoning_level.clear();
                    }
                    if let Some(provider) = &choice.provider {
                        agent.provider = provider.clone();
                    }
                    if let Some(model) = &choice.model {
                        agent.model = model.clone();
                    }
                    if let Some(effort) = &choice.reasoning_level {
                        agent.reasoning_level = effort.clone();
                    }
                }
            }
            if !catalog.is_empty() {
                let matches: Vec<_> = catalog
                    .iter()
                    .filter(|m| {
                        m.provider == agent.provider
                            && if agent.model.is_empty() {
                                m.is_default
                            } else {
                                m.model == agent.model
                            }
                    })
                    .collect();
                if matches.len() != 1 {
                    return Err(Error::Config(format!(
                        "{step}: no unique supported model; select an available model"
                    )));
                }
                let model = matches[0];
                agent.model = model.model.clone();
                if agent.reasoning_level.is_empty() {
                    agent.reasoning_level = model.default_effort.clone();
                }
                if !model.efforts.contains(&agent.reasoning_level)
                    || !model.permission_modes.contains(&agent.permission_mode)
                {
                    return Err(Error::Config(format!(
                        "{step}: unsupported effort/permissions; no fallback is allowed"
                    )));
                }
            }
            agent.validate()?;
            effective.insert(step.into(), agent);
        }
        self.resolved_settings = Some(SettingsSnapshot {
            version: 1,
            prompt_version: "1".into(),
            config_version: self.version,
            requested: serde_json::to_value(selected)?,
            effective,
            catalog: catalog
                .into_iter()
                .map(serde_json::to_value)
                .collect::<std::result::Result<_, _>>()?,
        });
        self.validate()
    }
}
