use std::process::Command;

#[test]
fn native_entrypoint_never_falls_back_to_python() {
    let output = Command::new(env!("CARGO_BIN_EXE_software-factory"))
        .args(["run", "Build a greeting"])
        .env_remove("FACTORY_CONTROL_URL")
        .env_remove("FACTORY_HTTP_TOKEN")
        .env("PATH", "/nonexistent")
        .output()
        .unwrap();
    assert!(!output.status.success());
    let error = String::from_utf8(output.stderr).unwrap();
    assert!(error.contains("FACTORY_HTTP_TOKEN"), "{error}");
    assert!(error.contains("software-factory web"), "{error}");
}

use factory_runtime::{Config, Runtime, control::Control};
use serde_json::{Value, json};

struct Service {
    _data: tempfile::TempDir,
    workspace: tempfile::TempDir,
    origin: String,
    task: tokio::task::JoinHandle<()>,
}
impl Drop for Service {
    fn drop(&mut self) {
        self.task.abort();
    }
}
impl Service {
    async fn new() -> Self {
        Self::with_worker(None).await
    }
    async fn with_worker(worker: Option<std::sync::Arc<dyn factory_runtime::Worker>>) -> Self {
        let data = tempfile::tempdir().unwrap();
        let workspace = tempfile::tempdir().unwrap();
        std::fs::create_dir(workspace.path().join(".factory")).unwrap();
        std::fs::write(
            workspace.path().join(".factory/factory.yaml"),
            r#"
version: 2
workflow: simple-sdlc
worker_connection: {runtime: codex, model_connection: native, codex_version: '0.154.0'}
agents:
  planner: {provider: codex, model: gpt-5.4}
  builder: {provider: codex, model: gpt-5.4, permission_mode: accept-edits}
  reviewer: {provider: codex, model: gpt-5.4}
quality: {commands: [{name: check, argv: ["true"]}]}
"#,
        )
        .unwrap();
        let config = Config::new(data.path(), workspace.path()).unwrap();
        let runtime = if let Some(worker) = worker {
            Runtime::open_with_worker(
                config,
                worker,
                std::sync::Arc::new(factory_runtime::NoFaults),
            )
            .await
            .unwrap()
        } else {
            Runtime::open(config).await.unwrap()
        };
        let control = Control::open(runtime).await.unwrap();
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let app = factory_runtime::http::router(
            control,
            "product-test-token-123456789".into(),
            address.to_string(),
        );
        let task = tokio::spawn(async move {
            axum::serve(listener, app).await.unwrap();
        });
        Self {
            _data: data,
            workspace,
            origin: format!("http://{address}"),
            task,
        }
    }
    fn command(&self) -> tokio::process::Command {
        let mut command = tokio::process::Command::new(env!("CARGO_BIN_EXE_software-factory"));
        command
            .env("FACTORY_CONTROL_URL", &self.origin)
            .env("FACTORY_HTTP_TOKEN", "product-test-token-123456789")
            .env("PATH", "/nonexistent")
            .current_dir(self.workspace.path());
        command
    }
    async fn cli(&self, args: &[&str]) -> Value {
        let output = self.command().args(args).output().await.unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        serde_json::from_slice(&output.stdout).unwrap()
    }
}
#[tokio::test]
async fn cli_preserves_service_identity_and_checks_ownership() {
    let service = Service::new().await;
    let first = service
        .cli(&["start", "Add a greeting", "--request-id", "stable"])
        .await;
    let again = service
        .cli(&["start", "Add a greeting", "--request-id", "stable"])
        .await;
    assert_eq!(first["run_id"], again["run_id"]);
    let id = first["run_id"].as_str().unwrap();
    assert_eq!(service.cli(&["inspect", id]).await["status"], "pending");
    assert_eq!(service.cli(&["cancel", id]).await["status"], "stopping");
    let other = tempfile::tempdir().unwrap();
    let output = service
        .command()
        .args(["inspect", id, "-w"])
        .arg(other.path())
        .output()
        .await
        .unwrap();
    assert!(!output.status.success());
    assert!(String::from_utf8_lossy(&output.stderr).contains("different workspace"));
    let output = service
        .command()
        .args(["resume", "fct_old-langgraph"])
        .output()
        .await
        .unwrap();
    assert!(!output.status.success());
    assert!(String::from_utf8_lossy(&output.stderr).contains("historical release"));
}

struct Adapter {
    child: tokio::process::Child,
    input: tokio::process::ChildStdin,
    lines: tokio::io::Lines<tokio::io::BufReader<tokio::process::ChildStdout>>,
}
impl Adapter {
    async fn open(service: &Service, protocol: &str, home: &std::path::Path) -> Self {
        use tokio::io::AsyncBufReadExt;
        let mut child = service
            .command()
            .arg(protocol)
            .args(["--session", "adapter-test"])
            .env("FACTORY_CLIENT_HOME", home)
            .stdin(std::process::Stdio::piped())
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::inherit())
            .kill_on_drop(true)
            .spawn()
            .unwrap();
        let input = child.stdin.take().unwrap();
        let lines = tokio::io::BufReader::new(child.stdout.take().unwrap()).lines();
        Self {
            child,
            input,
            lines,
        }
    }
    async fn send(&mut self, value: Value) {
        use tokio::io::AsyncWriteExt;
        self.input
            .write_all(format!("{value}\n").as_bytes())
            .await
            .unwrap();
    }
    async fn read(&mut self) -> Value {
        let line = tokio::time::timeout(std::time::Duration::from_secs(10), self.lines.next_line())
            .await
            .unwrap()
            .unwrap()
            .expect("adapter closed stdout");
        serde_json::from_str(&line).unwrap()
    }
    async fn rpc(&mut self, id: u32, method: &str, params: Value) -> Value {
        self.send(json!({"jsonrpc":"2.0","id":id,"method":method,"params":params}))
            .await;
        loop {
            let result = self.read().await;
            if result["id"] == id {
                assert!(result.get("error").is_none(), "{result}");
                return result["result"].clone();
            }
        }
    }
}
#[tokio::test]
async fn mcp_uses_public_tool_names_and_disconnect_does_not_cancel_work() {
    let service = Service::new().await;
    let home = tempfile::tempdir().unwrap();
    let mut adapter = Adapter::open(&service, "mcp", home.path()).await;
    let init = adapter.rpc(1,"initialize",json!({"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"test","version":"1"}})).await;
    assert_eq!(init["protocolVersion"], "2025-03-26");
    adapter
        .send(json!({"jsonrpc":"2.0","method":"notifications/initialized"}))
        .await;
    let tools = adapter.rpc(2, "tools/list", json!({})).await;
    for name in [
        "factory_start",
        "factory_settings",
        "factory_plan_start",
        "factory_plan_approve",
        "factory_plan_cancel",
    ] {
        assert!(
            tools["tools"]
                .as_array()
                .unwrap()
                .iter()
                .any(|t| t["name"] == name),
            "{name}"
        );
    }
    let start = adapter.rpc(3,"tools/call",json!({"name":"factory_start","arguments":{"request":"Add a greeting","request_id":"mcp-start"}})).await;
    assert_eq!(start["isError"], false);
    let run: Value = serde_json::from_str(start["content"][0]["text"].as_str().unwrap()).unwrap();
    adapter.child.kill().await.unwrap();
    assert_eq!(
        service
            .cli(&["inspect", run["run_id"].as_str().unwrap()])
            .await["status"],
        "pending"
    );
    let mut reconnect = Adapter::open(&service, "mcp", home.path()).await;
    let locked=reconnect.rpc(9,"tools/call",json!({"name":"factory_settings","arguments":{"selection":{"shared":{"model":"gpt-5.4"}}}})).await;
    assert_eq!(
        locked["isError"], true,
        "active session settings must be immutable"
    );

    let status = reconnect
        .rpc(
            1,
            "tools/call",
            json!({"name":"factory_status","arguments":{"run_id":run["run_id"]}}),
        )
        .await;
    let saved: Value =
        serde_json::from_str(status["content"][0]["text"].as_str().unwrap()).unwrap();
    assert_eq!(saved["run_id"], run["run_id"]);
}

#[tokio::test]
async fn acp_reloads_chat_and_attaches_the_same_cli_run_without_resuming() {
    let service = Service::new().await;
    let home = tempfile::tempdir().unwrap();
    let run = service
        .cli(&["start", "A greeting", "--request-id", "acp-attach"])
        .await;
    let mut adapter = Adapter::open(&service, "acp", home.path()).await;
    let init = adapter
        .rpc(
            1,
            "initialize",
            json!({"protocolVersion":1,"clientCapabilities":{}}),
        )
        .await;
    assert_eq!(init["agentCapabilities"]["loadSession"], true);
    let session = adapter
        .rpc(
            2,
            "session/new",
            json!({"cwd":service.workspace.path(),"mcpServers":[]}),
        )
        .await;
    let sid = session["sessionId"].as_str().unwrap();
    assert!(!session["configOptions"].as_array().unwrap().is_empty());
    adapter.send(json!({"jsonrpc":"2.0","id":3,"method":"session/prompt","params":{"sessionId":sid,"prompt":[{"type":"text","text":format!("/attach {}",run["run_id"].as_str().unwrap())}]}})).await;
    let update = adapter.read().await;
    assert_eq!(update["method"], "session/update");
    assert!(
        update["params"]["update"]["content"]["text"]
            .as_str()
            .unwrap()
            .contains(run["run_id"].as_str().unwrap())
    );
    assert_eq!(adapter.read().await["result"]["stopReason"], "end_turn");
    adapter
        .send(json!({"jsonrpc":"2.0","method":"session/cancel","params":{"sessionId":sid}}))
        .await;
    adapter.child.kill().await.unwrap();
    let mut reload = Adapter::open(&service, "acp", home.path()).await;
    reload.send(json!({"jsonrpc":"2.0","id":4,"method":"session/load","params":{"sessionId":sid,"cwd":service.workspace.path(),"mcpServers":[]}})).await;
    let history = reload.read().await;
    assert_eq!(
        history["params"]["update"]["sessionUpdate"],
        "user_message_chunk"
    );
    assert_eq!(
        reload.read().await["params"]["update"]["sessionUpdate"],
        "agent_message_chunk"
    );
    assert!(reload.read().await["result"]["configOptions"].is_array());
    assert_eq!(
        service
            .cli(&["inspect", run["run_id"].as_str().unwrap()])
            .await["status"],
        "pending"
    );
}

#[tokio::test]
async fn bb_panel_selected_models_remain_in_options_after_reload() {
    struct SettingsWorker;
    #[async_trait::async_trait]
    impl factory_runtime::Worker for SettingsWorker {
        async fn model_catalog(
            &self,
        ) -> factory_runtime::Result<Vec<factory_runtime::workflow::config::ModelOption>> {
            Ok(serde_json::from_value(json!([{
                "provider":"codex", "model":"gpt-5.4", "name":"GPT-5.4",
                "efforts":["low","high"], "default_effort":"high", "is_default":true,
                "permission_modes":["auto","accept-edits"]
            }]))?)
        }
        async fn reconcile(
            &self,
            _: &str,
            _: &str,
        ) -> factory_runtime::Result<factory_runtime::Reconciliation> {
            panic!("settings must not execute workers")
        }
        async fn dispatch(
            &self,
            _: &str,
            _: &str,
            _: tokio_util::sync::CancellationToken,
        ) -> factory_runtime::Result<String> {
            panic!("settings must not execute workers")
        }
    }
    let service = Service::with_worker(Some(std::sync::Arc::new(SettingsWorker))).await;
    let home = tempfile::tempdir().unwrap();
    let mut adapter = Adapter::open(&service, "acp", home.path()).await;
    let session = adapter
        .rpc(1, "session/new", json!({"cwd":service.workspace.path()}))
        .await;
    let sid = session["sessionId"].as_str().unwrap();
    for option in session["configOptions"].as_array().unwrap() {
        if option["id"] == "preset" {
            continue;
        }
        assert_eq!(option["currentValue"], "inherit");
        let choices = option["options"].as_array().unwrap();
        assert_eq!(choices.len(), 3);
        for choice in choices[1..].iter().chain(&choices[..1]) {
            // Each request starts a fresh panel process, exercising saved state.
            for request in [
                json!({"action":"change","config_id":option["id"],"value":choice["value"],"session_id":sid}),
                json!({"action":"read","session_id":sid}),
            ] {
                use tokio::io::AsyncWriteExt;
                let mut panel = service
                    .command()
                    .arg("settings-panel")
                    .env("FACTORY_CLIENT_HOME", home.path())
                    .stdin(std::process::Stdio::piped())
                    .stdout(std::process::Stdio::piped())
                    .spawn()
                    .unwrap();
                panel
                    .stdin
                    .take()
                    .unwrap()
                    .write_all(request.to_string().as_bytes())
                    .await
                    .unwrap();
                let output = panel.wait_with_output().await.unwrap();
                assert!(
                    output.status.success(),
                    "{}",
                    String::from_utf8_lossy(&output.stderr)
                );
                let saved: Value = serde_json::from_slice(&output.stdout).unwrap();
                let selected = saved["configOptions"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .find(|o| o["id"] == option["id"])
                    .unwrap();
                assert_eq!(selected["currentValue"], choice["value"]);
                assert!(
                    selected["options"]
                        .as_array()
                        .unwrap()
                        .iter()
                        .any(|c| c["value"] == selected["currentValue"])
                );
            }
        }
    }
}

#[tokio::test]
async fn planning_cli_and_bb_panel_share_saved_settings() {
    let service = Service::new().await;
    let home = tempfile::tempdir().unwrap();
    let mut adapter = Adapter::open(&service, "acp", home.path()).await;
    let session = adapter
        .rpc(1, "session/new", json!({"cwd":service.workspace.path()}))
        .await;
    let id = session["sessionId"].as_str().unwrap();
    let mut panel = service
        .command()
        .args(["settings-panel"])
        .env("FACTORY_CLIENT_HOME", home.path())
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .spawn()
        .unwrap();
    use tokio::io::AsyncWriteExt;
    panel
        .stdin
        .take()
        .unwrap()
        .write_all(format!("{}", json!({"action":"read","session_id":id})).as_bytes())
        .await
        .unwrap();
    let output = panel.wait_with_output().await.unwrap();
    assert!(output.status.success());
    let saved: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(saved["sessionId"], id);
    assert_eq!(saved["configOptions"], session["configOptions"]);
    let plan = service
        .cli(&["plan", "Plan a greeting", "--request-id", "shared-plan"])
        .await;
    let sid = plan["session_id"].as_str().unwrap();
    let status = service.cli(&["plan-status", sid]).await;
    assert_eq!(status["run_id"], plan["run_id"]);
    assert_eq!(
        service.cli(&["plan-cancel", sid]).await["status"],
        "stopping"
    );
    let stale = service
        .command()
        .args(["plan-approve", sid, "wrong-hash"])
        .output()
        .await
        .unwrap();
    assert!(!stale.status.success());
}

#[test]
fn native_policy_setup_needs_no_service_or_python() {
    let workspace = tempfile::tempdir().unwrap();
    let cli = |args: &[&str]| {
        Command::new(env!("CARGO_BIN_EXE_software-factory"))
            .args(args)
            .arg("-w")
            .arg(workspace.path())
            .env("PATH", "/nonexistent")
            .env_remove("FACTORY_HTTP_TOKEN")
            .output()
            .unwrap()
    };
    let init = cli(&["init"]);
    assert!(
        init.status.success(),
        "{}",
        String::from_utf8_lossy(&init.stderr)
    );
    assert!(!cli(&["init"]).status.success());
    let check = cli(&["repo-check"]);
    assert!(
        check.status.success(),
        "{}",
        String::from_utf8_lossy(&check.stderr)
    );
    assert_eq!(
        serde_json::from_slice::<Value>(&check.stdout).unwrap()["configuration"]["version"],
        2
    );
    let graph = cli(&["graph", "--format", "mermaid"]);
    assert!(graph.status.success());
    assert!(String::from_utf8_lossy(&graph.stdout).contains("review"));
    assert!(cli(&["convert-config"]).status.success());
}

#[tokio::test]
async fn operator_commands_use_service_owned_state_and_public_errors() {
    let service = Service::new().await;
    assert!(service.cli(&["doctor"]).await["service"].as_str().is_some());
    assert!(service.cli(&["models"]).await.is_array());
    assert!(service.cli(&["list"]).await.is_array());
    let run = service
        .cli(&["start", "Review greeting", "--request-id", "review-tools"])
        .await;
    let id = run["run_id"].as_str().unwrap();
    let evidence = service.cli(&["review-quality", id]).await;
    assert_eq!(evidence["run_id"], id);
    assert_eq!(evidence["evidence"], json!({}));
    for args in [
        vec!["deliver", id],
        vec!["fix-reviews", id, "--thread", "missing"],
        vec!["review-threads", id],
        vec![
            "review-feedback",
            id,
            "1",
            "code",
            "absent",
            "useful",
            "Reason",
        ],
    ] {
        let output = service.command().args(args).output().await.unwrap();
        assert!(!output.status.success());
        let error = String::from_utf8_lossy(&output.stderr);
        assert!(!error.contains("invalid command"), "{error}");
    }
}

#[tokio::test]
async fn canonical_web_packages_assets_and_requires_a_real_backend_by_default() {
    let data = tempfile::tempdir().unwrap();
    let workspace = tempfile::tempdir().unwrap();
    let error = tokio::time::timeout(
        std::time::Duration::from_secs(2),
        tokio::process::Command::new(env!("CARGO_BIN_EXE_software-factory"))
            .args(["web", "--storage"])
            .arg(data.path())
            .arg("-w")
            .arg(workspace.path())
            .env_remove("FACTORY_ALLOW_FAKE_WORKER")
            .kill_on_drop(true)
            .output(),
    )
    .await
    .expect("unconfigured product must reject fake execution")
    .unwrap();
    assert!(!error.status.success());
    assert!(String::from_utf8_lossy(&error.stderr).contains("configure-docker"));
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    drop(listener);
    let mut child = tokio::process::Command::new(env!("CARGO_BIN_EXE_software-factory"))
        .args(["web", "--storage"])
        .arg(data.path())
        .arg("-w")
        .arg(workspace.path())
        .args(["--port", &port.to_string()])
        .env("FACTORY_ALLOW_FAKE_WORKER", "1")
        .env("FACTORY_HTTP_TOKEN", "canonical-web-test-secret-123456")
        .env_remove("FACTORY_HTTP_HOST")
        .env_remove("FACTORY_HTTP_ORIGIN")
        .env_remove("FACTORY_CONTROL_URL")
        .env("PATH", "/nonexistent")
        .stdout(std::process::Stdio::null())
        .kill_on_drop(true)
        .spawn()
        .unwrap();
    let http = reqwest::Client::new();
    let origin = format!("http://127.0.0.1:{port}");
    let deadline = tokio::time::Instant::now() + std::time::Duration::from_secs(10);
    loop {
        if http
            .get(format!("{origin}/health/ready"))
            .send()
            .await
            .is_ok()
        {
            break;
        }
        assert!(child.try_wait().unwrap().is_none(), "service exited early");
        assert!(tokio::time::Instant::now() < deadline);
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    assert_eq!(
        http.get(format!("{origin}/static/app.js"))
            .send()
            .await
            .unwrap()
            .status(),
        401
    );
    for path in [
        "/",
        "/static/app.js",
        "/static/app.css",
        "/static/geist-latin.woff2",
        "/static/geist-LICENSE.txt",
    ] {
        let response = http
            .get(format!("{origin}{path}"))
            .bearer_auth("canonical-web-test-secret-123456")
            .send()
            .await
            .unwrap();
        assert_eq!(response.status(), 200, "{path}");
        assert!(!response.bytes().await.unwrap().is_empty());
    }
    // A different data directory/port cannot bypass workspace service ownership.
    let other_data = tempfile::tempdir().unwrap();
    let duplicate = tokio::time::timeout(
        std::time::Duration::from_secs(3),
        tokio::process::Command::new(env!("CARGO_BIN_EXE_software-factory"))
            .args(["web", "--storage"])
            .arg(other_data.path())
            .arg("-w")
            .arg(workspace.path())
            .env("FACTORY_ALLOW_FAKE_WORKER", "1")
            .env("FACTORY_HTTP_BIND", "127.0.0.1:0")
            .env_remove("FACTORY_HTTP_HOST")
            .env_remove("FACTORY_HTTP_ORIGIN")
            .kill_on_drop(true)
            .output(),
    )
    .await
    .expect("second controller must fail before listening")
    .unwrap();
    assert!(!duplicate.status.success());
    assert!(String::from_utf8_lossy(&duplicate.stderr).contains("owned by another executor"));
    // The unset URL selects the same local Rust service (default port is documented).
    child.kill().await.unwrap();
}

#[derive(Default)]
struct JourneyWorker(std::sync::Mutex<std::collections::BTreeMap<String, (String, String)>>);
#[async_trait::async_trait]
impl factory_runtime::Worker for JourneyWorker {
    async fn reconcile(
        &self,
        id: &str,
        input: &str,
    ) -> factory_runtime::Result<factory_runtime::Reconciliation> {
        Ok(match self.0.lock().unwrap().get(id) {
            Some((saved, result)) => {
                assert_eq!(saved, input);
                factory_runtime::Reconciliation::Completed(result.clone())
            }
            None => factory_runtime::Reconciliation::Absent,
        })
    }
    async fn dispatch(
        &self,
        id: &str,
        input: &str,
        _: tokio_util::sync::CancellationToken,
    ) -> factory_runtime::Result<String> {
        use factory_runtime::workflow::{CheckResult, ExecutionKind, ExecutionRequest};
        let request: ExecutionRequest = serde_json::from_str(input)?;
        let result=match request.execution {
            ExecutionKind::Quality {command,content_version}=>serde_json::to_value(CheckResult{name:command.name,argv:command.argv,exit_code:Some(0),timed_out:false,cancelled:false,output_tail:String::new(),content_version,comparison:String::new()})?,
            ExecutionKind::Specialist{prompt,..} if prompt.contains("read-only Grill with Docs")=>{
                if id.ends_with("grill-1") {json!({"version":1,"status":"questions","summary":"Choose recipient","questions":[{"id":"recipient","question":"Who?","options":["world"],"recommendation":"world"}]})}
                else {json!({"version":1,"status":"ready","summary":"Greeting plan","plan":{"goal":"Change greeting","scope":["greeting.txt"],"write_scope":["greeting.txt"],"requirements":["Hello, world!"],"completion_checks":[{"id":"greeting","description":"The greeting is Hello, world!"}]}})}
            }
            ExecutionKind::Specialist{step,..} if step=="plan"=>json!({"version":2,"status":"ready","summary":"Update greeting","acceptance_criteria":[{"id":"greeting","description":"The greeting is Hello, world!"}],"implementation_steps":["Update greeting.txt"],"test_strategy":"Verify greeting"}),
            ExecutionKind::Specialist{step,..} if step=="build"=>{
                std::fs::write(std::path::Path::new(&request.workspace).join("greeting.txt"),"Hello, world!\n")?;
                json!({"status":"completed","summary":"Updated greeting","changed_files":["greeting.txt"]})
            }
            ExecutionKind::Specialist{prompt,..}=>{
                let context:Value=serde_json::from_str(prompt.split("<factory_data>\n").nth(1).unwrap().split("\n</factory_data>").next().unwrap())?;
                let bundle=&context["bundle"];
                json!({"version":2,"content_version":bundle["content_version"],"approved":true,"summary":"Verified greeting","findings":[],"criteria":bundle["context"]["plan"]["acceptance_criteria"].as_array().unwrap().iter().map(|c|json!({"criterion_id":c["id"],"status":"satisfied","evidence":"Greeting verified","evidence_refs":[bundle["checks"].clone()]})).collect::<Vec<_>>(),"resolutions":[]})
            }
        }.to_string();
        assert!(
            self.0
                .lock()
                .unwrap()
                .insert(id.into(), (input.into(), result.clone()))
                .is_none()
        );
        Ok(result)
    }
}
#[tokio::test]
async fn browser_mcp_cli_and_acp_share_exact_approval_and_completed_report() {
    let service = Service::with_worker(Some(std::sync::Arc::new(JourneyWorker::default()))).await;
    for args in [
        vec!["init", "-q"],
        vec!["config", "user.email", "test@example.com"],
        vec!["config", "user.name", "Test"],
    ] {
        assert!(
            Command::new("git")
                .args(args)
                .current_dir(service.workspace.path())
                .status()
                .unwrap()
                .success()
        );
    }
    std::fs::write(service.workspace.path().join("greeting.txt"), "Hello\n").unwrap();
    for args in [vec!["add", "."], vec!["commit", "-qm", "Initial"]] {
        assert!(
            Command::new("git")
                .args(args)
                .current_dir(service.workspace.path())
                .status()
                .unwrap()
                .success()
        );
    }
    // Browser contract starts work; CLI and MCP continue the saved planning identity.
    let http = reqwest::Client::new();
    let started: Value = http
        .post(format!("{}/api/sessions", service.origin))
        .bearer_auth("product-test-token-123456789")
        .json(&json!({"request":"Change the greeting","request_id":"journey"}))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    let sid = started["session_id"].as_str().unwrap();
    // Drive the service-owned supervisor through the same durable runtime used in production.
    let config = Config::new(service._data.path(), service.workspace.path()).unwrap();
    let runtime = Runtime::open_with_worker(
        config,
        std::sync::Arc::new(JourneyWorker::default()),
        std::sync::Arc::new(factory_runtime::NoFaults),
    )
    .await
    .unwrap();
    let control = Control::open(runtime).await.unwrap();
    let supervisor = tokio::spawn(factory_runtime::http::supervise(control));
    let deadline = tokio::time::Instant::now() + std::time::Duration::from_secs(20);
    let pending = loop {
        let status = service.cli(&["plan-status", sid]).await;
        if status["pending"].is_object() {
            break status;
        }
        assert!(tokio::time::Instant::now() < deadline, "{status}");
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    };
    let home = tempfile::tempdir().unwrap();
    let mut mcp = Adapter::open(&service, "mcp", home.path()).await;
    mcp.rpc(1,"tools/call",json!({"name":"factory_plan_answer","arguments":{"session_id":sid,"interaction_id":pending["pending"]["interaction_id"],"answers":{"recipient":"world"}}})).await;
    let planned = loop {
        let status = service.cli(&["plan-status", sid]).await;
        if status["plan_hash"].is_string() {
            break status;
        }
        assert!(tokio::time::Instant::now() < deadline, "{status}");
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    };
    let rejected = service
        .command()
        .args(["plan-approve", sid, "incorrect"])
        .output()
        .await
        .unwrap();
    assert!(!rejected.status.success());
    let approved = service
        .cli(&["plan-approve", sid, planned["plan_hash"].as_str().unwrap()])
        .await;
    let id = approved["handoff"]["implementation_run_id"]
        .as_str()
        .unwrap();
    assert_eq!(
        service
            .cli(&["plan-approve", sid, planned["plan_hash"].as_str().unwrap()])
            .await["handoff"]["implementation_run_id"],
        id
    );
    mcp.child.kill().await.unwrap();
    loop {
        let status = service.cli(&["inspect", id]).await;
        if status["status"] == "ready_for_review" {
            break;
        }
        assert!(tokio::time::Instant::now() < deadline, "{status}");
        assert!(
            !matches!(status["status"].as_str(), Some("failed" | "blocked")),
            "{status}"
        );
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    let report = service.cli(&["plan-report", sid]).await;
    assert!(report["report"].as_str().unwrap().contains("greeting.txt"));
    assert_eq!(
        std::fs::read_to_string(service.workspace.path().join("greeting.txt")).unwrap(),
        "Hello, world!\n"
    );
    let mut acp = Adapter::open(&service, "acp", home.path()).await;
    let chat = acp
        .rpc(1, "session/new", json!({"cwd":service.workspace.path()}))
        .await;
    let result=acp.rpc(2,"session/prompt",json!({"sessionId":chat["sessionId"],"prompt":[{"type":"text","text":format!("/attach {id}")}]})).await;
    assert_eq!(result["stopReason"], "end_turn");
    assert_eq!(
        service.cli(&["inspect", id]).await["status"],
        "ready_for_review"
    );
    supervisor.abort();
}

#[tokio::test]
async fn mcp_planning_uses_the_registered_client_settings_session() {
    let service = Service::new().await;
    let http = reqwest::Client::new();
    let configure=http.post(format!("{}/api/control/settings",service.origin)).bearer_auth("product-test-token-123456789").json(&json!({"session_id":"adapter-test","selection":{"shared":{"model":"chosen-model","reasoning_level":"high"}}})).send().await.unwrap();
    assert!(configure.status().is_success());
    let home = tempfile::tempdir().unwrap();
    let mut mcp = Adapter::open(&service, "mcp", home.path()).await;
    let started=mcp.rpc(1,"tools/call",json!({"name":"factory_plan_start","arguments":{"request":"Plan with chosen model","request_id":"selected-model"}})).await;
    assert_eq!(started["isError"], false, "{started}");
    let plan: Value =
        serde_json::from_str(started["content"][0]["text"].as_str().unwrap()).unwrap();
    let config: Value = http
        .post(format!("{}/api/control/settings", service.origin))
        .bearer_auth("product-test-token-123456789")
        .json(&json!({"run_id":plan["run_id"]}))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(
        config["resolved_settings"]["effective"]["plan"]["model"],
        "chosen-model"
    );
}

#[tokio::test]
async fn acp_accepts_a_canonical_client_home_through_an_os_path_alias() {
    let service = Service::new().await;
    let root = tempfile::tempdir().unwrap();
    let real = root.path().join("real");
    std::fs::create_dir(&real).unwrap();
    let alias = root.path().join("alias");
    std::os::unix::fs::symlink(&real, &alias).unwrap();
    let mut adapter = Adapter::open(&service, "acp", &alias.join("client")).await;
    let session = adapter
        .rpc(1, "session/new", json!({"cwd":service.workspace.path()}))
        .await;
    assert!(session["sessionId"].is_string());
}

#[tokio::test]
async fn panel_without_thread_reference_reuses_the_latest_workspace_chat() {
    use tokio::io::AsyncWriteExt;
    let service = Service::new().await;
    let home = tempfile::tempdir().unwrap();
    let mut results = vec![];
    for request in [
        json!({"action":"read"}),
        json!({"action":"change","config_id":"shared","value":"{\"provider\":\"codex\",\"model\":\"gpt-5.4\",\"reasoning_level\":\"high\"}"}),
        json!({"action":"read"}),
    ] {
        let mut child = service
            .command()
            .arg("settings-panel")
            .env("FACTORY_CLIENT_HOME", home.path())
            .stdin(std::process::Stdio::piped())
            .stdout(std::process::Stdio::piped())
            .spawn()
            .unwrap();
        child
            .stdin
            .take()
            .unwrap()
            .write_all(request.to_string().as_bytes())
            .await
            .unwrap();
        let output = child.wait_with_output().await.unwrap();
        assert!(output.status.success());
        results.push(serde_json::from_slice::<Value>(&output.stdout).unwrap());
    }
    assert_eq!(results[0]["sessionId"], results[1]["sessionId"]);
    assert_eq!(results[1], results[2]);
}

#[tokio::test]
async fn acp_strips_bb_wrappers_without_starting_work_from_system_notifications() {
    let service = Service::new().await;
    let home = tempfile::tempdir().unwrap();
    let mut adapter = Adapter::open(&service, "acp", home.path()).await;
    let chat = adapter
        .rpc(1, "session/new", json!({"cwd":service.workspace.path()}))
        .await;
    let id = chat["sessionId"].as_str().unwrap();
    let completion = "<system_instructions>\nBB context\n</system_instructions>\n[bb system]\n\n@thread:child completed:\n\n{}";
    assert_eq!(
        adapter
            .rpc(
                2,
                "session/prompt",
                json!({"sessionId":id,"prompt":[{"type":"text","text":completion}]})
            )
            .await["stopReason"],
        "end_turn"
    );
    assert_eq!(service.cli(&["list"]).await, json!([]));
    let wrapped = "<system_instructions>\nBB context\n</system_instructions>\n[bb message from thread:parent_1]\n\n/new Actual user request";
    adapter
        .rpc(
            3,
            "session/prompt",
            json!({"sessionId":id,"prompt":[{"type":"text","text":wrapped}]}),
        )
        .await;
    let runs = service.cli(&["list"]).await;
    assert_eq!(runs.as_array().unwrap().len(), 1);
    assert_eq!(runs[0]["request"], "Actual user request");
}

#[tokio::test]
async fn acp_plain_plan_prefixed_words_remain_ordinary_work_requests() {
    let service = Service::new().await;
    let home = tempfile::tempdir().unwrap();
    let mut adapter = Adapter::open(&service, "acp", home.path()).await;
    let chat = adapter
        .rpc(1, "session/new", json!({"cwd":service.workspace.path()}))
        .await;
    adapter.rpc(2,"session/prompt",json!({"sessionId":chat["sessionId"],"prompt":[{"type":"text","text":"planning improvements for the greeting"}]})).await;
    let runs = service.cli(&["list"]).await;
    assert_eq!(runs.as_array().unwrap().len(), 1);
    assert_eq!(runs[0]["request"], "planning improvements for the greeting");
}

#[tokio::test]
async fn rust_settings_session_names_are_not_interpreted_as_legacy_checkpoints() {
    let service = Service::new().await;
    let first = service
        .cli(&[
            "start",
            "Native request",
            "--session",
            "checkpoint-study",
            "--request-id",
            "native-session-name",
        ])
        .await;
    assert!(first["run_id"].as_str().unwrap().starts_with("run-"));
    assert!(service.cli(&["settings", "--session", "fct_team"]).await["configuration"].is_object());
}

#[tokio::test]
async fn mcp_preserves_explicit_null_for_nullable_optional_arguments() {
    let service = Service::new().await;
    let home = tempfile::tempdir().unwrap();
    let mut adapter = Adapter::open(&service, "mcp", home.path()).await;
    let read = adapter
        .rpc(
            1,
            "tools/call",
            json!({"name":"factory_settings","arguments":{"run_id":null,"selection":null}}),
        )
        .await;
    assert_eq!(read["isError"], false);
    let start=adapter.rpc(2,"tools/call",json!({"name":"factory_start","arguments":{"request":"Nullable selection","request_id":"nullable","selection":null}})).await;
    assert_eq!(start["isError"], false);
    let listing = adapter.rpc(3, "tools/list", json!({})).await;
    let settings = listing["tools"]
        .as_array()
        .unwrap()
        .iter()
        .find(|t| t["name"] == "factory_settings")
        .unwrap();
    assert_eq!(
        settings["inputSchema"]["properties"]["run_id"]["type"],
        json!(["string", "null"])
    );
}

#[test]
fn policy_setup_accepts_os_workspace_aliases_but_rejects_linked_policy_directories() {
    let root = tempfile::tempdir().unwrap();
    let real = root.path().join("real");
    std::fs::create_dir(&real).unwrap();
    let alias = root.path().join("alias");
    std::os::unix::fs::symlink(&real, &alias).unwrap();
    let invoke = |workspace: &std::path::Path| {
        Command::new(env!("CARGO_BIN_EXE_software-factory"))
            .arg("init")
            .arg("-w")
            .arg(workspace)
            .output()
            .unwrap()
    };
    let output = invoke(&alias);
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let unsafe_workspace = root.path().join("linked-policy");
    std::fs::create_dir(&unsafe_workspace).unwrap();
    let external = root.path().join("external");
    std::fs::create_dir(&external).unwrap();
    std::os::unix::fs::symlink(&external, unsafe_workspace.join(".factory")).unwrap();
    assert!(!invoke(&unsafe_workspace).status.success());
    assert!(!external.join("factory.yaml").exists());
}
