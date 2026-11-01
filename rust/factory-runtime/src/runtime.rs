use crate::{
    Config, Error, FactoryStore, FakeWorker, Faults, NoFaults, Result, Run, SqliteFactoryStore,
    StartRequest, Status, Worker,
    contracts::{GRAPH_VERSION, SCHEMA_VERSION},
};
use adk_core::{DurableRunStore, NewRun, RunCheckpointRef, RunOutcome, RunState};
use adk_graph::checkpoint::{Checkpointer, SqliteCheckpointer};
use adk_server::background::{WorkflowExecutor, WorkflowRunContext};
use adk_session::{
    CreateRequest, GetRequest, SessionService, SqliteRunStore, SqliteSessionService,
};
use chrono::Utc;
use serde_json::json;
use sha2::{Digest, Sha256};
use std::{
    fs::{File, OpenOptions},
    sync::Arc,
    time::Duration,
};
use tokio_util::sync::CancellationToken;

const LEASE: Duration = Duration::from_secs(2);

/// Injectable boundaries; deployment construction always uses file-backed stores.
pub struct Services {
    pub store: Arc<dyn FactoryStore>,
    pub queue: Arc<dyn DurableRunStore>,
    pub sessions: Arc<dyn SessionService>,
    pub checkpoints: Arc<dyn Checkpointer>,
    pub worker: Arc<dyn Worker>,
    pub faults: Arc<dyn Faults>,
}

#[derive(Clone)]
pub struct Runtime {
    pub(crate) config: Config,
    pub(crate) services: Arc<Services>,
    pub(crate) workspace_id: String,
    delivery_remote: Arc<dyn crate::workflow::delivery::Remote>,
}

impl Runtime {
    pub async fn open(config: Config) -> Result<Self> {
        if config.data_dir.join("docker-backend.json").exists() {
            let docker = serde_json::from_slice(&crate::workflow::artifacts::regular(
                &config.data_dir.join("docker-backend.json"),
            )?)?;
            let worker = Arc::new(crate::workflow::docker::DockerWorker::open(
                config.clone(),
                docker,
            )?);
            return Self::open_with_worker(config, worker, Arc::new(NoFaults)).await;
        }
        let worker = Arc::new(FakeWorker::open(&config.data_dir.join("worker-records")).await?);
        Self::open_with_worker(config, worker, Arc::new(NoFaults)).await
    }

    pub async fn open_docker(
        config: Config,
        docker: crate::workflow::docker::DockerConfig,
    ) -> Result<Self> {
        let docker = crate::workflow::docker::DockerWorker::pin_image(docker).await?;
        let path = config.data_dir.join("docker-backend.json");
        let worker = Arc::new(crate::workflow::docker::DockerWorker::open(
            config.clone(),
            docker.clone(),
        )?);
        let docker = worker.backend().clone();
        if path.exists() {
            let saved: crate::workflow::docker::DockerConfig =
                serde_json::from_slice(&crate::workflow::artifacts::regular(&path)?)?;
            if saved != docker {
                return Err(Error::Conflict(
                    "Docker backend is already pinned for this data directory".into(),
                ));
            }
        } else {
            crate::workflow::artifacts::atomic(&path, &serde_json::to_vec_pretty(&docker)?)?;
        }
        Self::open_with_worker(config, worker, Arc::new(NoFaults)).await
    }

    pub async fn open_with_worker(
        config: Config,
        worker: Arc<dyn Worker>,
        faults: Arc<dyn Faults>,
    ) -> Result<Self> {
        let pool = crate::config::pool(&config.data_dir.join("factory-rust.sqlite")).await?;
        let store = Arc::new(SqliteFactoryStore::new(pool.clone()).await?);
        let sessions = Arc::new(SqliteSessionService::from_pool(pool.clone()));
        sessions.migrate().await?;
        let queue = Arc::new(SqliteRunStore::from_pool(pool.clone()));
        queue.migrate().await?;
        let checkpoints = Arc::new(SqliteCheckpointer::from_pool(pool).await?);
        Ok(Self::from_services(
            config,
            Services {
                store,
                sessions,
                queue,
                checkpoints,
                worker,
                faults,
            },
        ))
    }

    pub fn from_services(config: Config, services: Services) -> Self {
        let workspace_id = digest(config.workspace.to_string_lossy().as_bytes());
        let delivery_remote = Arc::new(crate::workflow::github::GitHub {
            workspace: config.workspace.clone(),
        });
        Self {
            config,
            services: Arc::new(services),
            workspace_id,
            delivery_remote,
        }
    }

    pub fn with_delivery_remote(
        mut self,
        remote: Arc<dyn crate::workflow::delivery::Remote>,
    ) -> Self {
        self.delivery_remote = remote;
        self
    }

    pub async fn start(&self, mut input: StartRequest) -> Result<Run> {
        input.validate()?;
        let run_id = input.run_id();
        match self.status(&run_id).await {
            Ok(saved) => {
                let mut expected = saved.input.clone();
                if input
                    .implementation
                    .as_ref()
                    .is_some_and(|i| i.config.resolved_settings.is_none())
                    && let Some(implementation) = &mut expected.implementation
                {
                    implementation.config.resolved_settings = None;
                }
                if expected != input {
                    return Err(Error::Conflict(
                        "request ID already has different input".into(),
                    ));
                }
                return self.ensure_scheduled(&saved).await;
            }
            Err(Error::NotFound(_)) => (),
            Err(error) => return Err(error),
        }
        if let Some(implementation) = &mut input.implementation
            && implementation.config.resolved_settings.is_none()
        {
            implementation.config.resolve(
                &crate::workflow::config::Selection::default(),
                self.services.worker.model_catalog().await?,
            )?;
        }
        let implementation = input.implementation.is_some();
        let now = Utc::now();
        let deadline_at = now
            + chrono::Duration::seconds(
                input
                    .implementation
                    .as_ref()
                    .map(|i| {
                        i.config
                            .limits
                            .total_run_timeout_seconds
                            .min(i.config.limits.overall_run_timeout_seconds)
                            .min(input.timeout_seconds)
                    })
                    .unwrap_or(input.timeout_seconds)
                    .into(),
            );
        let workflow = input.implementation.as_ref().map(|i| {
            Box::new(crate::workflow::WorkflowState {
                overall_deadline_at: Some(
                    now + chrono::Duration::seconds(
                        i.config.limits.overall_run_timeout_seconds.into(),
                    ),
                ),
                ..Default::default()
            })
        });
        let mut run = Run {
            schema_version: SCHEMA_VERSION,
            graph_version: if input.planning.is_some() {
                crate::planning::GRAPH_VERSION
            } else if implementation {
                crate::workflow::GRAPH_VERSION
            } else {
                GRAPH_VERSION
            }
            .into(),
            checkpoint_thread_id: format!("factory:{run_id}"),
            run_id,
            workspace_id: self.workspace_id.clone(),
            scheduler_id: None,
            deadline_at,
            input,
            revision: 0,
            status: Status::Pending,
            interaction: None,
            operation: None,
            attempts: 0,
            cancel_requested: false,
            result: None,
            error: None,
            events: Vec::new(),
            workflow,
        };
        run.event("start", "started", "Durable fake run saved");
        let saved = self.services.store.create(&run).await?;
        self.ensure_scheduled(&saved).await
    }

    pub async fn status(&self, id: &str) -> Result<Run> {
        let run = self.services.store.load(id).await?;
        run.check_version()?;
        if run.workspace_id != self.workspace_id {
            return Err(Error::Conflict(
                "run belongs to a different workspace".into(),
            ));
        }
        Ok(run)
    }

    pub async fn list(&self) -> Result<Vec<Run>> {
        Ok(self
            .services
            .store
            .list()
            .await?
            .into_iter()
            .filter(|r| r.workspace_id == self.workspace_id)
            .collect())
    }

    pub async fn answer(&self, id: &str, interaction: &str, approved: bool) -> Result<Run> {
        let run = self
            .change(id, |run| {
                let question = run
                    .interaction
                    .as_mut()
                    .ok_or_else(|| Error::Conflict("run has no question".into()))?;
                if question.interaction_id != interaction {
                    return Err(Error::Conflict("stale interaction ID".into()));
                }
                if let Some(previous) = question.approved {
                    if previous != approved {
                        return Err(Error::Conflict(
                            "interaction already has a different answer".into(),
                        ));
                    }
                    return Ok(());
                }
                if run.status.is_terminal() || run.cancel_requested {
                    return Err(Error::Conflict("run cannot accept an answer".into()));
                }
                question.approved = Some(approved);
                run.event("approval", "info", "Answer saved");
                Ok(())
            })
            .await?;
        let run = self.ensure_scheduled(&run).await?;
        if !run.status.is_terminal() {
            self.services
                .queue
                .wake(run.scheduler_id.as_deref().unwrap())
                .await?;
        }
        self.status(id).await
    }

    pub async fn answer_questions(
        &self,
        id: &str,
        interaction: &str,
        answers: std::collections::BTreeMap<String, String>,
    ) -> Result<Run> {
        let run = self
            .change(id, |run| {
                let state = run
                    .workflow
                    .as_mut()
                    .ok_or_else(|| Error::Conflict("not an implementation run".into()))?;
                if let Some(previous) = state.interactions.get(interaction) {
                    if previous.answers.as_ref() != Some(&answers) {
                        return Err(Error::Conflict(
                            "interaction already has different answers".into(),
                        ));
                    }
                    return Ok(());
                }
                if run.status.is_terminal() || run.cancel_requested {
                    return Err(Error::Conflict("run cannot accept answers".into()));
                }
                let mut question = state
                    .interaction
                    .clone()
                    .ok_or_else(|| Error::Conflict("no pending specialist question".into()))?;
                if question.interaction_id != interaction
                    || answers.keys().collect::<std::collections::BTreeSet<_>>()
                        != question.questions.iter().map(|q| &q.id).collect()
                    || answers.values().any(|a| a.trim().is_empty())
                {
                    return Err(Error::Conflict(
                        "answers must match the saved question IDs and contain nonempty text"
                            .into(),
                    ));
                }
                question.answers = Some(answers.clone());
                state.interactions.insert(interaction.into(), question);
                state.interaction = None;
                if let Some(started) = state.wait_started_at.take() {
                    run.deadline_at = (run.deadline_at + (Utc::now() - started))
                        .min(state.overall_deadline_at.unwrap_or(run.deadline_at));
                }
                run.status = Status::Running;
                run.event("answer", "info", "Specialist answers saved");
                Ok(())
            })
            .await?;
        let run = self.ensure_scheduled(&run).await?;
        if !run.status.is_terminal() {
            self.services
                .queue
                .wake(run.scheduler_id.as_deref().unwrap())
                .await?;
        }
        self.status(id).await
    }

    pub async fn cancel(&self, id: &str) -> Result<Run> {
        self.change(id, |run| {
            if run.delivery_pending() {
                run.workflow.as_mut().unwrap().delivery["cancel_requested"] = json!(true);
                run.workflow.as_mut().unwrap().delivery["status"] = json!("stopping");
            }
            if !run.status.is_terminal() && !run.cancel_requested {
                run.cancel_requested = true;
                run.status = Status::Stopping;
                run.event("cancel", "info", "Cancellation requested");
            }
            Ok(())
        })
        .await?;
        self.resume(id).await
    }

    pub async fn deliver(
        &self,
        id: &str,
        authorization: Option<crate::workflow::DeliveryPolicy>,
    ) -> Result<Run> {
        self.deliver_with_remote(id, self.delivery_remote.as_ref(), authorization)
            .await
    }

    pub async fn deliver_with_remote(
        &self,
        id: &str,
        remote: &dyn crate::workflow::delivery::Remote,
        authorization: Option<crate::workflow::DeliveryPolicy>,
    ) -> Result<Run> {
        let _ownership = self.workspace_lock().await?;
        let run = self.status(id).await?;
        if run.workflow.is_none() || run.status != Status::ReadyForReview {
            return Err(Error::Config(
                "delivery requires a verified implementation run".into(),
            ));
        }
        self.reserve_workspace(&run)?;
        let result = Box::pin(crate::workflow::delivery::deliver(
            self,
            id,
            remote,
            authorization,
        ))
        .await;
        let run = self.status(id).await?;
        self.materialize(&run)?;
        if run.workflow.as_ref().unwrap().delivery["status"] != "recovery_needed" {
            self.release_workspace(&run).await?;
        }
        result
    }

    pub async fn fix_reviews(&self, id: &str, threads: &[String]) -> Result<Run> {
        self.fix_reviews_with_remote(id, self.delivery_remote.as_ref(), threads)
            .await
    }

    pub async fn fix_reviews_with_remote(
        &self,
        id: &str,
        remote: &dyn crate::workflow::delivery::Remote,
        threads: &[String],
    ) -> Result<Run> {
        let batch = {
            let _ownership = self.workspace_lock().await?;
            let run = self.status(id).await?;
            if run.workflow.is_none() || run.status != Status::ReadyForReview {
                return Err(Error::Config(
                    "review fixes require a verified delivered run".into(),
                ));
            }
            self.reserve_workspace(&run)?;
            let batch =
                match crate::workflow::review_fixes::prepare(self, id, remote, threads).await {
                    Ok(batch) => batch,
                    Err(error) => {
                        self.release_workspace(&run).await?;
                        return Err(error);
                    }
                };
            Box::pin(crate::workflow::review_fixes::start(self, id, &batch)).await?;
            batch
        };
        if batch.status != "completed" {
            if self.status(id).await?.workflow.as_ref().unwrap().delivery["cancel_requested"]
                == true
            {
                Box::pin(self.cancel(&batch.run_id)).await?;
            } else {
                Box::pin(self.resume(&batch.run_id)).await?;
            }
        }
        let _ownership = self.workspace_lock().await?;
        let parent = self.status(id).await?;
        self.reserve_workspace(&parent)?;
        let result = Box::pin(crate::workflow::review_fixes::publish(
            self, id, remote, batch,
        ))
        .await;
        if let Err(error) = &result {
            self.change(id, |run| {
                let uncertain = run
                    .workflow
                    .as_ref()
                    .unwrap()
                    .remote_operations
                    .values()
                    .any(|op| op.dispatches > 0 && op.result.is_none());
                let state = run.workflow.as_mut().unwrap();
                state.delivery["status"] =
                    json!(if uncertain || matches!(error, Error::Worker(_)) {
                        "recovery_needed"
                    } else {
                        "blocked"
                    });
                state.delivery["error"] = json!(error.to_string());
                Ok(())
            })
            .await?;
        }
        let run = self.status(id).await?;
        self.materialize(&run)?;
        if !["recovery_needed", "fixes_incomplete"]
            .iter()
            .any(|s| run.workflow.as_ref().unwrap().delivery["status"] == *s)
        {
            self.release_workspace(&run).await?;
        }
        result.or(Ok(run))
    }

    pub async fn cancel_delivery(&self, id: &str) -> Result<Run> {
        self.change(id, |run| {
            let state = run
                .workflow
                .as_mut()
                .ok_or_else(|| Error::Conflict("not an implementation run".into()))?;
            state.delivery["cancel_requested"] = json!(true);
            state.delivery["status"] = json!("stopping");
            Ok(())
        })
        .await
    }

    pub(crate) async fn change<F>(&self, id: &str, change: F) -> Result<Run>
    where
        F: Fn(&mut Run) -> Result<()>,
    {
        for _ in 0..32 {
            let old = self.status(id).await?;
            let mut run = old.clone();
            change(&mut run)?;
            if run == old {
                return Ok(old);
            }
            run.revision += 1;
            if self.services.store.save(old.revision, &run).await? {
                return Ok(run);
            }
        }
        Err(Error::Conflict(
            "concurrent state changes; retry the command".into(),
        ))
    }

    pub(crate) async fn ensure_scheduled(&self, run: &Run) -> Result<Run> {
        if run.status.is_terminal() {
            return Ok(run.clone());
        }
        match self
            .services
            .sessions
            .get(GetRequest {
                app_name: "factory-slice".into(),
                user_id: "local".into(),
                session_id: run.input.session_id.clone(),
                num_recent_events: None,
                after: None,
            })
            .await
        {
            Ok(_) => (),
            Err(error) if error.code == "session.not_found" => {
                if let Err(error) = self
                    .services
                    .sessions
                    .create(CreateRequest {
                        app_name: "factory-slice".into(),
                        user_id: "local".into(),
                        session_id: Some(run.input.session_id.clone()),
                        state: [("workspace_id".into(), json!(run.workspace_id))].into(),
                    })
                    .await
                    && error.code != "session.already_exists"
                {
                    return Err(error.into());
                }
            }
            Err(error) => return Err(error.into()),
        }
        let queued = self
            .services
            .queue
            .enqueue(
                NewRun::new(
                    &run.run_id,
                    "factory-slice",
                    "local",
                    &run.graph_version,
                    json!({"factory_run_id": run.run_id}),
                )
                .with_session_id(&run.input.session_id)
                .with_idempotency_key(&run.input.request_id)
                .with_max_attempts(16),
            )
            .await?;
        let scheduler_id = queued
            .record()
            .ok_or_else(|| Error::Conflict("queue rejected run".into()))?
            .run_id
            .clone();
        self.change(&run.run_id, |r| {
            r.scheduler_id = Some(scheduler_id.clone());
            Ok(())
        })
        .await
    }

    async fn workspace_lock(&self) -> Result<File> {
        if self
            .config
            .workspace
            .join(".factory-rust.lock")
            .is_symlink()
        {
            return Err(Error::Config(
                "workspace lock cannot be a symbolic link".into(),
            ));
        }
        let lock = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(self.config.workspace.join(".factory-rust.lock"))?;
        // A concurrent fork can briefly inherit the descriptor until exec closes
        // it. Yield for that window; sustained ownership still returns Busy.
        let bound = tokio::time::Instant::now() + Duration::from_millis(100);
        loop {
            match lock.try_lock() {
                Ok(()) => return Ok(lock),
                Err(std::fs::TryLockError::WouldBlock) if tokio::time::Instant::now() < bound => {
                    tokio::time::sleep(Duration::from_millis(2)).await
                }
                Err(std::fs::TryLockError::WouldBlock) => return Err(Error::Busy),
                Err(std::fs::TryLockError::Error(error)) => return Err(error.into()),
            }
        }
    }

    async fn execution_lock(&self, run: &Run) -> Result<File> {
        if run.input.planning.is_none() {
            return self.workspace_lock().await;
        }
        let directory = self.config.data_dir.join("planning-locks");
        std::fs::create_dir_all(&directory)?;
        let lock = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(directory.join(&run.run_id))?;
        lock.try_lock().map_err(|_| Error::Busy)?;
        Ok(lock)
    }

    fn reserve_workspace(&self, run: &Run) -> Result<()> {
        let path = self.config.workspace.join(".factory-rust.claim");
        if run.input.planning.is_some() {
            return Ok(());
        }
        let owner = run
            .workflow
            .as_ref()
            .and_then(|s| s.parent_run_id.as_deref())
            .unwrap_or(&run.run_id);
        if path.exists() || path.is_symlink() {
            let saved: serde_json::Value =
                serde_json::from_slice(&crate::workflow::artifacts::regular(&path)?)?;
            if saved != json!({"version":1,"run_id":owner,"data_dir":self.config.data_dir}) {
                return Err(Error::Busy);
            }
        } else if run.workflow.is_some() {
            crate::workflow::artifacts::atomic(
                &path,
                &serde_json::to_vec(
                    &json!({"version":1,"run_id":owner,"data_dir":self.config.data_dir}),
                )?,
            )?;
        }
        Ok(())
    }

    async fn release_workspace(&self, run: &Run) -> Result<()> {
        if run.input.planning.is_some() {
            return Ok(());
        }
        if let Some(workflow) = &run.workflow {
            self.reserve_workspace(run)?;
            if workflow.parent_run_id.is_some() {
                return Ok(());
            }
            if workflow
                .remote_operations
                .values()
                .any(|op| op.dispatches > 0 && op.result.is_none())
            {
                return Ok(());
            }
            if let Some(child_id) = workflow.delivery["review_fix"]["run_id"].as_str() {
                match self.status(child_id).await {
                    Ok(child) if child.status.is_terminal() => {
                        crate::workflow::execution::stop_all(self, child_id).await?;
                    }
                    _ => return Ok(()),
                }
            }
            std::fs::remove_file(self.config.workspace.join(".factory-rust.claim"))?;
            File::open(&self.config.workspace)?.sync_all()?;
        }
        Ok(())
    }

    /// One independently recoverable pass. Waiting returns immediately and owns no client connection.
    pub async fn resume(&self, id: &str) -> Result<Run> {
        let run = self.status(id).await?;
        if run.status.is_terminal() {
            Box::pin(self.resume_terminal(id, run)).await
        } else {
            Box::pin(self.resume_active(id, run)).await
        }
    }
    async fn resume_terminal(&self, id: &str, run: Run) -> Result<Run> {
        if run.delivery_refreshable() {
            let batch = run.workflow.as_ref().unwrap().delivery.get("review_fix");
            let result = if let Some(batch) =
                batch.filter(|b| b["status"] != "completed" && b["status"] != "cancelled")
            {
                let threads: Vec<String> = batch["threads"]
                    .as_array()
                    .ok_or_else(|| Error::Integrity("saved review fix threads are invalid".into()))?
                    .iter()
                    .filter_map(|t| t["id"].as_str().map(String::from))
                    .collect();
                Box::pin(self.fix_reviews_with_remote(id, self.delivery_remote.as_ref(), &threads))
                    .await
            } else {
                Box::pin(self.deliver(id, None)).await
            };
            return match result {
                Err(Error::Busy) => Ok(run),
                result => result,
            };
        }

        let artifact = self
            .config
            .data_dir
            .join("artifacts")
            .join(format!("{}.json", run.run_id));
        let claim = self.config.workspace.join(".factory-rust.claim");
        let owns_claim = run.workflow.is_some()
            && claim.exists()
            && serde_json::from_slice::<serde_json::Value>(&crate::workflow::artifacts::regular(
                &claim,
            )?)? == json!({"version":1,"run_id":run.run_id,"data_dir":self.config.data_dir});
        if !artifact.exists() || owns_claim {
            let _ownership = match self.execution_lock(&run).await {
                Ok(lock) => lock,
                Err(Error::Busy) => return Ok(run),
                Err(error) => return Err(error),
            };
            if !artifact.exists() {
                self.materialize(&run)?;
            }
            if owns_claim
                && !["recovery_needed", "fixing_reviews", "fixes_incomplete"]
                    .iter()
                    .any(|s| run.workflow.as_ref().unwrap().delivery["status"] == *s)
            {
                crate::workflow::execution::stop_all(self, id).await?;
                self.release_workspace(&run).await?;
            }
        }
        Ok(run)
    }
    async fn resume_active(&self, id: &str, run: Run) -> Result<Run> {
        let _ownership = match self.execution_lock(&run).await {
            Ok(lock) => lock,
            Err(Error::Busy) => return self.status(id).await,
            Err(e) => return Err(e),
        };
        self.reserve_workspace(&run)?;
        let run = self.ensure_scheduled(&self.status(id).await?).await?;
        if let Some(checkpoint) = self
            .services
            .checkpoints
            .load(&run.checkpoint_thread_id)
            .await?
            && (checkpoint.state.get("schema_version") != Some(&json!(SCHEMA_VERSION))
                || checkpoint.state.get("graph_version") != Some(&json!(run.graph_version)))
        {
            return Err(Error::Incompatible("checkpoint state/graph version is unsupported; restore the previous binary or migrate explicitly".into()));
        }
        let scheduler_id = run.scheduler_id.as_deref().unwrap();
        if run.cancel_requested {
            self.services.queue.request_cancel(scheduler_id).await?;
            if let Err(error) = crate::workflow::execution::stop_all(self, id).await {
                crate::workflow::execution::recovery(self, id, &error.to_string()).await?;
                return self.status(id).await;
            }
            return self.finish(id, Status::Cancelled, None, None).await;
        }
        if run.status == Status::WaitingForInput
            && run.waiting_for_answers()
            && Utc::now() < run.expires_at()
        {
            return Ok(run);
        }
        // A saved answer or expired deadline must repair a lost wake after a crash.
        self.services.queue.wake(scheduler_id).await?;
        let queued = self
            .services
            .queue
            .get(scheduler_id)
            .await?
            .ok_or_else(|| Error::NotFound(scheduler_id.into()))?;
        if queued.state == RunState::Completed {
            let value = queued.result.unwrap_or_default();
            return self.finish_completed(id, value).await;
        }
        if queued.state == RunState::Cancelled {
            return self.finish(id, Status::Cancelled, None, None).await;
        }
        let Some(claim) = self
            .services
            .queue
            .claim(&run.run_id, "factory-supervisor", LEASE)
            .await?
        else {
            let queued = self
                .services
                .queue
                .get(scheduler_id)
                .await?
                .ok_or_else(|| Error::NotFound(scheduler_id.into()))?;
            if queued.state == RunState::Failed {
                return self.finish(id, Status::Failed, None, queued.error).await;
            }
            return self.status(id).await;
        };
        self.change(id, |run| {
            run.status = Status::Running;
            run.attempts = claim.record.attempt;
            run.error = None;
            Ok(())
        })
        .await?;
        let cancel = CancellationToken::new();
        let mut context = WorkflowRunContext::detached(id, cancel.clone());
        context.thread_id = run.checkpoint_thread_id.clone();
        context.attempt = claim.record.attempt;
        context.resumed = claim.record.checkpoint.is_some();
        let adapter = crate::graph::FactoryWorkflow(self.clone());
        let invocation = adapter.execute_durable(
            &run.graph_version,
            [("run_id".into(), json!(id))].into(),
            context,
        );
        tokio::pin!(invocation);
        let mut heartbeat = tokio::time::interval(Duration::from_millis(250));
        let outcome = loop {
            tokio::select! {
                result = &mut invocation => break result,
                _ = heartbeat.tick() => {
                    match self.services.queue.heartbeat(scheduler_id, claim.epoch, LEASE).await {
                        Ok(cancelled) => if cancelled { cancel.cancel(); },
                        Err(error) => { cancel.cancel(); return Err(error.into()); }
                    }
                    if self.status(id).await?.cancel_requested { cancel.cancel(); }
                }
            }
        };
        if let Some(checkpoint) = self
            .services
            .checkpoints
            .load(&run.checkpoint_thread_id)
            .await?
        {
            self.services
                .queue
                .set_checkpoint(
                    scheduler_id,
                    claim.epoch,
                    RunCheckpointRef::Graph {
                        thread_id: run.checkpoint_thread_id.clone(),
                        checkpoint_id: Some(checkpoint.checkpoint_id),
                    },
                )
                .await?;
        }
        let current = self.status(id).await?;
        if current.workflow.is_some()
            && (current.cancel_requested || !outcome.as_ref().is_ok_and(|v| v["waiting"] == true))
            && let Err(error) = crate::workflow::execution::stop_all(self, id).await
        {
            self.services
                .queue
                .wait(scheduler_id, claim.epoch, Some(current.deadline_at))
                .await?;
            crate::workflow::execution::recovery(self, id, &error.to_string()).await?;
            return self.status(id).await;
        }
        if current.cancel_requested {
            self.services
                .queue
                .finish(scheduler_id, claim.epoch, RunOutcome::Cancelled)
                .await?;
            return self.finish(id, Status::Cancelled, None, None).await;
        }
        match outcome {
            Ok(value) if value["waiting"] == true => {
                self.services
                    .queue
                    .wait(scheduler_id, claim.epoch, Some(current.deadline_at))
                    .await?;
                self.status(id).await
            }
            Ok(value) => {
                self.services
                    .queue
                    .finish(
                        scheduler_id,
                        claim.epoch,
                        RunOutcome::Completed(value.clone()),
                    )
                    .await?;
                self.services
                    .faults
                    .hit(crate::CrashPoint::AfterQueueFinish);
                self.finish_completed(id, value).await
            }
            Err(error) => {
                let expired = Utc::now() >= current.deadline_at
                    || error.contains("execution deadline expired");
                let exhausted = current
                    .operation
                    .as_ref()
                    .is_some_and(|o| o.dispatch_count >= current.input.max_dispatches);
                let queued = self
                    .services
                    .queue
                    .finish(
                        scheduler_id,
                        claim.epoch,
                        RunOutcome::Failed {
                            error: error.clone(),
                            retryable: !expired && !exhausted,
                        },
                    )
                    .await?;
                if queued.state == RunState::Failed {
                    self.finish(id, Status::Failed, None, Some(error)).await
                } else {
                    self.change(id, |r| {
                        r.status = Status::RecoveryNeeded;
                        r.error = Some(error.clone());
                        Ok(())
                    })
                    .await
                }
            }
        }
    }

    async fn finish_completed(&self, id: &str, value: serde_json::Value) -> Result<Run> {
        let status = value
            .get("workflow_status")
            .filter(|v| !v.is_null())
            .map(|v| serde_json::from_value::<Status>(v.clone()))
            .transpose()?;
        let error = if status.is_some_and(|s| matches!(s, Status::Blocked | Status::Failed)) {
            self.status(id).await?.error
        } else {
            None
        };
        self.finish(
            id,
            if let Some(status) = status {
                status
            } else if value["declined"] == true {
                Status::Cancelled
            } else {
                Status::ReadyForReview
            },
            value["result"].as_str().map(str::to_owned),
            error,
        )
        .await
    }

    async fn finish(
        &self,
        id: &str,
        status: Status,
        result: Option<String>,
        error: Option<String>,
    ) -> Result<Run> {
        if self.status(id).await?.workflow.is_some()
            && let Err(error) = crate::workflow::execution::stop_all(self, id).await
        {
            crate::workflow::execution::recovery(self, id, &error.to_string()).await?;
            return self.status(id).await;
        }
        let run = self
            .change(id, |r| {
                if !r.status.is_terminal() {
                    // Decide against the latest state inside the CAS mutation.
                    // A cancellation saved while the queue finishes must win.
                    let status = if r.cancel_requested {
                        Status::Cancelled
                    } else {
                        status
                    };
                    r.status = status;
                    r.result = if status == Status::Cancelled {
                        None
                    } else {
                        result.clone()
                    };
                    r.error = if status == Status::Cancelled {
                        None
                    } else {
                        error.clone()
                    };
                    r.event(
                        "finish",
                        if matches!(status, Status::ReadyForReview | Status::Planned) {
                            "completed"
                        } else if status == Status::Cancelled {
                            "cancelled"
                        } else {
                            "failed"
                        },
                        "Run finished",
                    );
                }
                Ok(())
            })
            .await?;
        self.materialize(&run)?;
        let run = if run.delivery_pending() {
            Box::pin(crate::workflow::delivery::deliver(
                self,
                id,
                self.delivery_remote.as_ref(),
                None,
            ))
            .await?
        } else {
            run
        };
        self.materialize(&run)?;
        if !run
            .workflow
            .as_ref()
            .is_some_and(|s| s.delivery["status"] == "recovery_needed")
        {
            self.release_workspace(&run).await?;
        }
        Ok(run)
    }

    fn materialize(&self, run: &Run) -> Result<()> {
        let directory = self.config.data_dir.join("artifacts");
        std::fs::create_dir_all(&directory)?;
        let temporary = directory.join(format!("{}.tmp", run.run_id));
        let destination = directory.join(format!("{}.json", run.run_id));
        let file = File::create(&temporary)?;
        serde_json::to_writer_pretty(&file, &run)?;
        file.sync_all()?;
        std::fs::rename(temporary, destination)?;
        File::open(directory)?.sync_all()?;
        Ok(())
    }
}

pub(crate) fn digest(input: &[u8]) -> String {
    Sha256::digest(input)
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}
