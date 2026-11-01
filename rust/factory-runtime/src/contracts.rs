use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::BTreeMap;

use crate::{Error, Result};

pub const SCHEMA_VERSION: u32 = 1;
pub const GRAPH_VERSION: &str = "factory-fake-v1";

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct StartRequest {
    pub request_id: String,
    pub session_id: String,
    pub request: String,
    pub timeout_seconds: u32,
    pub max_dispatches: u32,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub implementation: Option<crate::workflow::ImplementationInput>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub planning: Option<crate::planning::PlanningInput>,
}

impl StartRequest {
    pub fn run_id(&self) -> String {
        format!("run-{}", crate::runtime::digest(self.request_id.as_bytes()))
    }

    pub fn new(request_id: &str, session_id: &str, request: &str) -> Self {
        Self {
            request_id: request_id.into(),
            session_id: session_id.into(),
            request: request.into(),
            timeout_seconds: 3600,
            max_dispatches: 3,
            implementation: None,
            planning: None,
        }
    }

    pub fn validate(&self) -> Result<()> {
        if self.planning.is_some() && self.implementation.is_none() {
            return Err(Error::Config(
                "planning requires saved execution configuration".into(),
            ));
        }
        if let Some(implementation) = &self.implementation {
            implementation.validate()?;
        }
        for id in [&self.request_id, &self.session_id] {
            if id.is_empty()
                || id.len() > 128
                || !id
                    .bytes()
                    .all(|c| c.is_ascii_alphanumeric() || b"-_".contains(&c))
            {
                return Err(Error::Config(
                    "IDs must contain 1–128 ASCII letters, digits, - or _".into(),
                ));
            }
        }
        if self.request.trim().is_empty()
            || self.request.len() > 65536
            || !(1..=604800).contains(&self.timeout_seconds)
            || !(1..=10).contains(&self.max_dispatches)
        {
            return Err(Error::Config("invalid request or execution limits".into()));
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Status {
    Pending,
    Running,
    WaitingForInput,
    RecoveryNeeded,
    Stopping,
    ReadyForReview,
    Planned,
    Failed,
    Blocked,
    Cancelled,
}

impl Status {
    pub fn is_terminal(self) -> bool {
        matches!(
            self,
            Self::Planned | Self::ReadyForReview | Self::Failed | Self::Blocked | Self::Cancelled
        )
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Interaction {
    pub interaction_id: String,
    pub question: String,
    pub approved: Option<bool>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Event {
    pub schema_version: u32,
    pub run_id: String,
    pub sequence: u64,
    pub timestamp: DateTime<Utc>,
    pub node: String,
    pub status: String,
    pub attempt: u32,
    pub message: String,
    pub metadata: BTreeMap<String, Value>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum OperationState {
    Prepared,
    Dispatching,
    Completed,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Operation {
    pub operation_id: String,
    pub state: OperationState,
    pub dispatch_count: u32,
    pub result: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Run {
    pub schema_version: u32,
    pub graph_version: String,
    pub run_id: String,
    pub workspace_id: String,
    pub checkpoint_thread_id: String,
    pub scheduler_id: Option<String>,
    pub input: StartRequest,
    pub revision: u64,
    pub status: Status,
    pub interaction: Option<Interaction>,
    pub operation: Option<Operation>,
    pub attempts: u32,
    pub deadline_at: DateTime<Utc>,
    pub cancel_requested: bool,
    pub result: Option<String>,
    pub error: Option<String>,
    pub events: Vec<Event>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub workflow: Option<Box<crate::workflow::WorkflowState>>,
}

impl Run {
    pub fn delivery_pending(&self) -> bool {
        self.delivery_refreshable()
            && self.workflow.as_ref().is_some_and(|s| {
                !["delivered", "ci_failed", "ci_unavailable", "blocked"]
                    .iter()
                    .any(|v| s.delivery["status"] == *v)
            })
    }

    pub(crate) fn delivery_refreshable(&self) -> bool {
        self.status == Status::ReadyForReview
            && self.workflow.as_ref().is_some_and(|s| {
                s.parent_run_id.is_none()
                    && (self
                        .input
                        .implementation
                        .as_ref()
                        .is_some_and(|i| i.config.delivery.mode == "pull_request")
                        || s.delivery["policy"]["mode"] == "pull_request")
                    && !["no_change", "cancelled"]
                        .iter()
                        .any(|v| s.delivery["status"] == *v)
            })
    }

    pub fn expires_at(&self) -> DateTime<Utc> {
        self.workflow
            .as_ref()
            .filter(|w| w.wait_started_at.is_some())
            .and_then(|w| w.overall_deadline_at)
            .unwrap_or(self.deadline_at)
    }
    pub fn waiting_for_answers(&self) -> bool {
        self.interaction
            .as_ref()
            .is_some_and(|q| q.approved.is_none())
            || self
                .workflow
                .as_ref()
                .is_some_and(|w| w.interaction.as_ref().is_some_and(|q| q.answers.is_none()))
    }
    pub fn check_version(&self) -> Result<()> {
        let graph = if self.input.planning.is_some() {
            crate::planning::GRAPH_VERSION
        } else if self.input.implementation.is_some() {
            crate::workflow::GRAPH_VERSION
        } else {
            GRAPH_VERSION
        };
        if self.schema_version != SCHEMA_VERSION
            || self.graph_version != graph
            || self.input.implementation.is_some() != self.workflow.is_some()
        {
            return Err(Error::Incompatible(format!(
                "run {} uses state {} / graph {}; expected {} / {}. Keep the previous binary or migrate explicitly",
                self.run_id, self.schema_version, self.graph_version, SCHEMA_VERSION, GRAPH_VERSION
            )));
        }
        Ok(())
    }

    pub(crate) fn event(&mut self, node: &str, status: &str, message: &str) {
        self.events.push(Event {
            schema_version: 1,
            run_id: self.run_id.clone(),
            sequence: self.events.len() as u64 + 1,
            timestamp: Utc::now(),
            node: node.into(),
            status: status.into(),
            attempt: self.attempts.max(1),
            message: message.into(),
            metadata: BTreeMap::new(),
        });
    }
}
