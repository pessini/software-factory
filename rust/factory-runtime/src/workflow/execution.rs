use super::{AgentConfig, QualityCommand};
use crate::{
    CrashPoint, Error, Operation, OperationState, Reconciliation, Result, Runtime, Status,
    runtime::digest,
};
use serde::{Deserialize, Serialize};
use tokio_util::sync::CancellationToken;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum ExecutionKind {
    Specialist {
        role: String,
        step: String,
        agent: AgentConfig,
        prompt: String,
    },
    Quality {
        command: QualityCommand,
        content_version: String,
    },
}
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ExecutionRequest {
    pub version: u32,
    pub operation_id: String,
    pub run_id: String,
    pub workspace: String,
    pub evidence_directory: String,
    pub protected_paths: Vec<String>,
    pub deadline_at: chrono::DateTime<chrono::Utc>,
    pub execution: ExecutionKind,
}
impl ExecutionRequest {
    pub fn read_only(&self) -> bool {
        !matches!(&self.execution, ExecutionKind::Specialist { role, .. } if role == "builder")
    }
}
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct CheckResult {
    pub name: String,
    pub argv: Vec<String>,
    pub exit_code: Option<i32>,
    pub timed_out: bool,
    pub cancelled: bool,
    pub output_tail: String,
    pub content_version: String,
    pub comparison: String,
}
impl CheckResult {
    pub fn passed(&self) -> bool {
        self.exit_code == Some(0) && !self.timed_out && !self.cancelled
    }
    pub fn compare(&mut self, baseline: &Self) {
        self.comparison = if self.passed() {
            if baseline.passed() {
                "passed"
            } else {
                "baseline_fixed"
            }
        } else if baseline.passed() {
            "new_failure"
        } else if (self.exit_code, self.timed_out, &self.output_tail)
            == (
                baseline.exit_code,
                baseline.timed_out,
                &baseline.output_tail,
            )
        {
            "unchanged_baseline_failure"
        } else {
            "changed_baseline_failure"
        }
        .into();
    }
}

pub(crate) async fn execute(
    runtime: &Runtime,
    id: &str,
    key: &str,
    proposed: ExecutionKind,
    cancel: CancellationToken,
) -> Result<String> {
    let run = runtime.status(id).await?;
    let input = run.input.implementation.as_ref().unwrap();
    let operation_id = format!("{id}-{key}");
    let state = run.workflow.as_ref().unwrap();
    // Reuse the complete immutable request (especially its wall-clock deadline)
    // after restart rather than manufacturing a new provider request.
    let request = state
        .requests
        .get(&operation_id)
        .cloned()
        .unwrap_or_else(|| ExecutionRequest {
            version: 1,
            operation_id: operation_id.clone(),
            run_id: id.into(),
            workspace: runtime.config.workspace.to_string_lossy().into_owned(),
            evidence_directory: runtime
                .config
                .data_dir
                .join("evidence")
                .join(id)
                .to_string_lossy()
                .into_owned(),
            protected_paths: super::repository::protected_paths(
                &input.config.safety.protected_paths,
                &state
                    .baseline
                    .as_ref()
                    .map(|s| s.files.clone())
                    .unwrap_or_default(),
            ),
            deadline_at: run.deadline_at.min(
                chrono::Utc::now()
                    + chrono::Duration::seconds(
                        match &proposed {
                            ExecutionKind::Quality { command, .. } => command.timeout_seconds,
                            _ => input.config.limits.step_timeout_seconds,
                        }
                        .into(),
                    ),
            ),
            execution: proposed.clone(),
        });
    if request.execution != proposed {
        return Err(Error::Conflict(
            "operation input/settings changed during replay".into(),
        ));
    }
    let saved = runtime
        .change(id, |run| {
            let workflow = run.workflow.as_mut().unwrap();
            workflow
                .requests
                .entry(operation_id.clone())
                .or_insert(request.clone());
            workflow
                .operations
                .entry(operation_id.clone())
                .or_insert(Operation {
                    operation_id: operation_id.clone(),
                    state: OperationState::Prepared,
                    dispatch_count: 0,
                    result: None,
                });
            Ok(())
        })
        .await?;
    let op = &saved.workflow.as_ref().unwrap().operations[&operation_id];
    if let Some(result) = &op.result {
        return Ok(result.clone());
    }
    if cancel.is_cancelled() || saved.cancel_requested {
        return Err(Error::Cancelled);
    }
    let encoded = serde_json::to_string(&request)?;
    let result = match runtime
        .services
        .worker
        .reconcile(&operation_id, &encoded)
        .await?
    {
        Reconciliation::Completed(result) => result,
        Reconciliation::Unknown(reason) => return Err(Error::Worker(reason)),
        Reconciliation::Waiting(reason) => return Err(Error::Waiting(reason)),
        Reconciliation::Absent => {
            if chrono::Utc::now() >= request.deadline_at {
                return Err(Error::Deadline);
            }
            // Verification can repeat only when the reviewed command opted in.
            if op.dispatch_count > 0
                && matches!(&proposed, ExecutionKind::Quality { command,.. } if !command.repeat_on_recovery)
            {
                return Err(Error::Worker(
                    "interrupted quality command is not authorized to repeat".into(),
                ));
            }
            runtime
                .change(id, |run| {
                    if run.cancel_requested {
                        return Err(Error::Cancelled);
                    }
                    let max = run
                        .input
                        .implementation
                        .as_ref()
                        .unwrap()
                        .config
                        .limits
                        .connection_retries
                        + 1;
                    let op = run
                        .workflow
                        .as_mut()
                        .unwrap()
                        .operations
                        .get_mut(&operation_id)
                        .unwrap();
                    if op.dispatch_count >= max {
                        return Err(Error::Config(
                            "saved operation dispatch budget exhausted".into(),
                        ));
                    }
                    op.state = OperationState::Dispatching;
                    op.dispatch_count += 1;
                    run.event(
                        key,
                        "started",
                        &format!("Operation {} intent saved", digest(encoded.as_bytes())),
                    );
                    Ok(())
                })
                .await?;
            runtime
                .services
                .faults
                .hit_operation(CrashPoint::BeforeDispatch, &operation_id);
            let result = runtime
                .services
                .worker
                .dispatch(&operation_id, &encoded, cancel)
                .await?;
            runtime
                .services
                .faults
                .hit_operation(CrashPoint::AfterDispatch, &operation_id);
            result
        }
    };
    runtime
        .change(id, |run| {
            let op = run
                .workflow
                .as_mut()
                .unwrap()
                .operations
                .get_mut(&operation_id)
                .unwrap();
            op.state = OperationState::Completed;
            op.result = Some(result.clone());
            run.event(key, "completed", "Worker result saved");
            Ok(())
        })
        .await?;
    runtime
        .services
        .faults
        .hit_operation(CrashPoint::AfterResult, &operation_id);
    Ok(result)
}

pub(crate) async fn stop_all(runtime: &Runtime, id: &str) -> Result<()> {
    let run = runtime.status(id).await?;
    if let Some(state) = run.workflow {
        for (operation, request) in state.requests {
            runtime
                .services
                .worker
                .stop_operation(&operation, &serde_json::to_string(&request)?)
                .await?;
        }
    }
    Ok(())
}

pub(crate) async fn recovery(runtime: &Runtime, id: &str, reason: &str) -> Result<()> {
    runtime
        .change(id, |run| {
            run.status = Status::RecoveryNeeded;
            run.error = Some(reason.into());
            run.event("recovery", "waiting", reason);
            Ok(())
        })
        .await?;
    Ok(())
}
