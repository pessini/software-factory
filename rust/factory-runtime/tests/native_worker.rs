use factory_runtime::{Config, workflow::native::NativeConfig};

#[tokio::test]
async fn native_configuration_refuses_an_unpinned_provider() {
    let data = tempfile::tempdir().unwrap();
    let workspace = tempfile::tempdir().unwrap();
    let config = Config::new(data.path(), workspace.path()).unwrap();
    let native: NativeConfig = serde_json::from_value(serde_json::json!({
        "executable": "/usr/bin/true",
        "bubblewrap": "/usr/bin/bwrap",
        "cgroup_root": "/sys/fs/cgroup/factory",
        "runtime_roots": ["/usr"],
        "auth_file": null,
        "approval_policy": "never"
    }))
    .unwrap();
    let error = native.pin(&config).await.unwrap_err();
    assert!(error.to_string().contains("0.154.0"), "{error}");
}

struct IdentifiedWorker(&'static str);
#[async_trait::async_trait]
impl factory_runtime::Worker for IdentifiedWorker {
    fn execution_identity(&self) -> Option<serde_json::Value> {
        Some(serde_json::json!({"backend": self.0, "provider": "codex", "policy": "restricted-v1"}))
    }
    async fn reconcile(
        &self,
        _: &str,
        _: &str,
    ) -> factory_runtime::Result<factory_runtime::Reconciliation> {
        Ok(factory_runtime::Reconciliation::Absent)
    }
    async fn dispatch(
        &self,
        _: &str,
        _: &str,
        _: tokio_util::sync::CancellationToken,
    ) -> factory_runtime::Result<String> {
        panic!("identity mismatch must be rejected before dispatch")
    }
}

#[tokio::test]
async fn resume_cannot_change_backend_or_substitute_a_fake_worker() {
    use factory_runtime::{NoFaults, Runtime, StartRequest};
    use std::sync::Arc;
    let data = tempfile::tempdir().unwrap();
    let workspace = tempfile::tempdir().unwrap();
    let config = Config::new(data.path(), workspace.path()).unwrap();
    let runtime = Runtime::open_with_worker(
        config.clone(),
        Arc::new(IdentifiedWorker("native")),
        Arc::new(NoFaults),
    )
    .await
    .unwrap();
    let run = runtime
        .start(StartRequest::new("identity", "session", "request"))
        .await
        .unwrap();
    assert_eq!(run.execution_backend.as_ref().unwrap()["backend"], "native");
    let changed = Runtime::open_with_worker(
        config.clone(),
        Arc::new(IdentifiedWorker("docker")),
        Arc::new(NoFaults),
    )
    .await
    .unwrap();
    assert!(
        changed
            .resume(&run.run_id)
            .await
            .unwrap_err()
            .to_string()
            .contains("backend")
    );
    let fake = Runtime::open(config).await.unwrap();
    assert!(
        fake.resume(&run.run_id)
            .await
            .unwrap_err()
            .to_string()
            .contains("backend")
    );
}

/// Run on a provisioned native Linux host. The commands are real OS permission
/// probes; this is not a launch-argument assertion or a simulated sandbox.
#[tokio::test]
#[ignore = "requires FACTORY_NATIVE_TEST_CONFIG, pinned Codex, user namespaces and delegated cgroup v2"]
async fn native_quality_is_read_only_network_denied_and_durably_receipted() {
    use factory_runtime::{
        Reconciliation, Worker,
        workflow::{
            CheckResult, ExecutionKind, ExecutionRequest, QualityCommand, broker::ExecutionWorker,
        },
    };
    let data = tempfile::tempdir().unwrap();
    let workspace = tempfile::tempdir().unwrap();
    let config = Config::new(data.path(), workspace.path()).unwrap();
    assert!(
        std::process::Command::new("git")
            .args(["init", "-q"])
            .arg(workspace.path())
            .status()
            .unwrap()
            .success()
    );
    std::fs::write(workspace.path().join("source"), "original").unwrap();
    commit_repository(workspace.path());

    std::fs::create_dir_all(data.path().join("evidence/run-native")).unwrap();
    let native = native_test_configuration();
    let worker =
        ExecutionWorker::open_native(config.clone(), native.pin(&config).await.unwrap()).unwrap();
    let request = ExecutionRequest {
        version: 1, operation_id: "run-native-check".into(), run_id: "run-native".into(),
        workspace: workspace.path().display().to_string(), evidence_directory: data.path().join("evidence/run-native").display().to_string(),
        protected_paths: vec![], deadline_at: chrono::Utc::now() + chrono::Duration::seconds(30),
        execution: ExecutionKind::Quality { command: QualityCommand {
            name: "enforcement".into(), argv: vec!["/usr/bin/python3".into(), "-B".into(), "-c".into(), r#"
import os, socket
for action in [lambda: open('source','w'), lambda: os.unlink('source'), lambda: os.rename('source','moved'), lambda: os.symlink('/tmp/outside','link')]:
    try: action()
    except OSError: pass
    else: raise AssertionError('workspace mutation allowed')
s = socket.socket(); s.settimeout(1)
try: s.connect(('1.1.1.1', 443))
except OSError: pass
else: raise AssertionError('network allowed')
assert not os.path.exists('/var/run/docker.sock')
print('permissions enforced')
"#.into()], timeout_seconds: 30, repeat_on_recovery: false }, content_version: "fixture".into() },
    };
    let encoded = serde_json::to_string(&request).unwrap();
    assert_eq!(
        worker
            .reconcile(&request.operation_id, &encoded)
            .await
            .unwrap(),
        Reconciliation::Absent
    );
    let result = worker
        .dispatch(
            &request.operation_id,
            &encoded,
            tokio_util::sync::CancellationToken::new(),
        )
        .await
        .unwrap();
    let check: CheckResult = serde_json::from_str(&result).unwrap();
    assert!(check.passed(), "{check:?}");
    assert!(check.output_tail.contains("permissions enforced"));
    let receipt = worker.receipt(&request.operation_id).unwrap().unwrap();
    assert!(receipt.stopped);
    assert_eq!(receipt.backend_identity.unwrap()["backend"], "native");
    assert!(receipt.effective_policy.is_some());
    assert_eq!(
        worker
            .reconcile(&request.operation_id, &encoded)
            .await
            .unwrap(),
        Reconciliation::Completed(result)
    );
}

#[tokio::test]
#[ignore = "real Codex call; requires FACTORY_NATIVE_TEST_CONFIG with auth and delegated cgroup v2"]
async fn native_codex_reads_workspace_and_records_the_exact_session() {
    use factory_runtime::{
        Worker,
        workflow::{AgentConfig, ExecutionKind, ExecutionRequest, broker::ExecutionWorker},
    };
    let data = tempfile::tempdir().unwrap();
    let workspace = tempfile::tempdir().unwrap();
    std::fs::write(workspace.path().join("source"), "original\n").unwrap();
    commit_repository(workspace.path());
    let config = Config::new(data.path(), workspace.path()).unwrap();
    std::fs::create_dir_all(data.path().join("evidence/run-native-live")).unwrap();
    let native = native_test_configuration();
    let worker =
        ExecutionWorker::open_native(config.clone(), native.pin(&config).await.unwrap()).unwrap();
    let request = ExecutionRequest { version:1, operation_id:"run-native-live-plan".into(), run_id:"run-native-live".into(), workspace:workspace.path().display().to_string(), evidence_directory:data.path().join("evidence/run-native-live").display().to_string(), protected_paths:vec![], deadline_at:chrono::Utc::now()+chrono::Duration::seconds(180), execution:ExecutionKind::Specialist {role:"planner".into(), step:"plan".into(), agent:AgentConfig { provider:"codex".into(), model:std::env::var("FACTORY_NATIVE_TEST_MODEL").unwrap_or_else(|_| "gpt-5.6-luna".into()), reasoning_level:"low".into(), permission_mode:"auto".into()}, prompt:"Read source with a tool. Then return exactly this JSON without markdown: {\"read\":\"original\"}. Do not edit files.".into()} };
    let encoded = serde_json::to_string(&request).unwrap();
    let output = worker
        .dispatch(
            &request.operation_id,
            &encoded,
            tokio_util::sync::CancellationToken::new(),
        )
        .await
        .unwrap();
    assert_eq!(
        serde_json::from_str::<serde_json::Value>(&output)
            .unwrap_or_else(|error| panic!("{error}; provider output: {output}")),
        serde_json::json!({"read":"original"})
    );
    let receipt = worker.receipt(&request.operation_id).unwrap().unwrap();
    assert!(
        receipt.stopped && receipt.provider_thread_id.is_some() && receipt.execution_id.is_some()
    );
    assert!(receipt.effective_policy.is_some());
}

#[tokio::test]
#[ignore = "real Codex tool enforcement; requires FACTORY_NATIVE_TEST_CONFIG and delegated cgroup v2"]
async fn native_builder_can_write_application_paths_but_cannot_escape() {
    use factory_runtime::{
        Worker,
        workflow::{AgentConfig, ExecutionKind, ExecutionRequest, broker::ExecutionWorker},
    };
    let data = tempfile::tempdir().unwrap();
    let workspace = tempfile::tempdir().unwrap();
    std::fs::create_dir_all(workspace.path().join("src/application")).unwrap();
    std::fs::create_dir_all(workspace.path().join("src/protected")).unwrap();
    std::fs::write(workspace.path().join("src/protected/policy"), "protected").unwrap();
    std::fs::write(
        workspace.path().join("AGENTS.md"),
        "Follow the requested task.\n",
    )
    .unwrap();
    std::fs::write(
        workspace.path().join("probe.py"),
        r#"
import errno, os, socket
from pathlib import Path

def denied(action):
    try: action()
    except OSError: return
    raise AssertionError('protected operation was permitted')

for path in ['AGENTS.md', '.git/config', 'src/protected/policy', 'unreviewed-root-file', 'src/unreviewed-file']:
    denied(lambda: open(path, 'w'))
denied(lambda: os.rename('src/protected', 'src/moved'))
denied(lambda: os.rename('src', 'moved'))
denied(lambda: os.unlink('src/protected/policy'))
denied(lambda: open('/factory/provider/auth.json').read())
link = Path('src/application/escape')
link.symlink_to(Path('src/protected/policy').absolute())
denied(lambda: link.write_text('tampered'))
link.unlink()
try:
    sock = socket.socket(); sock.settimeout(1); sock.connect(('1.1.1.1', 443))
except OSError as error:
    assert error.errno in [errno.EPERM, errno.EACCES, errno.ENETUNREACH], error
else:
    raise AssertionError('tool network permitted')
Path('src/application/result').write_text('all permission probes passed\n')
print('all permission probes passed')
"#,
    )
    .unwrap();
    commit_repository(workspace.path());
    let config = Config::new(data.path(), workspace.path()).unwrap();
    std::fs::create_dir_all(data.path().join("evidence/run-native-builder")).unwrap();
    let mut native = native_test_configuration();
    native.writable_paths = vec!["src".into()];
    let worker =
        ExecutionWorker::open_native(config.clone(), native.pin(&config).await.unwrap()).unwrap();
    let request = ExecutionRequest { version:1, operation_id:"run-native-builder-build".into(), run_id:"run-native-builder".into(), workspace:workspace.path().display().to_string(), evidence_directory:data.path().join("evidence/run-native-builder").display().to_string(), protected_paths:vec!["src/protected".into()], deadline_at:chrono::Utc::now()+chrono::Duration::seconds(180), execution:ExecutionKind::Specialist {role:"builder".into(), step:"build".into(), agent:AgentConfig { provider:"codex".into(), model:std::env::var("FACTORY_NATIVE_TEST_MODEL").unwrap_or_else(|_| "gpt-5.6-luna".into()), reasoning_level:"low".into(), permission_mode:"accept-edits".into()}, prompt:"Run /usr/bin/python3 -B probe.py exactly once with a tool. Do not modify the script or any other file. If the command succeeds, return exactly {\"probes\":\"passed\"} without markdown. Otherwise report the actual failure. Do not request escalation.".into()} };
    let encoded = serde_json::to_string(&request).unwrap();
    let output = worker
        .dispatch(
            &request.operation_id,
            &encoded,
            tokio_util::sync::CancellationToken::new(),
        )
        .await
        .unwrap();
    assert_eq!(
        serde_json::from_str::<serde_json::Value>(&output)
            .unwrap_or_else(|error| panic!("{error}; provider output: {output}")),
        serde_json::json!({"probes":"passed"})
    );
    assert_eq!(
        std::fs::read_to_string(workspace.path().join("src/application/result")).unwrap(),
        "all permission probes passed\n"
    );
    assert_eq!(
        std::fs::read_to_string(workspace.path().join("src/protected/policy")).unwrap(),
        "protected"
    );
    let receipt = worker.receipt(&request.operation_id).unwrap().unwrap();
    assert!(receipt.stopped && receipt.provider_policy.is_some());
    assert!(!workspace.path().join(".codex").exists());
    assert!(!workspace.path().join(".agents").exists());
}

#[tokio::test]
#[ignore = "real full native workflow; requires FACTORY_NATIVE_TEST_CONFIG with login and delegated cgroup v2"]
async fn native_rust_engine_plans_approves_builds_checks_reviews_and_reports() {
    use factory_runtime::{Runtime, Status, control::Control};
    use serde_json::json;
    let data = tempfile::tempdir().unwrap();
    let workspace = tempfile::tempdir().unwrap();
    std::fs::create_dir_all(workspace.path().join("src")).unwrap();
    std::fs::create_dir_all(workspace.path().join(".factory")).unwrap();
    std::fs::write(
        workspace.path().join("src/greeting.py"),
        "def greeting():\n    return 'Hello'\n",
    )
    .unwrap();
    std::fs::write(workspace.path().join("CONTEXT.md"), "# Domain model\n\n- greeting: the static message returned by greeting().\n- greeting(): the public, zero-argument function in src/greeting.py.\n").unwrap();
    let model =
        std::env::var("FACTORY_NATIVE_TEST_MODEL").unwrap_or_else(|_| "gpt-5.6-luna".into());
    std::fs::write(workspace.path().join(".factory/factory.yaml"),format!(r#"
version: 2
workflow: simple-sdlc
review_mode: single
worker_connection: {{runtime: codex, model_connection: native, codex_version: '0.154.0'}}
agents:
  planner: {{provider: codex, model: {model}, reasoning_level: low, permission_mode: auto}}
  builder: {{provider: codex, model: {model}, reasoning_level: low, permission_mode: accept-edits}}
  reviewer: {{provider: codex, model: {model}, reasoning_level: low, permission_mode: auto}}
quality:
  commands:
    - name: syntax
      argv: [/usr/bin/python3, -B, -c, "exec(open('src/greeting.py').read()); value = greeting(); assert value in ('Hello', 'Hello, world!'); print(repr(value))"]
      timeout_seconds: 60
limits: {{step_timeout_seconds: 300, total_run_timeout_seconds: 1800}}
"#)).unwrap();
    commit_repository(workspace.path());
    let config = Config::new(data.path(), workspace.path()).unwrap();
    let mut native = native_test_configuration();
    native.writable_paths = vec!["src".into(), "CONTEXT.md".into()];
    let runtime = Runtime::open_native(config.clone(), native).await.unwrap();
    let control = Control::open(runtime.clone()).await.unwrap();
    let planned = control.call("plan_start",json!({"request_id":"native-journey","request":"Change src/greeting.py so greeting() returns exactly 'Hello, world!' instead of 'Hello'. Keep the function name and signature. Keep the existing CONTEXT.md domain glossary aligned with the approved plan. Completion check: invoking greeting() returns 'Hello, world!'; the reviewed check executes the function and prints its actual value. No packaging, dependency or API changes are required. Keep the existing verification command. All scope and behavior decisions are specified; no clarification is needed."})).await.unwrap();
    let planning_run = planned["run_id"].as_str().unwrap();
    let session = planned["session_id"].as_str().unwrap();
    let run = runtime.resume(planning_run).await.unwrap();
    assert_eq!(run.status, Status::Planned, "{:?}", run.error);
    let plan = control
        .call("plan_status", json!({"session_id":session}))
        .await
        .unwrap();
    assert!(
        control
            .call(
                "plan_approve",
                json!({"session_id":session,"plan_hash":"stale"})
            )
            .await
            .is_err()
    );
    let approved = control
        .call(
            "plan_approve",
            json!({"session_id":session,"plan_hash":plan["plan_hash"]}),
        )
        .await
        .unwrap();
    let id = approved["implementation"]["run_id"].as_str().unwrap();
    // Reopen the engine before implementation: backend authority must persist.
    let restarted = Runtime::open(config).await.unwrap();
    let result = restarted.resume(id).await.unwrap();
    assert_eq!(result.status, Status::ReadyForReview, "{:?}", result.error);
    assert_eq!(
        result.execution_backend.as_ref().unwrap()["backend"],
        "native"
    );
    assert!(result.result.is_some());
    assert!(
        std::process::Command::new("/usr/bin/python3")
            .args([
                "-B",
                "-c",
                "exec(open('src/greeting.py').read()); assert greeting() == 'Hello, world!'"
            ])
            .current_dir(workspace.path())
            .status()
            .unwrap()
            .success()
    );
}

#[tokio::test]
#[ignore = "requires native Linux enforcement and FACTORY_NATIVE_TEST_CONFIG"]
async fn native_timeout_and_cancellation_confirm_cleanup_of_detached_descendants() {
    use factory_runtime::{
        Worker,
        workflow::{
            CheckResult, ExecutionKind, ExecutionRequest, QualityCommand, broker::ExecutionWorker,
        },
    };
    use std::sync::Arc;
    let data = tempfile::tempdir().unwrap();
    let workspace = tempfile::tempdir().unwrap();
    std::fs::write(workspace.path().join("source"), "original").unwrap();
    commit_repository(workspace.path());
    let config = Config::new(data.path(), workspace.path()).unwrap();
    std::fs::create_dir_all(data.path().join("evidence/run-native-cleanup")).unwrap();
    let native = native_test_configuration();
    let worker = Arc::new(
        ExecutionWorker::open_native(config.clone(), native.pin(&config).await.unwrap()).unwrap(),
    );
    let mut request=ExecutionRequest {version:1,operation_id:"run-native-cleanup-timeout".into(),run_id:"run-native-cleanup".into(),workspace:workspace.path().display().to_string(),evidence_directory:data.path().join("evidence/run-native-cleanup").display().to_string(),protected_paths:vec![],deadline_at:chrono::Utc::now()+chrono::Duration::seconds(20),execution:ExecutionKind::Quality {command:QualityCommand {name:"process-tree".into(),argv:vec!["/usr/bin/python3".into(),"-B".into(),"-c".into(),"import os,time; pid=os.fork(); os.setsid() if pid == 0 else None; print('descendant alive',flush=True); time.sleep(120)".into()],timeout_seconds:20,repeat_on_recovery:false},content_version:"cleanup".into()}};
    let encoded = serde_json::to_string(&request).unwrap();
    let result = worker
        .dispatch(
            &request.operation_id,
            &encoded,
            tokio_util::sync::CancellationToken::new(),
        )
        .await
        .unwrap();
    let check: CheckResult = serde_json::from_str(&result).unwrap();
    assert!(check.timed_out && !check.passed());
    assert!(check.output_tail.contains("descendant alive"));
    assert!(
        worker
            .receipt(&request.operation_id)
            .unwrap()
            .unwrap()
            .stopped
    );
    request.operation_id = "run-native-cleanup-cancel".into();
    request.deadline_at = chrono::Utc::now() + chrono::Duration::seconds(90);
    let encoded = serde_json::to_string(&request).unwrap();
    let cancel = tokio_util::sync::CancellationToken::new();
    let task = {
        let worker = worker.clone();
        let request = request.clone();
        let encoded = encoded.clone();
        let cancel = cancel.clone();
        tokio::spawn(async move {
            worker
                .dispatch(&request.operation_id, &encoded, cancel)
                .await
        })
    };
    let deadline = tokio::time::Instant::now() + std::time::Duration::from_secs(60);
    loop {
        if worker
            .receipt(&request.operation_id)
            .unwrap()
            .is_some_and(|r| r.creation_requested)
        {
            break;
        }
        assert!(
            tokio::time::Instant::now() < deadline,
            "native dispatch did not start"
        );
        tokio::time::sleep(std::time::Duration::from_millis(100)).await;
    }
    tokio::time::sleep(std::time::Duration::from_millis(500)).await;
    cancel.cancel();
    assert!(matches!(
        task.await.unwrap(),
        Err(factory_runtime::Error::Cancelled)
    ));
    worker
        .stop_operation(&request.operation_id, &encoded)
        .await
        .unwrap();
    assert!(
        worker
            .receipt(&request.operation_id)
            .unwrap()
            .unwrap()
            .stopped
    );
}

#[tokio::test]
async fn diagnostics_reject_changed_provider_bytes_before_executing_them() {
    use std::os::unix::fs::PermissionsExt;
    let data = tempfile::tempdir().unwrap();
    let workspace = tempfile::tempdir().unwrap();
    let executable = data.path().join("provider");
    std::fs::write(
        &executable,
        "#!/bin/sh\n: > \"$0.executed\"\nprintf 'codex-cli 0.154.0\\n'\n",
    )
    .unwrap();
    std::fs::set_permissions(&executable, std::fs::Permissions::from_mode(0o700)).unwrap();
    let native:NativeConfig=serde_json::from_value(serde_json::json!({"executable":executable,"executable_sha256":"previous-identity","bubblewrap":"/usr/bin/bwrap","cgroup_root":"/sys/fs/cgroup/factory","runtime_roots":["/usr"],"auth_file":null,"approval_policy":"never"})).unwrap();
    assert!(
        native
            .pin(&Config::new(data.path(), workspace.path()).unwrap())
            .await
            .is_err()
    );
    assert!(
        !data.path().join("provider.executed").exists(),
        "a changed pinned binary ran before identity validation"
    );
}

#[tokio::test]
async fn native_policy_refuses_host_socket_and_process_roots_before_provider_start() {
    let data = tempfile::tempdir().unwrap();
    let workspace = tempfile::tempdir().unwrap();
    let config = Config::new(data.path(), workspace.path()).unwrap();
    for root in ["/proc", "/proc/self", "/run", "/home", "/root"] {
        let native:NativeConfig=serde_json::from_value(serde_json::json!({"executable":"/usr/bin/true","bubblewrap":"/usr/bin/bwrap","cgroup_root":"/sys/fs/cgroup/factory","runtime_roots":[root],"auth_file":null,"approval_policy":"never"})).unwrap();
        let error = native.pin(&config).await.unwrap_err();
        assert!(
            error.to_string().contains("runtime roots"),
            "{root}: {error}"
        );
    }
}

// Compile a simulated provider at the external process boundary. Isolation tests
// above use the real provider; these tests target protocol timing and recovery.
fn simulated_provider(directory: &std::path::Path) -> std::path::PathBuf {
    let deps = std::env::current_exe()
        .unwrap()
        .parent()
        .unwrap()
        .to_path_buf();
    let serde_json = std::fs::read_dir(&deps)
        .unwrap()
        .map(|e| e.unwrap().path())
        .find(|p| {
            p.file_name()
                .unwrap()
                .to_string_lossy()
                .starts_with("libserde_json-")
                && p.extension().is_some_and(|e| e == "rlib")
        })
        .unwrap();
    let executable = directory.join("codex");
    let result = std::process::Command::new("rustc")
        .args(["--edition=2024", "-L"])
        .arg(format!("dependency={}", deps.display()))
        .arg("--extern")
        .arg(format!("serde_json={}", serde_json.display()))
        .arg(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/tests/fixtures/native-provider.rs"
        ))
        .arg("-o")
        .arg(&executable)
        .output()
        .unwrap();
    assert!(
        result.status.success(),
        "{}",
        String::from_utf8_lossy(&result.stderr)
    );
    std::fs::copy("/usr/bin/true", directory.join("codex-code-mode-host")).unwrap();
    executable
}
fn simulated_native_configuration(executable: std::path::PathBuf) -> NativeConfig {
    let mut native = native_test_configuration();
    native.executable = executable;
    native.executable_sha256 = None;
    native.code_mode_host_sha256 = None;
    native.auth_file = None;
    native.runtime_roots = vec!["/usr".into()];
    native.writable_paths = vec![];
    native
}
fn fixture_workflow() -> factory_runtime::workflow::WorkflowConfig {
    factory_runtime::workflow::WorkflowConfig::from_yaml(r#"
version: 2
workflow: simple-sdlc
review_mode: single
worker_connection: {runtime: codex, model_connection: native, codex_version: '0.154.0'}
agents:
  planner: {provider: codex, model: fixture-model, reasoning_level: low}
  builder: {provider: codex, model: fixture-model, reasoning_level: low, permission_mode: accept-edits}
  reviewer: {provider: codex, model: fixture-model, reasoning_level: low}
quality:
  commands: [{name: check, argv: [/usr/bin/true], timeout_seconds: 30}]
"#).unwrap()
}
fn fixture_repository() -> tempfile::TempDir {
    let workspace = tempfile::tempdir().unwrap();
    std::fs::write(workspace.path().join("source"), "original\n").unwrap();
    commit_repository(workspace.path());
    workspace
}
#[tokio::test]
#[ignore = "simulated protocol with real native supervision; requires delegated cgroup and FACTORY_NATIVE_TEST_CONFIG"]
async fn native_questions_before_turn_response_survive_reconnect_and_saved_answers() {
    use factory_runtime::{
        Reconciliation, Runtime, StartRequest, Status, Worker,
        workflow::{
            AgentConfig, ExecutionKind, ExecutionRequest, ImplementationInput,
            broker::ExecutionWorker,
        },
    };
    let data = tempfile::tempdir().unwrap();
    let workspace = fixture_repository();
    let provider = tempfile::tempdir().unwrap();
    let config = Config::new(data.path(), workspace.path()).unwrap();
    let runtime = Runtime::open_native(
        config.clone(),
        simulated_native_configuration(simulated_provider(provider.path())),
    )
    .await
    .unwrap();
    let mut input = StartRequest::new("native-question", "session", "Keep source unchanged");
    input.implementation = Some(ImplementationInput {
        config: fixture_workflow(),
        required_criteria: vec![],
        approved_plan: None,
    });
    let run = runtime.start(input).await.unwrap();
    let evidence = data.path().join("evidence").join(&run.run_id);
    std::fs::create_dir_all(&evidence).unwrap();
    let request = ExecutionRequest {
        version: 1,
        operation_id: format!("{}-question", run.run_id),
        run_id: run.run_id.clone(),
        workspace: workspace.path().display().to_string(),
        evidence_directory: evidence.display().to_string(),
        protected_paths: vec![],
        deadline_at: chrono::Utc::now() + chrono::Duration::seconds(30),
        execution: ExecutionKind::Specialist {
            role: "planner".into(),
            step: "plan".into(),
            agent: AgentConfig {
                provider: "codex".into(),
                model: "fixture-model".into(),
                reasoning_level: "low".into(),
                permission_mode: "auto".into(),
            },
            prompt: "question-before-start-response".into(),
        },
    };
    let encoded = serde_json::to_string(&request).unwrap();
    let worker = ExecutionWorker::configured(config.clone())
        .unwrap()
        .unwrap();
    assert!(matches!(
        worker
            .dispatch(
                &request.operation_id,
                &encoded,
                tokio_util::sync::CancellationToken::new()
            )
            .await,
        Err(factory_runtime::Error::Waiting(_))
    ));
    let saved = runtime.status(&run.run_id).await.unwrap();
    assert_eq!(saved.status, Status::WaitingForInput);
    let interaction = saved.workflow.unwrap().interaction.unwrap();
    drop(runtime);
    let restarted = Runtime::open(config.clone()).await.unwrap();
    restarted
        .answer_questions(
            &run.run_id,
            &interaction.interaction_id,
            std::collections::BTreeMap::from([("recipient".into(), "world".into())]),
        )
        .await
        .unwrap();
    let reconnected = ExecutionWorker::configured(config).unwrap().unwrap();
    assert_eq!(
        reconnected
            .reconcile(&request.operation_id, &encoded)
            .await
            .unwrap(),
        Reconciliation::Completed("fixture completed".into())
    );
    assert!(
        reconnected
            .receipt(&request.operation_id)
            .unwrap()
            .unwrap()
            .pending_input
            .unwrap()
            .delivered
    );
    reconnected
        .stop_operation(&request.operation_id, &encoded)
        .await
        .unwrap();
}

#[tokio::test]
#[ignore = "simulated protocol with actual native controller death; requires delegated cgroup and FACTORY_NATIVE_TEST_CONFIG"]
async fn native_turn_survives_controller_process_death_without_duplicate_dispatch() {
    use factory_runtime::{
        Runtime, StartRequest, Status,
        workflow::{ExecutionKind, ImplementationInput, broker::ExecutionWorker},
    };
    let data = tempfile::tempdir().unwrap();
    let workspace = fixture_repository();
    let provider = tempfile::tempdir().unwrap();
    let config = Config::new(data.path(), workspace.path()).unwrap();
    let runtime = Runtime::open_native(
        config.clone(),
        simulated_native_configuration(simulated_provider(provider.path())),
    )
    .await
    .unwrap();
    let mut input = StartRequest::new("native-restart", "session", "Keep source unchanged");
    input.implementation = Some(ImplementationInput {
        config: fixture_workflow(),
        required_criteria: vec![],
        approved_plan: None,
    });
    let run = runtime.start(input).await.unwrap();
    let worker = ExecutionWorker::configured(config.clone())
        .unwrap()
        .unwrap();
    let mut controller = std::process::Command::new(env!("CARGO_BIN_EXE_factory-runtime"))
        .arg(data.path())
        .arg(workspace.path())
        .args(["resume", &run.run_id])
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .spawn()
        .unwrap();
    let deadline = tokio::time::Instant::now() + std::time::Duration::from_secs(20);
    let active = loop {
        let saved = runtime.status(&run.run_id).await.unwrap();
        if let Some(state) = saved.workflow
            && let Some(request) = state.requests.values().find(|r| matches!(&r.execution, ExecutionKind::Specialist {role,..} if role == "planner"))
            && let Some(receipt) = worker.receipt(&request.operation_id).unwrap()
            && receipt.execution_id.is_some() && receipt.output.is_none()
        { break receipt; }
        assert!(
            tokio::time::Instant::now() < deadline,
            "native turn did not become active"
        );
        tokio::time::sleep(std::time::Duration::from_millis(25)).await;
    };
    controller.kill().unwrap();
    controller.wait().unwrap();
    let restarted = Runtime::open(config).await.unwrap();
    let deadline = tokio::time::Instant::now() + std::time::Duration::from_secs(30);
    let finished = loop {
        let result = restarted.resume(&run.run_id).await.unwrap();
        if result.status.is_terminal() {
            break result;
        }
        assert!(tokio::time::Instant::now() < deadline, "{:?}", result.error);
        tokio::time::sleep(std::time::Duration::from_millis(100)).await;
    };
    assert_eq!(
        finished.status,
        Status::ReadyForReview,
        "{:?}",
        finished.error
    );
    let receipt = worker.receipt(&active.operation_id).unwrap().unwrap();
    assert!(receipt.stopped);
    assert_eq!(receipt.execution_id, active.execution_id);
    assert_eq!(
        finished.workflow.unwrap().operations[&active.operation_id].dispatch_count,
        1
    );
}

fn commit_repository(workspace: &std::path::Path) {
    for args in [
        vec!["init", "-q"],
        vec!["config", "user.email", "test@example.com"],
        vec!["config", "user.name", "Test"],
        vec!["add", "."],
        vec!["commit", "-qm", "Initial"],
    ] {
        assert!(
            std::process::Command::new("git")
                .args(args)
                .current_dir(workspace)
                .status()
                .unwrap()
                .success()
        );
    }
}

fn native_test_configuration() -> NativeConfig {
    serde_json::from_slice(
        &std::fs::read(std::env::var("FACTORY_NATIVE_TEST_CONFIG").unwrap()).unwrap(),
    )
    .unwrap()
}

#[tokio::test]
async fn reconfiguration_without_hashes_checks_the_saved_backend_before_executing() {
    use std::os::unix::fs::PermissionsExt;
    let data = tempfile::tempdir().unwrap();
    let workspace = tempfile::tempdir().unwrap();
    let executable = data.path().join("provider");
    std::fs::write(
        &executable,
        "#!/bin/sh\n: > \"$0.executed\"\nprintf 'codex-cli 0.154.0\\n'\n",
    )
    .unwrap();
    std::fs::set_permissions(&executable, std::fs::Permissions::from_mode(0o700)).unwrap();
    let input = serde_json::json!({"executable":executable,"bubblewrap":"/usr/bin/bwrap","cgroup_root":"/sys/fs/cgroup/factory","runtime_roots":["/usr"],"auth_file":null,"approval_policy":"never"});
    let mut saved = input.clone();
    saved["executable_sha256"] = serde_json::json!("original-provider-identity");
    saved["code_mode_host_sha256"] = serde_json::json!("original-companion-identity");
    saved["bubblewrap_sha256"] = serde_json::json!("original-sandbox-identity");
    std::fs::write(
        data.path().join("native-backend.json"),
        serde_json::to_vec(&saved).unwrap(),
    )
    .unwrap();
    let native: NativeConfig = serde_json::from_value(input).unwrap();
    let config = Config::new(data.path(), workspace.path()).unwrap();
    assert!(
        factory_runtime::Runtime::open_native(config.clone(), native.clone())
            .await
            .is_err()
    );
    assert!(
        !data.path().join("provider.executed").exists(),
        "reconfiguration executed changed saved provider bytes"
    );
    assert!(native.pin(&config).await.is_err());
    assert!(
        !data.path().join("provider.executed").exists(),
        "doctor executed changed saved provider bytes"
    );
}
