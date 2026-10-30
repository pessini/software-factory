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
    workspace_id: String,
}

impl Runtime {
    pub async fn open(config: Config) -> Result<Self> {
        let worker = Arc::new(FakeWorker::open(&config.data_dir.join("worker-records")).await?);
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
        Self {
            config,
            services: Arc::new(services),
            workspace_id,
        }
    }

    pub async fn start(&self, input: StartRequest) -> Result<Run> {
        input.validate()?;
        let run_id = format!("run-{}", digest(input.request_id.as_bytes()));
        let mut run = Run {
            schema_version: SCHEMA_VERSION,
            graph_version: GRAPH_VERSION.into(),
            checkpoint_thread_id: format!("factory:{run_id}"),
            run_id,
            workspace_id: self.workspace_id.clone(),
            scheduler_id: None,
            deadline_at: Utc::now() + chrono::Duration::seconds(input.timeout_seconds.into()),
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

    pub async fn cancel(&self, id: &str) -> Result<Run> {
        self.change(id, |run| {
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

    async fn ensure_scheduled(&self, run: &Run) -> Result<Run> {
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
                    GRAPH_VERSION,
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

    fn workspace_lock(&self) -> Result<File> {
        let lock = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(self.config.workspace.join(".factory-rust.lock"))?;
        lock.try_lock().map_err(|error| match error {
            std::fs::TryLockError::WouldBlock => Error::Busy,
            std::fs::TryLockError::Error(error) => Error::Io(error),
        })?;
        Ok(lock)
    }

    /// One independently recoverable pass. Waiting returns immediately and owns no client connection.
    pub async fn resume(&self, id: &str) -> Result<Run> {
        let run = self.status(id).await?;
        if run.status.is_terminal() {
            if !self
                .config
                .data_dir
                .join("artifacts")
                .join(format!("{}.json", run.run_id))
                .exists()
            {
                let _ownership = match self.workspace_lock() {
                    Ok(lock) => lock,
                    Err(Error::Busy) => return Ok(run),
                    Err(error) => return Err(error),
                };
                self.materialize(&run)?;
            }
            return Ok(run);
        }
        let _ownership = match self.workspace_lock() {
            Ok(lock) => lock,
            Err(Error::Busy) => return self.status(id).await,
            Err(e) => return Err(e),
        };
        let run = self.ensure_scheduled(&self.status(id).await?).await?;
        if let Some(checkpoint) = self
            .services
            .checkpoints
            .load(&run.checkpoint_thread_id)
            .await?
            && (checkpoint.state.get("schema_version") != Some(&json!(SCHEMA_VERSION))
                || checkpoint.state.get("graph_version") != Some(&json!(GRAPH_VERSION)))
        {
            return Err(Error::Incompatible("checkpoint state/graph version is unsupported; restore the previous binary or migrate explicitly".into()));
        }
        let scheduler_id = run.scheduler_id.as_deref().unwrap();
        if run.cancel_requested {
            self.services.queue.request_cancel(scheduler_id).await?;
            return self.finish(id, Status::Cancelled, None, None).await;
        }
        if run.status == Status::WaitingForInput
            && run
                .interaction
                .as_ref()
                .is_some_and(|q| q.approved.is_none())
            && Utc::now() < run.deadline_at
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
            GRAPH_VERSION,
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
                let expired = Utc::now() >= current.deadline_at;
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
        self.finish(
            id,
            if value["declined"] == true {
                Status::Cancelled
            } else {
                Status::ReadyForReview
            },
            value["result"].as_str().map(str::to_owned),
            None,
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
                        if status == Status::ReadyForReview {
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
