use crate::{
    CrashPoint, Error, Interaction, Operation, OperationState, Reconciliation, Result, Runtime,
    Status, contracts::GRAPH_VERSION,
};
use adk_graph::{
    edge::{END, START},
    error::GraphError,
    graph::StateGraph,
    node::{ExecutionConfig, NodeOutput},
    state::State,
};
use adk_server::background::{WorkflowExecutor, WorkflowRunContext, WorkflowState};
use async_trait::async_trait;
use chrono::Utc;
use serde_json::{Value, json};
use tokio_util::sync::CancellationToken;

pub(crate) struct FactoryWorkflow(pub Runtime);

#[async_trait]
impl WorkflowExecutor for FactoryWorkflow {
    fn has_workflow(&self, id: &str) -> bool {
        id == GRAPH_VERSION
    }
    async fn execute(
        &self,
        _: &str,
        _: WorkflowState,
        _: CancellationToken,
    ) -> std::result::Result<Value, String> {
        Err("Factory graphs require execute_durable and a saved thread ID".into())
    }
    async fn execute_durable(
        &self,
        workflow: &str,
        input: WorkflowState,
        context: WorkflowRunContext,
    ) -> std::result::Result<Value, String> {
        if !self.has_workflow(workflow) {
            return Err("unknown graph version".into());
        }
        self.run(input, context).await.map_err(|e| e.to_string())
    }
}

impl FactoryWorkflow {
    async fn run(&self, input: WorkflowState, context: WorkflowRunContext) -> Result<Value> {
        let id = input
            .get("run_id")
            .and_then(Value::as_str)
            .ok_or_else(|| Error::Config("missing run_id".into()))?
            .to_owned();
        let run = self.0.status(&id).await?;
        if run.status.is_terminal() {
            return Err(Error::Conflict("terminal run cannot execute".into()));
        }
        if context.thread_id != run.checkpoint_thread_id {
            return Err(Error::Conflict("durable graph thread changed".into()));
        }
        if Utc::now() >= run.deadline_at {
            return Err(Error::Deadline);
        }
        let approval_runtime = self.0.clone();
        let approval_id = id.clone();
        let worker_runtime = self.0.clone();
        let worker_id = id.clone();
        let worker_cancel = context.cancel.clone();
        let graph =
            StateGraph::with_channels(&["result", "declined", "schema_version", "graph_version"])
                .add_node_fn("approval", move |_| {
                    let runtime = approval_runtime.clone();
                    let id = approval_id.clone();
                    async move {
                        let run = runtime
                            .change(&id, |run| {
                                if run.interaction.is_none() {
                                    run.interaction = Some(Interaction {
                                        interaction_id: format!("{}-approval-1", run.run_id),
                                        question: "Run the fake worker?".into(),
                                        approved: None,
                                    });
                                    run.event("approval", "waiting", "Run the fake worker?");
                                }
                                if run.interaction.as_ref().unwrap().approved.is_none() {
                                    run.status = Status::WaitingForInput;
                                }
                                Ok(())
                            })
                            .await
                            .map_err(node_error)?;
                        match run.interaction.unwrap().approved {
                            None => Ok(NodeOutput::interrupt("Run the fake worker?")),
                            Some(false) => Ok(NodeOutput::new()
                                .with_update("declined", json!(true))
                                .with_goto([END])),
                            Some(true) => Ok(NodeOutput::new()),
                        }
                    }
                })
                .add_node_fn("fake_worker", move |_| {
                    let runtime = worker_runtime.clone();
                    let id = worker_id.clone();
                    let cancel = worker_cancel.clone();
                    async move {
                        Ok(NodeOutput::new().with_update(
                            "result",
                            json!(
                                execute_worker(&runtime, &id, cancel)
                                    .await
                                    .map_err(node_error)?
                            ),
                        ))
                    }
                })
                .add_edge(START, "approval")
                .add_edge("approval", "fake_worker")
                .add_edge("fake_worker", END)
                .compile()?
                .with_checkpointer_arc(self.0.services.checkpoints.clone());
        let execution = graph.invoke(
            State::from([
                (
                    "schema_version".into(),
                    json!(crate::contracts::SCHEMA_VERSION),
                ),
                ("graph_version".into(), json!(GRAPH_VERSION)),
            ]),
            ExecutionConfig::new(&context.thread_id),
        );
        tokio::pin!(execution);
        let outcome = loop {
            let remaining = (run.deadline_at - Utc::now()).to_std().unwrap_or_default();
            tokio::select! {
                biased;
                _ = context.cancel.cancelled() => return Err(Error::Cancelled),
                _ = tokio::time::sleep(remaining) => {
                    // Timers are monotonic; saved deadlines use wall time. Recheck
                    // after waking in case the host clock was adjusted backwards.
                    if Utc::now() < run.deadline_at { continue; }
                    context.cancel.cancel();
                    return Err(Error::Deadline);
                },
                result = &mut execution => break result,
            }
        };
        match outcome {
            Ok(state) => {
                Ok(json!({"result": state.get("result"), "declined": state.get("declined")}))
            }
            Err(GraphError::Interrupted(_)) => Ok(json!({"waiting": true})),
            Err(error) => Err(error.into()),
        }
    }
}

fn node_error(error: Error) -> GraphError {
    GraphError::NodeExecutionFailed {
        node: "factory".into(),
        message: error.to_string(),
    }
}

async fn execute_worker(runtime: &Runtime, id: &str, cancel: CancellationToken) -> Result<String> {
    let run = runtime
        .change(id, |run| {
            if run.operation.is_none() {
                run.operation = Some(Operation {
                    operation_id: format!("{}-fake-1", run.run_id),
                    state: OperationState::Prepared,
                    dispatch_count: 0,
                    result: None,
                });
                run.event("fake_worker", "started", "Operation intent saved");
            }
            Ok(())
        })
        .await?;
    let operation = run.operation.unwrap();
    if let Some(result) = operation.result {
        return Ok(result);
    }
    if cancel.is_cancelled() || run.cancel_requested {
        return Err(Error::Cancelled);
    }
    let result = match runtime
        .services
        .worker
        .reconcile(&operation.operation_id, &run.input.request)
        .await?
    {
        Reconciliation::Completed(result) => result,
        Reconciliation::Unknown(reason) => return Err(Error::Worker(reason)),
        Reconciliation::Absent => {
            runtime
                .change(id, |run| {
                    let operation = run.operation.as_mut().unwrap();
                    if operation.dispatch_count >= run.input.max_dispatches {
                        return Err(Error::Worker("dispatch budget exhausted".into()));
                    }
                    operation.state = OperationState::Dispatching;
                    operation.dispatch_count += 1;
                    Ok(())
                })
                .await?;
            runtime.services.faults.hit(CrashPoint::BeforeDispatch);
            let result = runtime
                .services
                .worker
                .dispatch(&operation.operation_id, &run.input.request, cancel)
                .await?;
            runtime.services.faults.hit(CrashPoint::AfterDispatch);
            result
        }
    };
    runtime
        .change(id, |run| {
            let operation = run.operation.as_mut().unwrap();
            operation.state = OperationState::Completed;
            operation.result = Some(result.clone());
            Ok(())
        })
        .await?;
    runtime.services.faults.hit(CrashPoint::AfterResult);
    Ok(result)
}
