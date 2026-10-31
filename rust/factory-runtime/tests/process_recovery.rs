use factory_runtime::{Config, Run, Runtime, StartRequest, Status};
use std::{
    path::Path,
    process::{Command, Output},
    time::Duration,
};

fn command(data: &Path, workspace: &Path, arguments: &[&str], crash: Option<&str>) -> Output {
    let mut cmd = Command::new(env!("CARGO_BIN_EXE_factory-runtime"));
    cmd.arg(data).arg(workspace).args(arguments);
    if let Some(crash) = crash {
        cmd.env("FACTORY_SLICE_CRASH_AT", crash);
    }
    cmd.output().unwrap()
}
fn success(output: Output) -> Run {
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    serde_json::from_slice(&output.stdout).unwrap()
}

#[tokio::test]
async fn completed_queue_result_repairs_a_crash_before_domain_finalization() {
    let data = tempfile::tempdir().unwrap();
    let workspace = tempfile::tempdir().unwrap();
    let runtime = Runtime::open(Config::new(data.path(), workspace.path()).unwrap())
        .await
        .unwrap();
    let run = runtime
        .start(StartRequest::new("finish-gap", "session", "Greeting"))
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
    drop(runtime);
    let crashed = command(
        data.path(),
        workspace.path(),
        &["resume", &run.run_id],
        Some("after_queue_finish"),
    );
    assert_eq!(crashed.status.code(), Some(86));
    let finished = success(command(
        data.path(),
        workspace.path(),
        &["resume", &run.run_id],
        None,
    ));
    assert_eq!(finished.status, Status::ReadyForReview);
    assert_eq!(
        finished.result.as_deref(),
        Some("fake worker completed: Greeting")
    );
}

#[tokio::test]
async fn crash_dispatch_windows_reconcile_the_same_logical_operation() {
    for point in ["before_dispatch", "after_dispatch", "after_result"] {
        let data = tempfile::tempdir().unwrap();
        let workspace = tempfile::tempdir().unwrap();
        let runtime = Runtime::open(Config::new(data.path(), workspace.path()).unwrap())
            .await
            .unwrap();
        let run = runtime
            .start(StartRequest::new("dispatch-gap", "session", "Greeting"))
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
        drop(runtime);
        let crashed = command(
            data.path(),
            workspace.path(),
            &["resume", &run.run_id],
            Some(point),
        );
        assert_eq!(crashed.status.code(), Some(86), "crash point {point}");
        let recovery_deadline = std::time::Instant::now() + Duration::from_secs(10);
        let finished = loop {
            let recovered = success(command(
                data.path(),
                workspace.path(),
                &["resume", &run.run_id],
                None,
            ));
            if recovered.status == Status::ReadyForReview {
                break recovered;
            }
            assert!(
                std::time::Instant::now() < recovery_deadline,
                "recovery lease did not expire"
            );
            std::thread::sleep(Duration::from_millis(100));
        };
        assert_eq!(
            finished.status,
            Status::ReadyForReview,
            "{point}: {:?}",
            finished.error
        );
        let operation = finished.operation.as_ref().unwrap();
        assert_eq!(operation.operation_id, format!("{}-fake-1", run.run_id));
        assert_eq!(
            operation.dispatch_count,
            if point == "before_dispatch" { 2 } else { 1 }
        );
        assert_eq!(finished.attempts, 2);
        assert_eq!(
            success(command(
                data.path(),
                workspace.path(),
                &["resume", &run.run_id],
                None
            )),
            finished
        );
    }
}
