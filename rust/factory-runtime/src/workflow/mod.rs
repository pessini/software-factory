//! Versioned implementation workflow. Provider receipts and domain phase commits
//! remain authoritative when an ADK checkpoint lags behind an external effect.
pub(crate) mod artifacts;
pub mod config;
pub mod delivery;
pub mod docker;
pub(crate) mod execution;
pub mod github;
pub mod handoffs;
mod phases;
mod process;
mod project;
mod prompts;
pub(crate) mod repository;
pub(crate) mod review_fixes;
mod review_policy;
mod verified_git;

use crate::{Operation, Result};
pub use config::{AgentConfig, DeliveryPolicy, QualityCommand, ReviewMode, WorkflowConfig};
pub use execution::{CheckResult, ExecutionKind, ExecutionRequest};
pub use handoffs::{Build, Criterion, FocusedReview, Plan, Question, Review};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

pub const GRAPH_VERSION: &str = "factory-sdlc-v1";
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ImplementationInput {
    pub config: WorkflowConfig,
    #[serde(default)]
    pub required_criteria: Vec<Criterion>,
    pub approved_plan: Option<ApprovedPlan>,
}
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ApprovedPlan {
    pub planning_session_id: String,
    pub revision_id: String,
    pub sha256: String,
    pub content: String,
    pub completion_checks: Vec<Criterion>,
}
impl ImplementationInput {
    pub fn validate(&self) -> Result<()> {
        self.config.validate()?;
        let mut criteria = self.required_criteria.clone();
        if let Some(plan) = &self.approved_plan {
            handoffs::require(
                crate::runtime::digest(plan.content.as_bytes()) == plan.sha256
                    && !plan.content.trim().is_empty()
                    && !plan.completion_checks.is_empty(),
                "approved plan digest/content mismatch",
            )?;
            handoffs::text(&plan.planning_session_id)?;
            handoffs::text(&plan.revision_id)?;
            criteria.extend(plan.completion_checks.clone());
        }
        handoffs::unique(criteria.iter().map(|c| c.id.as_str()))?;
        for c in criteria {
            handoffs::identifier(&c.id)?;
            handoffs::text(&c.description)?;
        }
        Ok(())
    }
}
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct SavedInteraction {
    pub interaction_id: String,
    pub questions: Vec<Question>,
    pub answers: Option<BTreeMap<String, String>>,
}
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct Artifact {
    pub sha256: String,
    pub bytes: u64,
}
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct WorkflowState {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub planning: Option<crate::planning::PlanningState>,
    pub next: String,
    pub outcome: Option<crate::Status>,
    pub project: Option<project::Project>,
    pub parent_run_id: Option<String>,
    pub wait_started_at: Option<chrono::DateTime<chrono::Utc>>,
    pub overall_deadline_at: Option<chrono::DateTime<chrono::Utc>>,
    pub baseline: Option<repository::RepositorySnapshot>,
    pub plan: Option<Plan>,
    pub build: Option<Build>,
    pub checks: Vec<CheckResult>,
    pub baseline_checks: Vec<CheckResult>,
    pub test_attempt_count: u32,
    pub test_repair_count: u32,
    pub review_attempt_count: u32,
    pub review_revision_count: u32,
    pub test_passed: bool,
    pub review_approved: bool,
    pub content_version: Option<String>,
    pub tested_snapshot: Option<repository::RepositorySnapshot>,
    pub operations: BTreeMap<String, Operation>,
    pub requests: BTreeMap<String, ExecutionRequest>,
    pub guards: BTreeMap<String, repository::RepositorySnapshot>,
    pub artifacts: BTreeMap<String, Artifact>,
    pub interaction: Option<SavedInteraction>,
    pub interactions: BTreeMap<String, SavedInteraction>,
    pub review: Option<Review>,
    pub history: BTreeMap<String, handoffs::FindingHistory>,
    pub review_selection: Option<review_policy::ReviewSelection>,
    pub handoff: Option<serde_json::Value>,
    pub delivery: serde_json::Value,
    #[serde(default)]
    pub remote_operations: BTreeMap<String, delivery::RemoteOperation>,
}
impl Default for WorkflowState {
    fn default() -> Self {
        Self {
            planning: None,
            next: "bootstrap".into(),
            outcome: None,
            project: None,
            parent_run_id: None,
            wait_started_at: None,
            overall_deadline_at: None,
            baseline: None,
            plan: None,
            build: None,
            checks: vec![],
            baseline_checks: vec![],
            test_attempt_count: 0,
            test_repair_count: 0,
            review_attempt_count: 0,
            review_revision_count: 0,
            test_passed: false,
            review_approved: false,
            content_version: None,
            tested_snapshot: None,
            operations: BTreeMap::new(),
            requests: BTreeMap::new(),
            guards: BTreeMap::new(),
            artifacts: BTreeMap::new(),
            interaction: None,
            interactions: BTreeMap::new(),
            review: None,
            history: BTreeMap::new(),
            review_selection: None,
            handoff: None,
            remote_operations: BTreeMap::new(),
            delivery: serde_json::json!({"status":"manual"}),
        }
    }
}
pub(crate) use phases::run_graph;
