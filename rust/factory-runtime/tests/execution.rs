use async_trait::async_trait;
use factory_runtime::{
    Config, Error, NoFaults, Reconciliation, Result, Runtime, StartRequest, Status, Worker,
};
use std::sync::{
    Arc,
    atomic::{AtomicU32, Ordering},
};
use tokio::sync::Notify;
use tokio_util::sync::CancellationToken;

struct BlockingWorker {
    entered: Notify,
    dispatches: AtomicU32,
}
#[async_trait]
impl Worker for BlockingWorker {
    async fn reconcile(&self, _: &str, _: &str) -> Result<Reconciliation> {
        Ok(Reconciliation::Absent)
    }
    async fn dispatch(&self, _: &str, _: &str, cancel: CancellationToken) -> Result<String> {
        self.dispatches.fetch_add(1, Ordering::SeqCst);
        self.entered.notify_one();
        cancel.cancelled().await;
        Err(Error::Cancelled)
    }
}

async fn approve(runtime: &Runtime, request: StartRequest) -> String {
    let run = runtime.start(request).await.unwrap();
    let waiting = runtime.resume(&run.run_id).await.unwrap();
    runtime
        .answer(
            &run.run_id,
            &waiting.interaction.unwrap().interaction_id,
            true,
        )
        .await
        .unwrap();
    run.run_id
}

#[tokio::test]
async fn concurrent_resumes_have_one_writer_and_saved_cancellation_stops_it() {
    let data = tempfile::tempdir().unwrap();
    let workspace = tempfile::tempdir().unwrap();
    let config = Config::new(data.path(), workspace.path()).unwrap();
    let worker = Arc::new(BlockingWorker {
        entered: Notify::new(),
        dispatches: AtomicU32::new(0),
    });
    let runtime = Runtime::open_with_worker(config.clone(), worker.clone(), Arc::new(NoFaults))
        .await
        .unwrap();
    let id = approve(
        &runtime,
        StartRequest::new("concurrent", "session", "Greeting"),
    )
    .await;
    let task_runtime = runtime.clone();
    let task_id = id.clone();
    let active = tokio::spawn(async move { task_runtime.resume(&task_id).await.unwrap() });
    tokio::time::timeout(std::time::Duration::from_secs(5), worker.entered.notified())
        .await
        .unwrap();
    let observer = Runtime::open(config.clone()).await.unwrap();
    assert_eq!(observer.resume(&id).await.unwrap().status, Status::Running);
    assert_eq!(observer.cancel(&id).await.unwrap().status, Status::Stopping);
    let cancelled = tokio::time::timeout(std::time::Duration::from_secs(5), active)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(cancelled.status, Status::Cancelled);
    assert_eq!(worker.dispatches.load(Ordering::SeqCst), 1);
    drop(runtime);
    drop(observer);
    assert_eq!(
        Runtime::open(config)
            .await
            .unwrap()
            .resume(&id)
            .await
            .unwrap(),
        cancelled
    );
}

struct FailingWorker;
#[async_trait]
impl Worker for FailingWorker {
    async fn reconcile(&self, _: &str, _: &str) -> Result<Reconciliation> {
        Ok(Reconciliation::Absent)
    }
    async fn dispatch(&self, _: &str, _: &str, _: CancellationToken) -> Result<String> {
        Err(Error::Worker("temporarily unavailable".into()))
    }
}

#[tokio::test]
async fn retry_budget_survives_restart_and_exhaustion_is_terminal() {
    let data = tempfile::tempdir().unwrap();
    let workspace = tempfile::tempdir().unwrap();
    let config = Config::new(data.path(), workspace.path()).unwrap();
    let runtime =
        Runtime::open_with_worker(config.clone(), Arc::new(FailingWorker), Arc::new(NoFaults))
            .await
            .unwrap();
    let mut input = StartRequest::new("retry", "session", "Greeting");
    input.max_dispatches = 2;
    let id = approve(&runtime, input).await;
    let failed = runtime.resume(&id).await.unwrap();
    assert_eq!(failed.status, Status::RecoveryNeeded);
    assert_eq!(failed.operation.unwrap().dispatch_count, 1);
    drop(runtime);
    let runtime = Runtime::open_with_worker(config, Arc::new(FailingWorker), Arc::new(NoFaults))
        .await
        .unwrap();
    let failed = runtime.resume(&id).await.unwrap();
    assert_eq!(failed.status, Status::Failed);
    assert_eq!(failed.operation.as_ref().unwrap().dispatch_count, 2);
    assert_eq!(runtime.resume(&id).await.unwrap(), failed);
}

struct UncertainWorker;
#[async_trait]
impl Worker for UncertainWorker {
    async fn reconcile(&self, _: &str, _: &str) -> Result<Reconciliation> {
        Ok(Reconciliation::Unknown("receipt unavailable".into()))
    }
    async fn dispatch(&self, _: &str, _: &str, _: CancellationToken) -> Result<String> {
        panic!("uncertain operations must not be dispatched")
    }
}

#[tokio::test]
async fn uncertain_dispatch_is_preserved_for_recovery_without_redispatch() {
    let data = tempfile::tempdir().unwrap();
    let workspace = tempfile::tempdir().unwrap();
    let config = Config::new(data.path(), workspace.path()).unwrap();
    let runtime = Runtime::open_with_worker(
        config.clone(),
        Arc::new(UncertainWorker),
        Arc::new(NoFaults),
    )
    .await
    .unwrap();
    let id = approve(
        &runtime,
        StartRequest::new("uncertain", "session", "Greeting"),
    )
    .await;
    assert_eq!(
        runtime.resume(&id).await.unwrap().status,
        Status::RecoveryNeeded
    );
    drop(runtime);
    let runtime = Runtime::open_with_worker(config, Arc::new(UncertainWorker), Arc::new(NoFaults))
        .await
        .unwrap();
    let run = runtime.resume(&id).await.unwrap();
    assert_eq!(run.status, Status::RecoveryNeeded);
    assert_eq!(run.operation.unwrap().dispatch_count, 0);
}

#[tokio::test]
async fn an_expired_saved_deadline_cannot_restart_its_budget() {
    let data = tempfile::tempdir().unwrap();
    let workspace = tempfile::tempdir().unwrap();
    let config = Config::new(data.path(), workspace.path()).unwrap();
    let runtime = Runtime::open(config.clone()).await.unwrap();
    let mut input = StartRequest::new("deadline", "session", "Greeting");
    input.timeout_seconds = 1;
    let run = runtime.start(input).await.unwrap();
    runtime.resume(&run.run_id).await.unwrap();
    drop(runtime);
    while chrono::Utc::now() < run.deadline_at {
        tokio::time::sleep(std::time::Duration::from_millis(100)).await;
    }
    let runtime = Runtime::open(config).await.unwrap();
    let failed = runtime.resume(&run.run_id).await.unwrap();
    assert_eq!(failed.status, Status::Failed);
    assert_eq!(failed.deadline_at, run.deadline_at);
    assert!(failed.operation.is_none());
}

struct DeadlineWorker {
    token: tokio::sync::Mutex<Option<CancellationToken>>,
}
#[async_trait]
impl Worker for DeadlineWorker {
    async fn reconcile(&self, _: &str, _: &str) -> Result<Reconciliation> {
        Ok(Reconciliation::Absent)
    }
    async fn dispatch(&self, _: &str, _: &str, cancel: CancellationToken) -> Result<String> {
        *self.token.lock().await = Some(cancel.clone());
        cancel.cancelled().await;
        Err(Error::Cancelled)
    }
}

#[tokio::test]
async fn execution_deadline_signals_the_worker_cancellation_boundary() {
    let data = tempfile::tempdir().unwrap();
    let workspace = tempfile::tempdir().unwrap();
    let worker = Arc::new(DeadlineWorker {
        token: tokio::sync::Mutex::new(None),
    });
    let runtime = Runtime::open_with_worker(
        Config::new(data.path(), workspace.path()).unwrap(),
        worker.clone(),
        Arc::new(NoFaults),
    )
    .await
    .unwrap();
    let mut input = StartRequest::new("worker-deadline", "session", "Greeting");
    input.timeout_seconds = 1;
    let id = approve(&runtime, input).await;
    let failed = runtime.resume(&id).await.unwrap();
    assert_eq!(failed.status, Status::Failed);
    assert!(worker.token.lock().await.as_ref().unwrap().is_cancelled());
}

struct CancelAtFinalization {
    config: Config,
    run_id: std::sync::Mutex<Option<String>>,
}
impl factory_runtime::Faults for CancelAtFinalization {
    fn hit(&self, point: factory_runtime::CrashPoint) {
        if point != factory_runtime::CrashPoint::AfterQueueFinish {
            return;
        }
        let config = self.config.clone();
        let id = self.run_id.lock().unwrap().clone().unwrap();
        std::thread::spawn(move || {
            tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()
                .unwrap()
                .block_on(async {
                    let observer = Runtime::open(config).await.unwrap();
                    assert_eq!(observer.cancel(&id).await.unwrap().status, Status::Stopping);
                });
        })
        .join()
        .unwrap();
    }
}

#[tokio::test]
async fn cancellation_saved_during_finalization_wins_over_completion() {
    let data = tempfile::tempdir().unwrap();
    let workspace = tempfile::tempdir().unwrap();
    let config = Config::new(data.path(), workspace.path()).unwrap();
    let faults = Arc::new(CancelAtFinalization {
        config: config.clone(),
        run_id: std::sync::Mutex::new(None),
    });
    let worker = Arc::new(
        factory_runtime::FakeWorker::open(&data.path().join("worker-records"))
            .await
            .unwrap(),
    );
    let runtime = Runtime::open_with_worker(config, worker, faults.clone())
        .await
        .unwrap();
    let id = approve(
        &runtime,
        StartRequest::new("cancel-finalize", "session", "Greeting"),
    )
    .await;
    *faults.run_id.lock().unwrap() = Some(id.clone());
    let cancelled = runtime.resume(&id).await.unwrap();
    assert_eq!(cancelled.status, Status::Cancelled);
    assert!(cancelled.cancel_requested);
    assert_eq!(runtime.resume(&id).await.unwrap(), cancelled);
}

#[tokio::test]
async fn service_expires_unattended_recovery_needed_runs() {
    let data = tempfile::tempdir().unwrap();
    let workspace = tempfile::tempdir().unwrap();
    let config = Config::new(data.path(), workspace.path()).unwrap();
    let runtime = Runtime::open_with_worker(
        config.clone(),
        Arc::new(UncertainWorker),
        Arc::new(NoFaults),
    )
    .await
    .unwrap();
    let mut input = StartRequest::new("recovery-deadline", "session", "Greeting");
    input.timeout_seconds = 1;
    let id = approve(&runtime, input).await;
    assert_eq!(
        runtime.resume(&id).await.unwrap().status,
        Status::RecoveryNeeded
    );
    drop(runtime);
    let mut service = std::process::Command::new(env!("CARGO_BIN_EXE_factory-runtime"))
        .arg(data.path())
        .arg(workspace.path())
        .arg("serve")
        .stdout(std::process::Stdio::null())
        .spawn()
        .unwrap();
    let observer = Runtime::open(config).await.unwrap();
    let result = tokio::time::timeout(std::time::Duration::from_secs(4), async {
        loop {
            let run = observer.status(&id).await.unwrap();
            if run.status.is_terminal() {
                break run;
            }
            tokio::time::sleep(std::time::Duration::from_millis(100)).await;
        }
    })
    .await;
    service.kill().unwrap();
    service.wait().unwrap();
    assert_eq!(
        result
            .expect("service must expire recovery-needed runs")
            .status,
        Status::Failed
    );
}
