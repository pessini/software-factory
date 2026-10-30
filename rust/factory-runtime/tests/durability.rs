use factory_runtime::{Config, Runtime, StartRequest, Status};

#[tokio::test]
async fn waiting_question_survives_restart_and_finishes_once() {
    let root = tempfile::tempdir().unwrap();
    let workspace = tempfile::tempdir().unwrap();
    let config = Config::new(root.path(), workspace.path()).unwrap();
    let runtime = Runtime::open(config.clone()).await.unwrap();
    let request = StartRequest::new("start-1", "session-1", "Build a greeting");
    let run = runtime.start(request.clone()).await.unwrap();
    let waiting = runtime.resume(&run.run_id).await.unwrap();
    assert_eq!(waiting.status, Status::WaitingForInput);
    assert_eq!(
        waiting.interaction.as_ref().unwrap().question,
        "Run the fake worker?"
    );
    drop(runtime);

    let runtime = Runtime::open(config).await.unwrap();
    assert_eq!(runtime.status(&run.run_id).await.unwrap(), waiting);
    assert_eq!(runtime.start(request).await.unwrap(), waiting);
    let question = waiting.interaction.unwrap();
    runtime
        .answer(&run.run_id, &question.interaction_id, true)
        .await
        .unwrap();
    let finished = runtime.resume(&run.run_id).await.unwrap();
    assert_eq!(finished.status, Status::ReadyForReview);
    assert_eq!(
        finished.result.as_deref(),
        Some("fake worker completed: Build a greeting")
    );
    assert_eq!(runtime.resume(&run.run_id).await.unwrap(), finished);
}

#[tokio::test]
async fn reused_request_and_answer_identities_reject_conflicting_input() {
    let data = tempfile::tempdir().unwrap();
    let workspace = tempfile::tempdir().unwrap();
    let runtime = Runtime::open(Config::new(data.path(), workspace.path()).unwrap())
        .await
        .unwrap();
    let request = StartRequest::new("idempotency", "session", "Greeting");
    let run = runtime.start(request.clone()).await.unwrap();
    let (first, second) = tokio::join!(runtime.start(request.clone()), runtime.start(request));
    assert_eq!(first.unwrap(), second.unwrap());
    assert!(
        runtime
            .start(StartRequest::new("idempotency", "session", "Different"))
            .await
            .is_err()
    );
    let waiting = runtime.resume(&run.run_id).await.unwrap();
    let question = waiting.interaction.unwrap();
    assert!(runtime.answer(&run.run_id, "stale", true).await.is_err());
    let answered = runtime
        .answer(&run.run_id, &question.interaction_id, true)
        .await
        .unwrap();
    assert_eq!(
        runtime
            .answer(&run.run_id, &question.interaction_id, true)
            .await
            .unwrap(),
        answered
    );
    assert!(
        runtime
            .answer(&run.run_id, &question.interaction_id, false)
            .await
            .is_err()
    );
}

#[tokio::test]
async fn declined_approval_and_saved_cancellation_never_dispatch() {
    let data = tempfile::tempdir().unwrap();
    let workspace = tempfile::tempdir().unwrap();
    let config = Config::new(data.path(), workspace.path()).unwrap();
    let runtime = Runtime::open(config.clone()).await.unwrap();
    let run = runtime
        .start(StartRequest::new("decline", "session", "Greeting"))
        .await
        .unwrap();
    let waiting = runtime.resume(&run.run_id).await.unwrap();
    runtime
        .answer(
            &run.run_id,
            &waiting.interaction.unwrap().interaction_id,
            false,
        )
        .await
        .unwrap();
    let declined = runtime.resume(&run.run_id).await.unwrap();
    assert_eq!(declined.status, Status::Cancelled);
    assert!(declined.operation.is_none());
    let run = runtime
        .start(StartRequest::new("cancel", "session", "Greeting"))
        .await
        .unwrap();
    let cancelled = runtime.cancel(&run.run_id).await.unwrap();
    drop(runtime);
    let runtime = Runtime::open(config).await.unwrap();
    assert_eq!(runtime.resume(&run.run_id).await.unwrap(), cancelled);
    assert_eq!(cancelled.status, Status::Cancelled);
    assert!(cancelled.cancel_requested);
    assert!(cancelled.operation.is_none());
}

#[tokio::test]
async fn a_newer_graph_checkpoint_is_rejected_before_execution() {
    use sqlx::sqlite::SqlitePoolOptions;
    let data = tempfile::tempdir().unwrap();
    let workspace = tempfile::tempdir().unwrap();
    let config = Config::new(data.path(), workspace.path()).unwrap();
    let runtime = Runtime::open(config.clone()).await.unwrap();
    let run = runtime
        .start(StartRequest::new("future", "session", "Greeting"))
        .await
        .unwrap();
    runtime.resume(&run.run_id).await.unwrap();
    drop(runtime);
    // Upgrade fixture: simulate a volume written by a future graph binary.
    let pool = SqlitePoolOptions::new()
        .connect_with(
            sqlx::sqlite::SqliteConnectOptions::new()
                .filename(data.path().join("factory-rust.sqlite")),
        )
        .await
        .unwrap();
    sqlx::query("UPDATE graph_checkpoints SET state = json_set(state, '$.graph_version', 'factory-fake-v99')").execute(&pool).await.unwrap();
    pool.close().await;
    let runtime = Runtime::open(config).await.unwrap();
    assert!(matches!(
        runtime.resume(&run.run_id).await,
        Err(factory_runtime::Error::Incompatible(_))
    ));
}

#[tokio::test]
async fn missing_final_artifact_is_repaired_without_reexecuting_terminal_work() {
    let data = tempfile::tempdir().unwrap();
    let workspace = tempfile::tempdir().unwrap();
    let config = Config::new(data.path(), workspace.path()).unwrap();
    let runtime = Runtime::open(config.clone()).await.unwrap();
    let run = runtime
        .start(StartRequest::new("artifact", "session", "Greeting"))
        .await
        .unwrap();
    let waiting = runtime.resume(&run.run_id).await.unwrap();
    runtime
        .answer(
            &run.run_id,
            &waiting.interaction.unwrap().interaction_id,
            true,
        )
        .await
        .unwrap();
    let finished = runtime.resume(&run.run_id).await.unwrap();
    let artifact = data
        .path()
        .join("artifacts")
        .join(format!("{}.json", run.run_id));
    std::fs::remove_file(&artifact).unwrap();
    drop(runtime);
    let runtime = Runtime::open(config).await.unwrap();
    assert_eq!(runtime.resume(&run.run_id).await.unwrap(), finished);
    let saved: factory_runtime::Run =
        serde_json::from_slice(&std::fs::read(artifact).unwrap()).unwrap();
    assert_eq!(saved, finished);
}
