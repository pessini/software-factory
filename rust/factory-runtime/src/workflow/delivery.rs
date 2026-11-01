//! Publication is a controller operation with saved authorization and remote
//! reconciliation. Specialists never receive credentials for this boundary.
use super::{
    DeliveryPolicy, artifacts,
    handoffs::require,
    repository,
    verified_git::{CommitRecord, VerifiedCommit},
};
use crate::{Error, Result, Runtime, Status};
use async_trait::async_trait;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::future::Future;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct Pull {
    pub number: u64,
    pub url: String,
    pub branch: String,
    pub base: String,
    pub head: String,
    pub draft: bool,
    pub state: String,
    pub body: String,
    pub title: String,
}
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct Checks {
    pub commit: String,
    pub status: String,
    pub checks: Vec<Value>,
}
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ReviewThread {
    pub id: String,
    pub resolved: bool,
    pub outdated: bool,
    pub path: String,
    pub line: Option<u32>,
    pub body: String,
    pub comment_id: u64,
    pub commit: Option<String>,
    pub comments: Vec<Value>,
}
#[async_trait]
pub trait Remote: Send + Sync {
    async fn branch_head(&self, repository: &str, branch: &str) -> Result<Option<String>>;
    async fn push(
        &self,
        repository: &str,
        branch: &str,
        commit: &str,
        previous: Option<&str>,
    ) -> Result<()>;
    async fn find_pull(&self, repository: &str, branch: &str) -> Result<Option<Pull>>;
    async fn create_pull(
        &self,
        repository: &str,
        branch: &str,
        base: &str,
        title: &str,
        body: &str,
    ) -> Result<()>;
    async fn pull(&self, repository: &str, number: u64) -> Result<Pull>;
    async fn checks(&self, repository: &str, commit: &str) -> Result<Checks>;
    async fn review_threads(&self, _repository: &str, _number: u64) -> Result<Vec<ReviewThread>> {
        Err(Error::Config("review threads are unavailable".into()))
    }
    async fn reply(
        &self,
        _repository: &str,
        _number: u64,
        _thread: &ReviewThread,
        _body: &str,
    ) -> Result<()> {
        Err(Error::Config("review replies are unavailable".into()))
    }
    async fn resolve(&self, _thread_id: &str) -> Result<()> {
        Err(Error::Config("thread resolution is unavailable".into()))
    }
    async fn comments(&self, _repository: &str, _number: u64) -> Result<Vec<Value>> {
        Err(Error::Config("PR comments are unavailable".into()))
    }
    async fn comment(&self, _repository: &str, _number: u64, _body: &str) -> Result<()> {
        Err(Error::Config("PR comments are unavailable".into()))
    }
}
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct RemoteOperation {
    pub request: Value,
    pub dispatches: u32,
    pub result: Option<Value>,
}

pub(crate) async fn operation<R, F, E, G>(
    runtime: &Runtime,
    id: &str,
    key: &str,
    request: Value,
    reconcile: R,
    execute: E,
    repeat_if_absent: bool,
) -> Result<Value>
where
    R: Fn() -> F,
    F: Future<Output = Result<Option<Value>>>,
    E: Fn() -> G,
    G: Future<Output = Result<()>>,
{
    let saved = runtime
        .change(id, |run| {
            let operations = &mut run.workflow.as_mut().unwrap().remote_operations;
            if let Some(op) = operations.get(key) {
                require(op.request == request, "remote operation input changed")?;
            } else {
                operations.insert(
                    key.into(),
                    RemoteOperation {
                        request: request.clone(),
                        dispatches: 0,
                        result: None,
                    },
                );
            }
            Ok(())
        })
        .await?;
    let op = &saved.workflow.as_ref().unwrap().remote_operations[key];
    let result = match reconcile().await? {
        Some(found) => found,
        None => {
            if op.dispatches > 0 && !repeat_if_absent {
                return Err(Error::Worker(format!(
                    "uncertain {key}: refusing a duplicate remote mutation"
                )));
            }
            check_cancel(runtime, id).await?;
            runtime
                .change(id, |run| {
                    if run.cancel_requested
                        || run.workflow.as_ref().unwrap().delivery["cancel_requested"] == true
                    {
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
                        .remote_operations
                        .get_mut(key)
                        .unwrap();
                    require(op.dispatches < max, "remote dispatch budget exhausted")?;
                    op.dispatches += 1;
                    Ok(())
                })
                .await?;
            let invocation = execute();
            tokio::pin!(invocation);
            let mut tick = tokio::time::interval(std::time::Duration::from_millis(100));
            loop {
                tokio::select! {result=&mut invocation=>{result?;break;},_=tick.tick()=>check_cancel(runtime,id).await?}
            }

            reconcile().await?.ok_or_else(|| {
                Error::Worker(format!("{key} returned without a confirmed remote result"))
            })?
        }
    };
    runtime
        .change(id, |run| {
            run.workflow
                .as_mut()
                .unwrap()
                .remote_operations
                .get_mut(key)
                .unwrap()
                .result = Some(result.clone());
            Ok(())
        })
        .await?;
    Ok(result)
}
pub(crate) async fn check_cancel(runtime: &Runtime, id: &str) -> Result<()> {
    let run = runtime.status(id).await?;
    if run.cancel_requested || run.workflow.as_ref().unwrap().delivery["cancel_requested"] == true {
        return Err(Error::Cancelled);
    }
    Ok(())
}
pub(crate) fn validate_pull(pull: &Pull, branch: &str, base: &str, commit: &str) -> Result<()> {
    require(
        pull.branch == branch
            && pull.base == base
            && pull.head == commit
            && !pull.draft
            && pull.state == "open",
        "PR head, base, branch or state differs from the verified result",
    )
}
pub(crate) async fn track(
    remote: &dyn Remote,
    policy: &DeliveryPolicy,
    pull: &Pull,
    commit: &str,
) -> Result<Value> {
    let repository = policy.repository.as_ref().unwrap();
    let checks = remote.checks(repository, commit).await?;
    require(
        checks.commit == commit,
        "CI result belongs to another commit",
    )?;
    let latest = remote.pull(repository, pull.number).await?;
    validate_pull(&latest, &pull.branch, &policy.base_branch, commit)?;
    let status = match checks.status.as_str() {
        "passed" => "delivered",
        "failed" => "ci_failed",
        "pending" => "ci_pending",
        "unavailable" => "ci_unavailable",
        _ => return Err(Error::Config("unknown CI status".into())),
    };
    Ok(
        json!({"status":status,"repository":repository,"branch":pull.branch,"commit":commit,"pr_number":pull.number,"pr_url":pull.url,"ci":checks}),
    )
}
pub(crate) async fn policy(
    runtime: &Runtime,
    id: &str,
    authorization: Option<DeliveryPolicy>,
) -> Result<DeliveryPolicy> {
    let run = runtime.status(id).await?;
    let state = run
        .workflow
        .as_ref()
        .ok_or_else(|| Error::Config("delivery requires an implementation run".into()))?;
    let mut selected = authorization.clone().unwrap_or_else(|| {
        state
            .delivery
            .get("policy")
            .and_then(|v| serde_json::from_value(v.clone()).ok())
            .unwrap_or_else(|| {
                run.input
                    .implementation
                    .as_ref()
                    .unwrap()
                    .config
                    .delivery
                    .clone()
            })
    });
    selected.validate()?;
    if selected.issue.is_none()
        && let Some(repository) = &selected.repository
    {
        let pattern = regex::Regex::new(&format!(
            r"https://github\.com/{}/issues/(\d+)\b",
            regex::escape(repository)
        ))
        .unwrap();
        let numbers: std::collections::BTreeSet<_> = pattern
            .captures_iter(&run.input.request)
            .filter_map(|c| c[1].parse::<u64>().ok())
            .collect();
        if numbers.len() == 1 {
            selected.issue = numbers.first().copied();
        }
    }
    if let Some(saved) = state.delivery.get("policy") {
        let mut existing: DeliveryPolicy = serde_json::from_value(saved.clone())?;
        if existing != selected {
            existing.review_fixes = true;
            require(
                existing == selected,
                "saved delivery destination cannot change",
            )?;
        }
    }
    // A manual policy grants no publication authority. Do not freeze a destination
    // until an explicit pull-request authorization exists.
    if selected.mode == "pull_request" {
        runtime
            .change(id, |run| {
                run.workflow.as_mut().unwrap().delivery["policy"] =
                    serde_json::to_value(&selected)?;
                Ok(())
            })
            .await?;
    }
    Ok(selected)
}
pub(crate) async fn deliver(
    runtime: &Runtime,
    id: &str,
    remote: &dyn Remote,
    authorization: Option<DeliveryPolicy>,
) -> Result<crate::Run> {
    let policy = policy(runtime, id, authorization).await?;
    let run = runtime.status(id).await?;
    require(
        run.status == Status::ReadyForReview,
        "delivery requires ready_for_review",
    )?;
    if policy.mode == "manual" {
        return runtime.status(id).await;
    }
    if let Some(child_id) = run.workflow.as_ref().unwrap().delivery["review_fix"]["run_id"].as_str()
        && run.workflow.as_ref().unwrap().delivery["review_fix"]["status"] != "completed"
        && !runtime.status(child_id).await?.status.is_terminal()
    {
        return runtime
            .change(id, |run| {
                run.workflow.as_mut().unwrap().delivery["status"] = json!("fixes_incomplete");
                Ok(())
            })
            .await;
    }
    let result = publish(runtime, id, remote, &policy).await;
    match result {
        Ok(value) => {
            runtime
                .change(id, |run| {
                    let delivery = &mut run.workflow.as_mut().unwrap().delivery;
                    for (key, value) in value
                        .as_object()
                        .ok_or_else(|| Error::Config("invalid delivery result".into()))?
                    {
                        delivery[key] = value.clone();
                    }
                    if delivery["cancel_requested"] == true {
                        delivery["status"] = json!("cancelled");
                    }
                    Ok(())
                })
                .await?;
        }
        Err(error) => {
            let status = match error {
                Error::Config(_) | Error::Conflict(_) | Error::Integrity(_) => "blocked",
                Error::Cancelled => "cancelled",
                _ => "recovery_needed",
            };
            let uncertain = runtime
                .status(id)
                .await?
                .workflow
                .unwrap()
                .remote_operations
                .values()
                .any(|op| op.dispatches > 0 && op.result.is_none());
            runtime
                .change(id, |run| {
                    let state = run.workflow.as_mut().unwrap();
                    if matches!(error, Error::Integrity(_)) {
                        state.test_passed = false;
                        state.review_approved = false;
                        if let Some(handoff) = &mut state.handoff {
                            handoff["evidence_status"] = json!("stale");
                        }
                    }
                    state.delivery["status"] =
                        json!(if uncertain { "recovery_needed" } else { status });
                    state.delivery["error"] = json!(error.to_string());
                    Ok(())
                })
                .await?;
        }
    }
    runtime.status(id).await
}
async fn publish(
    runtime: &Runtime,
    id: &str,
    remote: &dyn Remote,
    policy: &DeliveryPolicy,
) -> Result<Value> {
    artifacts::verify(runtime, id).await?;
    let run = runtime.status(id).await?;
    let state = run.workflow.as_ref().unwrap();
    require(
        state.test_passed && state.review_approved && state.handoff.is_some(),
        "verified evidence is missing or stale",
    )?;
    if let Some(project) = &state.project {
        project.verify()?;
    }
    let repository = policy.repository.as_ref().unwrap();
    let title = policy.title.as_ref().unwrap();
    let commit = if let Some(record) = state.delivery.get("commit_record") {
        serde_json::from_value(record.clone())?
    } else {
        let snapshot = state
            .tested_snapshot
            .as_ref()
            .ok_or_else(|| Error::Config("missing tested snapshot".into()))?;
        let current = repository::snapshot(
            &runtime.config.workspace,
            &run.input
                .implementation
                .as_ref()
                .unwrap()
                .config
                .safety
                .protected_paths,
        )
        .await?;
        if !super::verified_git::same_tested_state(snapshot, &current) {
            return Err(Error::Integrity(
                "files/index/references changed before delivery".into(),
            ));
        }
        let changed = repository::changed_files(&runtime.config.workspace).await?;
        if changed.is_empty() {
            return Ok(json!({"status":"no_change"}));
        }
        require(
            remote
                .branch_head(repository, &policy.base_branch)
                .await?
                .as_deref()
                == Some(&snapshot.head),
            "remote base moved; verify the new base in a fresh run",
        )?;
        let slug = regex::Regex::new("[^a-z0-9]+")
            .unwrap()
            .replace_all(&title.to_ascii_lowercase(), "-")
            .trim_matches('-')
            .chars()
            .take(48)
            .collect::<String>();
        let branch = format!(
            "{}/{}-{}-{id}",
            policy.work_type,
            policy
                .issue
                .map(|n| n.to_string())
                .unwrap_or_else(|| id.into()),
            slug
        );
        let record = VerifiedCommit::prepare(runtime, id, snapshot, title, &branch).await?;
        runtime
            .change(id, |run| {
                run.workflow.as_mut().unwrap().delivery["commit_record"] =
                    serde_json::to_value(&record)?;
                Ok(())
            })
            .await?;
        record
    };
    if let Some(verified_id) = state.delivery["verified_run_id"].as_str() {
        let verified = runtime.status(verified_id).await?;
        let evidence = verified
            .workflow
            .as_ref()
            .ok_or_else(|| Error::Integrity("verified review-fix workflow is missing".into()))?;
        require(
            evidence.parent_run_id.as_deref() == Some(id)
                && verified.status == Status::ReadyForReview
                && evidence.test_passed
                && evidence.review_approved
                && evidence.tested_snapshot.as_ref() == Some(&commit.snapshot),
            "latest review-fix evidence differs from the saved delivery commit",
        )?;
        artifacts::verify(runtime, verified_id).await?;
        VerifiedCommit::require_installed(runtime, id, &commit).await?;
        let number = state.delivery["pr_number"]
            .as_u64()
            .ok_or_else(|| Error::Integrity("saved review-fix PR is missing".into()))?;
        let pull = remote.pull(repository, number).await?;
        validate_pull(&pull, &commit.branch, &policy.base_branch, &commit.commit)?;
        require(
            pull.body
                .contains(&format!("<!-- factory-delivery:{id} -->")),
            "PR does not carry the saved delivery marker",
        )?;
        return track(remote, policy, &pull, &commit.commit).await;
    }
    publish_commit(runtime, id, remote, policy, &commit, None, "delivery").await
}
pub(crate) async fn publish_commit(
    runtime: &Runtime,
    id: &str,
    remote: &dyn Remote,
    policy: &DeliveryPolicy,
    commit: &CommitRecord,
    previous: Option<&str>,
    prefix: &str,
) -> Result<Value> {
    let repository = policy.repository.as_ref().unwrap();
    let title = policy.title.as_ref().unwrap();
    VerifiedCommit::install(runtime, id, commit).await?;
    let pushed = || async {
        let current = remote.branch_head(repository, &commit.branch).await?;
        if current.as_deref() == Some(&commit.commit) {
            Ok(Some(json!({"commit":commit.commit})))
        } else {
            require(
                current.as_deref() == previous,
                "remote delivery branch changed; refusing to overwrite it",
            )?;
            Ok(None)
        }
    };
    let push = || async {
        VerifiedCommit::require_installed(runtime, id, commit).await?;
        remote
            .push(repository, &commit.branch, &commit.commit, previous)
            .await
    };
    operation(runtime,id,&format!("{prefix}:push"),json!({"repository":repository,"branch":commit.branch,"commit":commit.commit,"previous":previous}),pushed,push,true).await?;
    let run = runtime.status(id).await?;
    let marker = format!("<!-- factory-delivery:{id} -->");
    let closes = policy
        .issue
        .map(|n| format!("\n\nCloses issue #{n}.\n\nCloses #{n}"))
        .unwrap_or_default();
    let body = format!(
        "{title}\n\n{}\n\nVerified content: `{}`\n\nRequired local checks and review passed. Remaining manual work: {}.{closes}\n\n{marker}",
        run.input.request,
        run.workflow
            .as_ref()
            .unwrap()
            .content_version
            .as_deref()
            .unwrap_or(""),
        serde_json::to_string(
            &run.workflow
                .as_ref()
                .unwrap()
                .handoff
                .as_ref()
                .map(|h| &h["manual_work"])
        )?
    );
    let existing = || async {
        let pull = remote.find_pull(repository, &commit.branch).await?;
        if let Some(p) = &pull {
            validate_pull(p, &commit.branch, &policy.base_branch, &commit.commit)?;
            require(
                p.body.contains(&marker),
                "PR does not carry the saved delivery marker",
            )?;
        }
        pull.map(serde_json::to_value)
            .transpose()
            .map_err(Error::from)
    };
    let create = || async {
        VerifiedCommit::require_installed(runtime, id, commit).await?;
        remote
            .create_pull(
                repository,
                &commit.branch,
                &policy.base_branch,
                title,
                &body,
            )
            .await
    };
    let pull:Pull=serde_json::from_value(operation(runtime,id,&format!("{prefix}:pull"),json!({"repository":repository,"branch":commit.branch,"base":policy.base_branch,"title":title,"body":body}),existing,create,false).await?)?;
    track(remote, policy, &pull, &commit.commit).await
}
