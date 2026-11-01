use super::{artifacts, execution, handoffs::ReviewContext, prompts, repository, *};
use crate::{Error, Result, Runtime, Status, runtime::digest};
use adk_graph::{
    edge::{END, START},
    error::GraphError,
    graph::StateGraph,
    node::{ExecutionConfig, NodeOutput},
    state::State,
};
use adk_server::background::{WorkflowRunContext, WorkflowState};
use serde::{Serialize, de::DeserializeOwned};
use serde_json::{Value, json};
use std::collections::{BTreeMap, BTreeSet};
use tokio_util::sync::CancellationToken;

pub(crate) async fn run_graph(
    runtime: &Runtime,
    input: WorkflowState,
    context: WorkflowRunContext,
) -> Result<Value> {
    let id = input
        .get("run_id")
        .and_then(Value::as_str)
        .ok_or_else(|| Error::Config("missing run ID".into()))?
        .to_owned();
    let run = runtime.status(&id).await?;
    handoffs::require(
        run.checkpoint_thread_id == context.thread_id && !run.status.is_terminal(),
        "invalid durable invocation identity",
    )?;
    let mut builder = StateGraph::with_channels(&["schema_version", "graph_version", "result"]);
    for name in [
        "route",
        "bootstrap",
        "baseline",
        "plan",
        "build",
        "test",
        "repair",
        "review",
        "revise",
        "finalize",
        "failed",
    ] {
        let runtime = runtime.clone();
        let id = id.clone();
        let cancel = context.cancel.clone();
        builder = builder.add_node_fn(name, move |_| {
            let runtime = runtime.clone();
            let id = id.clone();
            let cancel = cancel.clone();
            async move {
                let run = runtime.status(&id).await.map_err(node_error)?;
                let next = &run.workflow.as_ref().unwrap().next;
                if next == "done" {
                    return Ok(NodeOutput::new().with_goto([END]));
                }
                if next != name {
                    return Ok(NodeOutput::new().with_goto([next]));
                }
                match Box::pin(phase(&runtime, &id, name, cancel)).await {
                    Ok(()) => {
                        let run = runtime.status(&id).await.map_err(node_error)?;
                        if run.status == Status::WaitingForInput {
                            return Ok(NodeOutput::interrupt(
                                "Saved specialist questions need answers",
                            ));
                        }
                        let next = &run.workflow.as_ref().unwrap().next;
                        Ok(NodeOutput::new().with_goto([if next == "done" { END } else { next }]))
                    }
                    Err(Error::Worker(reason)) => Err(node_error(Error::Worker(reason))),
                    Err(Error::Waiting(_)) => Ok(NodeOutput::interrupt(
                        "Saved provider questions need answers",
                    )),
                    Err(Error::Cancelled) => Err(node_error(Error::Cancelled)),
                    Err(Error::Deadline) => Err(node_error(Error::Deadline)),
                    Err(error) => {
                        runtime
                            .change(&id, |run| {
                                run.workflow.as_mut().unwrap().outcome = Some(Status::Blocked);
                                run.error = Some(error.to_string());
                                let state = run.workflow.as_mut().unwrap();
                                state.test_passed = false;
                                state.review_approved = false;
                                state.next = "failed".into();
                                run.event(name, "failed", &error.to_string());
                                Ok(())
                            })
                            .await
                            .map_err(node_error)?;
                        Ok(NodeOutput::new().with_goto(["failed"]))
                    }
                }
            }
        });
    }
    let graph = builder
        .add_edge(START, "route")
        .compile()?
        .with_checkpointer_arc(runtime.services.checkpoints.clone());
    let invocation = graph.invoke(
        State::from([
            (
                "schema_version".into(),
                json!(crate::contracts::SCHEMA_VERSION),
            ),
            ("graph_version".into(), json!(GRAPH_VERSION)),
        ]),
        ExecutionConfig::new(&context.thread_id).with_recursion_limit(200),
    );
    tokio::pin!(invocation);
    // Do not drop a protected worker future on deadline/cancellation. Signal it,
    // await its confirmed cleanup, then let the supervisor release ownership.
    let deadline = tokio::time::sleep(
        (run.deadline_at - chrono::Utc::now())
            .to_std()
            .unwrap_or_default(),
    );
    tokio::pin!(deadline);
    let result = tokio::select! {
        result = &mut invocation => result,
        _ = &mut deadline => { context.cancel.cancel(); let _ = invocation.await; return Err(Error::Deadline); }
    };
    match result {
        Ok(_) => {
            let run = runtime.status(&id).await?;
            Ok(
                json!({"workflow_status":run.workflow.as_ref().unwrap().outcome,"result":run.workflow.unwrap().handoff.map(|h|h.to_string())}),
            )
        }
        Err(GraphError::Interrupted(_)) => Ok(json!({"waiting":true})),
        Err(error) => Err(error.into()),
    }
}
fn node_error(error: Error) -> GraphError {
    GraphError::NodeExecutionFailed {
        node: "factory-sdlc".into(),
        message: error.to_string(),
    }
}
async fn update(
    runtime: &Runtime,
    id: &str,
    next: &str,
    change: impl Fn(&mut super::WorkflowState),
) -> Result<()> {
    runtime
        .change(id, |run| {
            let state = run.workflow.as_mut().unwrap();
            change(state);
            state.next = next.into();
            run.event(next, "info", "Workflow phase committed");
            Ok(())
        })
        .await?;
    Ok(())
}
async fn current(runtime: &Runtime, id: &str) -> Result<repository::RepositorySnapshot> {
    artifacts::verify(runtime, id).await?;
    let run = runtime.status(id).await?;
    if let Some(project) = &run.workflow.as_ref().unwrap().project {
        project.verify()?;
    }
    repository::snapshot(
        &runtime.config.workspace,
        &run.input
            .implementation
            .unwrap()
            .config
            .safety
            .protected_paths,
    )
    .await
}
fn version(run: &crate::Run, snapshot: &repository::RepositorySnapshot) -> Result<String> {
    Ok(digest(&serde_json::to_vec(
        &json!({"repository":snapshot,"input":run.input,"plan":run.workflow.as_ref().unwrap().plan}),
    )?))
}
async fn require_current(
    runtime: &Runtime,
    id: &str,
    snapshot: &repository::RepositorySnapshot,
) -> Result<()> {
    handoffs::require(
        current(runtime, id).await? == *snapshot,
        "stale evidence: repository files or metadata changed",
    )
}
struct SpecialistTurn<'a> {
    key: &'a str,
    step: &'a str,
    role: &'a str,
}
async fn specialist<T: DeserializeOwned + Serialize>(
    runtime: &Runtime,
    id: &str,
    turn: SpecialistTurn<'_>,
    context: Value,
    cancel: CancellationToken,
    validate: impl Fn(&T) -> Result<()>,
) -> Result<T> {
    let SpecialistTurn { key, step, role } = turn;
    let run = runtime.status(id).await?;
    let observed = current(runtime, id).await?;
    let operation_id = format!("{id}-{key}");
    let before = run
        .workflow
        .as_ref()
        .unwrap()
        .guards
        .get(&operation_id)
        .cloned()
        .unwrap_or(observed);
    runtime
        .change(id, |run| {
            run.workflow
                .as_mut()
                .unwrap()
                .guards
                .entry(operation_id.clone())
                .or_insert(before.clone());
            Ok(())
        })
        .await?;
    let config = &run.input.implementation.as_ref().unwrap().config;
    let execution = run
        .workflow
        .as_ref()
        .unwrap()
        .requests
        .get(&operation_id)
        .map(|r| r.execution.clone())
        .unwrap_or_else(|| ExecutionKind::Specialist {
            role: role.into(),
            step: step.into(),
            agent: config.agent(step).clone(),
            prompt: prompts::prompt(step, context),
        });
    let output = execution::execute(runtime, id, key, execution.clone(), cancel.clone()).await?;
    let after = current(runtime, id).await?;
    before.check_protection(&after, role != "builder")?;
    let parsed = serde_json::from_str::<T>(&output)
        .map_err(Error::from)
        .and_then(|value| {
            validate(&value)?;
            Ok(value)
        });
    match parsed {
        Ok(value) => Ok(value),
        Err(error) => {
            let corrected = execution::execute(
                runtime,
                id,
                &format!("{key}-correction"),
                prompts::correction(&execution, &output, &error.to_string()),
                cancel,
            )
            .await?;
            let latest = current(runtime, id).await?;
            before.check_protection(&latest, role != "builder")?;
            let value = serde_json::from_str(&corrected)?;
            validate(&value)?;
            Ok(value)
        }
    }
}
async fn context(runtime: &Runtime, id: &str) -> Result<Value> {
    let run = runtime.status(id).await?;
    let state = run.workflow.as_ref().unwrap();
    Ok(
        json!({"original_request":run.input.request,"workspace":runtime.config.workspace,"evidence_directory":runtime.config.data_dir.join("evidence").join(id),"approved_plan":run.input.implementation.as_ref().unwrap().approved_plan,"required_criteria":run.input.implementation.as_ref().unwrap().required_criteria,"plan":state.plan,"baseline":state.baseline_checks,"checks":state.checks,"changed_files":repository::changed_files(&runtime.config.workspace).await?,"previous_findings":state.history,"previous_review":state.review,"project":state.project,"saved_answers":state.interactions}),
    )
}
async fn phase(runtime: &Runtime, id: &str, name: &str, cancel: CancellationToken) -> Result<()> {
    let run = runtime.status(id).await?;
    if run.cancel_requested || cancel.is_cancelled() {
        return Err(Error::Cancelled);
    }
    if chrono::Utc::now() >= run.deadline_at {
        return Err(Error::Deadline);
    }
    let state = run.workflow.as_ref().unwrap();
    let input = run.input.implementation.as_ref().unwrap();
    match name {
        "bootstrap" => {
            handoffs::require(
                repository::changed_files(&runtime.config.workspace)
                    .await?
                    .is_empty(),
                "worktree must be clean before a run; user work preserved",
            )?;
            let snapshot = current(runtime, id).await?;
            let project = super::project::Project::capture(
                &runtime.config.workspace,
                &snapshot,
                &input.config.quality.commands,
            )?;
            artifacts::write(runtime, id, "project.json", &project).await?;
            artifacts::write(runtime, id, "config.json", &input.config).await?;
            artifacts::write(
                runtime,
                id,
                "request.json",
                &json!({"original_request":run.input.request,"approved_plan":input.approved_plan}),
            )
            .await?;
            artifacts::write(runtime, id, "protection.json", &snapshot).await?;
            artifacts::write(runtime, id, "review-rules.json", &prompts::REVIEW_RULES).await?;
            update(runtime, id, "baseline", |s| {
                s.baseline = Some(snapshot.clone());
                s.project = Some(project.clone());
            })
            .await
        }
        "baseline" => {
            let checks = checks(runtime, id, "baseline", 1, cancel).await?;
            artifacts::write(runtime, id, "baseline.json", &checks).await?;
            update(runtime, id, "plan", |s| s.baseline_checks = checks.clone()).await
        }
        "plan" => {
            let round = state
                .interactions
                .keys()
                .filter(|key| key.starts_with(&format!("{id}:plan:")))
                .count() as u32
                + 1;
            let mut ctx = context(runtime, id).await?;
            let mut required = input.required_criteria.clone();
            if let Some(approved) = &input.approved_plan {
                required.extend(approved.completion_checks.clone());
            }
            ctx["required_criteria"] = serde_json::to_value(&required)?;
            let plan: Plan = specialist(
                runtime,
                id,
                SpecialistTurn {
                    key: &format!("plan-{round}"),
                    step: "plan",
                    role: "planner",
                },
                ctx,
                cancel,
                Plan::validate,
            )
            .await?;
            if plan.status == "needs_input" {
                let question = SavedInteraction {
                    interaction_id: format!("{id}:plan:{round}"),
                    questions: plan.questions.clone(),
                    answers: None,
                };
                artifacts::write(runtime, id, &format!("plan/questions-{round}.json"), &plan)
                    .await?;
                runtime
                    .change(id, |run| {
                        run.status = Status::WaitingForInput;
                        run.workflow.as_mut().unwrap().interaction = Some(question.clone());
                        run.workflow
                            .as_mut()
                            .unwrap()
                            .wait_started_at
                            .get_or_insert(chrono::Utc::now());
                        run.event("plan", "waiting", "Planner questions saved");
                        Ok(())
                    })
                    .await?;
                return Ok(());
            }
            handoffs::require(
                plan.status == "ready" && plan.questions.is_empty(),
                "Planner blocked",
            )?;
            let mut plan = plan;
            let required_ids: BTreeSet<_> = required.iter().map(|c| &c.id).collect();
            plan.acceptance_criteria
                .retain(|c| !required_ids.contains(&c.id));
            plan.acceptance_criteria.extend(required);
            plan.validate()?;
            artifacts::write(runtime, id, "plan.json", &plan).await?;
            update(runtime, id, "build", |s| s.plan = Some(plan.clone())).await
        }
        "build" | "repair" | "revise" => {
            let (step, key, count) = match name {
                "repair" => (
                    "repair",
                    format!("repair-{}", state.test_repair_count + 1),
                    state.test_repair_count + 1,
                ),
                "revise" => (
                    "revision",
                    format!("revision-{}", state.review_revision_count + 1),
                    state.review_revision_count + 1,
                ),
                _ => ("build", "build-1".into(), 0),
            };
            let build: Build = specialist(
                runtime,
                id,
                SpecialistTurn {
                    key: &key,
                    step,
                    role: "builder",
                },
                context(runtime, id).await?,
                cancel,
                Build::validate,
            )
            .await?;
            handoffs::require(
                build.status == "completed",
                &format!("Builder blocked: {}", build.summary),
            )?;
            let actual = repository::changed_files(&runtime.config.workspace).await?;
            handoffs::require(
                build.changed_files.iter().collect::<BTreeSet<_>>() == actual.iter().collect(),
                "Builder report does not match the actual complete diff",
            )?;
            artifacts::write(runtime, id, &format!("build/{key}.json"), &build).await?;
            update(runtime, id, "test", |s| {
                s.build = Some(build.clone());
                s.test_passed = false;
                s.review_approved = false;
                if name == "repair" {
                    s.test_repair_count = count;
                }
                if name == "revise" {
                    s.review_revision_count = count;
                }
            })
            .await
        }
        "test" => {
            let attempt = state.test_attempt_count + 1;
            let checks = checks(runtime, id, "tests", attempt, cancel).await?;
            let snapshot = current(runtime, id).await?;
            let saved = runtime.status(id).await?;
            let content = version(&saved, &snapshot)?;
            let passed = checks.iter().all(CheckResult::passed);
            let baseline_only = checks
                .iter()
                .filter(|c| !c.passed())
                .all(|c| c.comparison != "new_failure");
            let next = if passed {
                "review"
            } else if !baseline_only
                && state.test_repair_count < input.config.limits.max_test_repairs
            {
                "repair"
            } else {
                "failed"
            };
            update(runtime, id, next, |s| {
                s.checks = checks.clone();
                s.test_attempt_count = attempt;
                s.test_passed = passed;
                s.review_approved = false;
                s.content_version = Some(content.clone());
                s.tested_snapshot = Some(snapshot.clone());
            })
            .await?;
            if !passed {
                runtime
                    .change(id, |run| {
                        run.workflow.as_mut().unwrap().outcome =
                            baseline_only.then_some(Status::Blocked);
                        run.error = Some(
                            if baseline_only {
                                "baseline failures require user action"
                            } else {
                                "configured quality commands failed"
                            }
                            .into(),
                        );
                        Ok(())
                    })
                    .await?;
            }
            Ok(())
        }
        "review" => review(runtime, id, cancel).await,
        "finalize" => {
            let snapshot = state
                .tested_snapshot
                .as_ref()
                .ok_or_else(|| Error::Config("missing tested snapshot".into()))?;
            require_current(runtime, id, snapshot).await?;
            handoffs::require(
                state.test_passed
                    && state.review_approved
                    && state.checks.len() == input.config.quality.commands.len()
                    && state.checks.iter().all(CheckResult::passed),
                "required checks/review are incomplete",
            )?;
            let handoff = json!({"version":1,"status":"ready_for_review","original_request":run.input.request,"plan":state.plan,"changed_files":repository::changed_files(&runtime.config.workspace).await?,"content_version":state.content_version,"checks":state.checks,"review":state.review,"manual_work":state.build.as_ref().map(|b|b.manual_work.clone()).unwrap_or_default().into_iter().chain(state.review.as_ref().map(|r|r.manual_work.clone()).unwrap_or_default()).collect::<BTreeSet<_>>(),"change":state.build.as_ref().map(|b|&b.summary),"criteria":state.review.as_ref().map(|r|&r.criteria),"findings":state.review.as_ref().map(|r|&r.findings),"review_history":state.history,"human_approval":"required outside this workflow"});
            artifacts::write(runtime, id, "handoff.json", &handoff).await?;
            update(runtime, id, "done", |s| s.handoff = Some(handoff.clone())).await?;
            runtime
                .change(id, |run| {
                    if !run.cancel_requested {
                        run.workflow.as_mut().unwrap().outcome = Some(Status::ReadyForReview);
                        run.error = None;
                    }
                    Ok(())
                })
                .await?;
            Ok(())
        }
        "failed" => {
            runtime
                .change(id, |run| {
                    if run.workflow.as_ref().unwrap().outcome != Some(Status::Blocked) {
                        run.workflow.as_mut().unwrap().outcome = Some(Status::Failed);
                    }
                    run.workflow.as_mut().unwrap().next = "done".into();
                    Ok(())
                })
                .await?;
            Ok(())
        }
        _ => Err(Error::Config("unknown workflow phase".into())),
    }
}
async fn checks(
    runtime: &Runtime,
    id: &str,
    phase: &str,
    attempt: u32,
    cancel: CancellationToken,
) -> Result<Vec<CheckResult>> {
    let run = runtime.status(id).await?;
    let input = run.input.implementation.as_ref().unwrap();
    let before = current(runtime, id).await?;
    let content = version(&run, &before)?;
    artifacts::write(
        runtime,
        id,
        &format!("{phase}/attempt-{attempt:02}-content.json"),
        &json!({"repository":before,"content_version":content}),
    )
    .await?;
    let mut results = vec![];
    for (index, command) in input.config.quality.commands.iter().enumerate() {
        require_current(runtime, id, &before).await?;
        let output = execution::execute(
            runtime,
            id,
            &format!("{phase}-{attempt}-{}", command.name),
            ExecutionKind::Quality {
                command: command.clone(),
                content_version: content.clone(),
            },
            cancel.clone(),
        )
        .await?;
        require_current(runtime, id, &before).await?;
        let mut result: CheckResult = serde_json::from_str(&output)?;
        handoffs::require(
            result.name == command.name
                && result.argv == command.argv
                && result.content_version == content,
            "quality result does not match reviewed command/content",
        )?;
        if phase == "tests" {
            result.compare(&run.workflow.as_ref().unwrap().baseline_checks[index]);
        }
        results.push(result);
    }
    artifacts::write(
        runtime,
        id,
        &format!("{phase}/attempt-{attempt:02}.json"),
        &results,
    )
    .await?;
    Ok(results)
}
async fn review(runtime: &Runtime, id: &str, cancel: CancellationToken) -> Result<()> {
    let run = runtime.status(id).await?;
    let state = run.workflow.as_ref().unwrap();
    let input = run.input.implementation.as_ref().unwrap();
    let snapshot = state.tested_snapshot.as_ref().unwrap();
    require_current(runtime, id, snapshot).await?;
    let attempt = state.review_attempt_count + 1;
    let changed = repository::changed_files(&runtime.config.workspace).await?;
    let patches = repository::patches(&runtime.config.workspace, &changed).await?;
    let checks_path = format!("tests/attempt-{:02}.json", state.test_attempt_count);
    let mut refs: BTreeSet<_> = [
        checks_path.clone(),
        format!("tests/attempt-{:02}-content.json", state.test_attempt_count),
        "baseline/attempt-01-content.json".into(),
    ]
    .into();
    for (index, (path, patch)) in patches.iter().enumerate() {
        refs.insert(
            artifacts::write(
                runtime,
                id,
                &format!("patches/review-{attempt}-{index}.json"),
                &json!({"path":path,"patch":patch}),
            )
            .await?,
        );
    }
    refs.extend(
        snapshot
            .files
            .iter()
            .filter(|(_, e)| e.kind == "file")
            .map(|(p, _)| format!("source:{p}")),
    );
    let source_lines = snapshot
        .files
        .iter()
        .map(|(p, e)| (p.clone(), e.lines))
        .collect();
    let validation = ReviewContext {
        plan: state.plan.as_ref().unwrap(),
        content_version: state.content_version.as_ref().unwrap(),
        evidence_refs: &refs,
        source_lines: &source_lines,
    };
    let selection = if input.config.review_mode == ReviewMode::RiskBased {
        let selection = super::review_policy::select(
            &input.config.review_policy,
            &patches,
            state.content_version.as_ref().unwrap(),
            state.baseline.as_ref().unwrap(),
            snapshot,
        )?;
        artifacts::write(
            runtime,
            id,
            &format!("review/selection-{attempt:02}.json"),
            &selection,
        )
        .await?;
        runtime
            .change(id, |run| {
                run.workflow.as_mut().unwrap().review_selection = Some(selection.clone());
                Ok(())
            })
            .await?;
        Some(selection)
    } else {
        None
    };
    let bundle = json!({"review_selection":selection,"content_version":state.content_version,"checks":checks_path,"evidence_refs":refs,"changed_files":changed,"patches":patches,"context":{"plan":state.plan,"baseline":state.baseline_checks,"checks":state.checks}});
    let mut ctx = context(runtime, id).await?;
    ctx["bundle"] = bundle;
    let mut reviews = BTreeMap::new();
    if input.config.review_mode != ReviewMode::Single {
        let roles = selection
            .as_ref()
            .map(|s| s.required_reviewers.clone())
            .unwrap_or_else(|| vec!["request".into(), "code".into(), "security".into()]);
        for role in roles.iter().map(String::as_str) {
            require_current(runtime, id, snapshot).await?;
            let step = format!("{role}_review");
            let focused: FocusedReview = specialist(
                runtime,
                id,
                SpecialistTurn {
                    key: &format!("{step}-{attempt}"),
                    step: &step,
                    role: &format!("{role}_reviewer"),
                },
                ctx.clone(),
                cancel.clone(),
                |r| validation.focused(role, r),
            )
            .await?;
            artifacts::write(
                runtime,
                id,
                &format!("review/{role}-{attempt:02}.json"),
                &focused,
            )
            .await?;
            reviews.insert(role.to_owned(), focused);
        }
    }
    let focused = (input.config.review_mode != ReviewMode::Single).then_some(&reviews);
    let step = if focused.is_some() { "judge" } else { "review" };
    ctx["reviews"] = serde_json::to_value(&reviews)?;
    let decision: Review = specialist(
        runtime,
        id,
        SpecialistTurn {
            key: &format!("{step}-{attempt}"),
            step,
            role: if focused.is_some() {
                "judge"
            } else {
                "reviewer"
            },
        },
        ctx,
        cancel,
        |r| validation.decision(r, &state.history, focused).map(|_| ()),
    )
    .await?;
    let history = validation.decision(&decision, &state.history, focused)?;
    artifacts::write(
        runtime,
        id,
        &format!("review/findings-{attempt:02}.json"),
        &history,
    )
    .await?;
    require_current(runtime, id, snapshot).await?;
    artifacts::write(
        runtime,
        id,
        &format!("review/decision-{attempt:02}.json"),
        &decision,
    )
    .await?;
    let next = if decision.approved {
        "finalize"
    } else if state.review_revision_count < input.config.limits.max_review_revisions {
        "revise"
    } else {
        "failed"
    };
    update(runtime, id, next, |s| {
        s.review_attempt_count = attempt;
        s.review_approved = decision.approved;
        s.review = Some(decision.clone());
        s.history = history.clone();
    })
    .await
}
