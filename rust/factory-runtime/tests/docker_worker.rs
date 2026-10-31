use factory_runtime::{
    Config, Reconciliation, Worker,
    workflow::{
        CheckResult, ExecutionKind, ExecutionRequest, QualityCommand,
        docker::{DockerConfig, DockerWorker},
    },
};
use std::{path::Path, process::Command};
use tokio_util::sync::CancellationToken;
fn git(root: &Path, args: &[&str]) {
    assert!(
        Command::new("git")
            .args(args)
            .current_dir(root)
            .output()
            .unwrap()
            .status
            .success()
    );
}
#[tokio::test]
#[ignore = "requires local Docker daemon and pinned software-factory-worker:0.154.0 image"]
async fn docker_quality_is_read_only_receipted_and_confirmed_stopped() {
    let data = tempfile::tempdir().unwrap();
    let workspace = tempfile::tempdir().unwrap();
    git(workspace.path(), &["init", "-q"]);
    git(workspace.path(), &["config", "user.name", "Test"]);
    git(
        workspace.path(),
        &["config", "user.email", "test@example.com"],
    );
    std::fs::write(workspace.path().join("source.txt"), "original\n").unwrap();
    git(workspace.path(), &["add", "source.txt"]);
    git(workspace.path(), &["commit", "-qm", "Initial"]);
    std::fs::create_dir_all(data.path().join("evidence/run-docker")).unwrap();
    let config = Config::new(data.path(), workspace.path()).unwrap();
    let worker = DockerWorker::open(config.clone(), DockerConfig::local(&config, None)).unwrap();
    let request=ExecutionRequest {version:1,operation_id:"run-docker-check-1".into(),run_id:"run-docker".into(),workspace:workspace.path().to_string_lossy().into_owned(),evidence_directory:data.path().join("evidence/run-docker").to_string_lossy().into_owned(),protected_paths:vec![],deadline_at:chrono::Utc::now()+chrono::Duration::seconds(60),execution:ExecutionKind::Quality {command:QualityCommand {name:"protection".into(),argv:vec!["python3".into(),"-B".into(),"-c".into(),"import os; assert not os.path.exists('/var/run/docker.sock'); assert not os.path.exists('/data/factory-rust.sqlite');\ntry:\n open('source.txt','w').write('tampered')\n raise AssertionError('write permitted')\nexcept OSError: pass\nprint('protected')".into()],timeout_seconds:60,repeat_on_recovery:false},content_version:"test-content".into()}};
    let encoded = serde_json::to_string(&request).unwrap();
    assert_eq!(
        worker
            .reconcile(&request.operation_id, &encoded)
            .await
            .unwrap(),
        Reconciliation::Absent
    );
    let output = worker
        .dispatch(&request.operation_id, &encoded, CancellationToken::new())
        .await
        .unwrap();
    let check: CheckResult = serde_json::from_str(&output).unwrap();
    assert!(check.passed(), "{:?}", check);
    assert!(check.output_tail.contains("protected"));
    let receipt = worker.receipt(&request.operation_id).unwrap().unwrap();
    assert!(receipt.stopped);
    assert_eq!(receipt.container_id.as_ref().unwrap().len(), 64);
    assert_eq!(
        worker
            .reconcile(&request.operation_id, &encoded)
            .await
            .unwrap(),
        Reconciliation::Completed(output)
    );
    assert_eq!(
        std::fs::read_to_string(workspace.path().join("source.txt")).unwrap(),
        "original\n"
    );
    assert!(
        Command::new("docker")
            .args(["rm", receipt.container_id.as_ref().unwrap()])
            .output()
            .unwrap()
            .status
            .success()
    );
}

#[tokio::test]
#[ignore = "real Codex provider call; requires Docker image and CODEX auth file"]
async fn native_codex_protocol_in_docker_returns_the_exact_saved_turn() {
    let data = tempfile::tempdir().unwrap();
    let workspace = tempfile::tempdir().unwrap();
    git(workspace.path(), &["init", "-q"]);
    git(workspace.path(), &["config", "user.name", "Test"]);
    git(
        workspace.path(),
        &["config", "user.email", "test@example.com"],
    );
    std::fs::write(workspace.path().join("source.txt"), "original\n").unwrap();
    git(workspace.path(), &["add", "source.txt"]);
    git(workspace.path(), &["commit", "-qm", "Initial"]);
    std::fs::create_dir_all(data.path().join("evidence/run-native")).unwrap();
    let config = Config::new(data.path(), workspace.path()).unwrap();
    let auth = std::env::var_os("FACTORY_TEST_AUTH_FILE")
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|| {
            std::env::var_os("CODEX_HOME")
                .map(std::path::PathBuf::from)
                .unwrap_or_else(|| {
                    std::path::PathBuf::from(std::env::var_os("HOME").unwrap()).join(".codex")
                })
                .join("auth.json")
        });
    let mut docker = DockerConfig::local(&config, Some(auth));
    docker.approval_policy = "on-request".into();
    let worker = DockerWorker::open(config.clone(), docker).unwrap();
    let run_id = format!(
        "run-native-{}",
        workspace
            .path()
            .file_name()
            .unwrap()
            .to_string_lossy()
            .replace('.', "-")
    );
    std::fs::create_dir_all(data.path().join("evidence").join(&run_id)).unwrap();
    let request=ExecutionRequest {version:1,operation_id:format!("{run_id}-plan-1"),run_id:run_id.clone(),workspace:workspace.path().to_string_lossy().into_owned(),evidence_directory:data.path().join("evidence").join(&run_id).to_string_lossy().into_owned(),protected_paths:vec![],deadline_at:chrono::Utc::now()+chrono::Duration::seconds(120),execution:ExecutionKind::Specialist {role:"planner".into(),step:"plan".into(),agent:factory_runtime::workflow::AgentConfig {provider:"codex".into(),model:"gpt-5.6-luna".into(),reasoning_level:"low".into(),permission_mode:"auto".into()},prompt:"Read source.txt with a tool, then return exactly this JSON: {\"read\":\"original\"}. Do not edit any files.".into()}};
    let encoded = serde_json::to_string(&request).unwrap();
    let result = worker
        .dispatch(&request.operation_id, &encoded, CancellationToken::new())
        .await;
    let receipt = worker.receipt(&request.operation_id).unwrap().unwrap();
    if receipt.stopped
        && let Some(id) = &receipt.container_id
    {
        let _ = Command::new("docker").args(["rm", id]).output();
    }
    let output = result.unwrap();
    assert_eq!(
        serde_json::from_str::<serde_json::Value>(&output).unwrap(),
        serde_json::json!({"read":"original"})
    );
    assert!(
        receipt.stopped && receipt.provider_thread_id.is_some() && receipt.execution_id.is_some()
    );
    assert_eq!(
        worker
            .reconcile(&request.operation_id, &encoded)
            .await
            .unwrap(),
        Reconciliation::Completed(output)
    );
}

#[tokio::test]
#[ignore = "real multi-step Codex workflow; requires Docker and existing file login"]
async fn pinned_real_provider_workflow_in_docker() {
    use factory_runtime::{
        Runtime, StartRequest, Status,
        workflow::{ImplementationInput, WorkflowConfig},
    };
    let checks = std::env::current_dir().unwrap().join("../../.checks");
    std::fs::create_dir_all(&checks).unwrap();
    let root = tempfile::Builder::new()
        .prefix("issue-17-live-")
        .tempdir_in(&checks)
        .unwrap()
        .keep()
        .canonicalize()
        .unwrap();
    let workspace = root.join("workspace");
    let data = root.join("data");
    std::fs::create_dir_all(&workspace).unwrap();
    git(&workspace, &["init", "-q"]);
    git(&workspace, &["config", "user.name", "Test"]);
    git(&workspace, &["config", "user.email", "test@example.com"]);
    std::fs::write(workspace.join("greeting.txt"), "Hello\n").unwrap();
    git(&workspace, &["add", "greeting.txt"]);
    git(&workspace, &["commit", "-qm", "Initial greeting"]);
    let config = Config::new(&data, &workspace).unwrap();
    let auth = std::env::var_os("FACTORY_TEST_AUTH_FILE")
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|| {
            std::env::var_os("CODEX_HOME")
                .map(std::path::PathBuf::from)
                .unwrap_or_else(|| {
                    std::path::PathBuf::from(std::env::var_os("HOME").unwrap()).join(".codex")
                })
                .join("auth.json")
        });
    let mut docker = DockerConfig::local(&config, Some(auth));
    docker.approval_policy = "on-request".into();
    let runtime = Runtime::open_docker(config, docker).await.unwrap();
    let settings=WorkflowConfig::from_yaml(r#"
version: 2
workflow: simple-sdlc
review_mode: single
worker_connection: {runtime: codex, model_connection: native, codex_version: '0.154.0'}
agents:
  planner: {provider: codex, model: gpt-5.6-luna, reasoning_level: low, permission_mode: auto}
  builder: {provider: codex, model: gpt-5.6-luna, reasoning_level: low, permission_mode: accept-edits}
  reviewer: {provider: codex, model: gpt-5.6-luna, reasoning_level: low, permission_mode: auto}
quality:
  commands:
    - {name: greeting, argv: [python3, -B, -c, "assert open('greeting.txt').read() == 'Hello, world!\\n'"], timeout_seconds: 30, repeat_on_recovery: true}
limits: {step_timeout_seconds: 120, total_run_timeout_seconds: 600}
"#).unwrap();
    let mut input = StartRequest::new(
        "live-workflow",
        "live-session",
        "Change greeting.txt to contain exactly Hello, world! followed by one newline. The only required deliverable is that file. No new dependencies or publication.",
    );
    input.implementation = Some(ImplementationInput {
        config: settings,
        required_criteria: vec![],
        approved_plan: None,
    });
    let run = runtime.start(input).await.unwrap();
    let result = runtime.resume(&run.run_id).await.unwrap();
    let record = serde_json::json!({"kind":"live","root":root,"run_id":result.run_id,"status":result.status,"error":result.error,"model":"gpt-5.6-luna","effort":"low","codex_version":"0.154.0","result":result});
    std::fs::write(
        checks.join("issue-17-live.json"),
        serde_json::to_vec_pretty(&record).unwrap(),
    )
    .unwrap();
    println!(
        "Live evidence: {}",
        checks.join("issue-17-live.json").display()
    );
    assert_eq!(result.status, Status::ReadyForReview, "{:?}", result.error);
}
