use factory_runtime::{Config, Runtime};
use serde_json::json;

#[tokio::test]
async fn planning_start_is_durable_idempotent_and_scoped() {
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
quality:
  commands: [{name: check, argv: ["true"]}]
"#,
    )
    .unwrap();
    let config = Config::new(data.path(), workspace.path()).unwrap();
    let runtime = Runtime::open(config.clone()).await.unwrap();
    let control = factory_runtime::control::Control::open(runtime)
        .await
        .unwrap();
    let input = json!({"request":"Add a greeting", "request_id":"planning-start"});
    let first = control.call("plan_start", input.clone()).await.unwrap();
    let restarted = factory_runtime::control::Control::open(Runtime::open(config).await.unwrap())
        .await
        .unwrap();
    assert_eq!(first, restarted.call("plan_start", input).await.unwrap());
    assert_eq!(
        restarted
            .call("plan_status", json!({"session_id":first["session_id"]}))
            .await
            .unwrap()["status"],
        "pending"
    );
    assert!(
        restarted
            .call(
                "plan_start",
                json!({"request":"Different", "request_id":"planning-start"})
            )
            .await
            .is_err()
    );
    let other = tempfile::tempdir().unwrap();
    let other = factory_runtime::control::Control::open(
        Runtime::open(Config::new(data.path(), other.path()).unwrap())
            .await
            .unwrap(),
    )
    .await
    .unwrap();
    assert!(
        other
            .call("plan_status", json!({"session_id":first["session_id"]}))
            .await
            .is_err()
    );
    let cancelled = control
        .call("plan_cancel", json!({"session_id":first["session_id"]}))
        .await
        .unwrap();
    assert_eq!(cancelled["status"], "stopping");
    assert_eq!(
        control
            .runtime
            .resume(first["run_id"].as_str().unwrap())
            .await
            .unwrap()
            .status,
        factory_runtime::Status::Cancelled
    );
}

#[derive(Default)]
struct Interviewer {
    receipts: std::sync::Mutex<std::collections::BTreeMap<String, (String, String)>>,
}
#[async_trait::async_trait]
impl factory_runtime::Worker for Interviewer {
    async fn reconcile(
        &self,
        id: &str,
        input: &str,
    ) -> factory_runtime::Result<factory_runtime::Reconciliation> {
        Ok(match self.receipts.lock().unwrap().get(id) {
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
        let request: factory_runtime::workflow::ExecutionRequest = serde_json::from_str(input)?;
        assert!(request.read_only());
        assert!(std::path::Path::new(&request.evidence_directory).is_dir());
        let result=if id.ends_with("grill-1") {json!({"version":1,"status":"questions","summary":"Choose a recipient","questions":[{"id":"recipient","question":"Who?","options":["world"],"recommendation":"world","context":"A public greeting"}],"plan":null})} else {json!({"version":1,"status":"ready","summary":"Greeting planned","questions":[],"decisions":[{"id":"recipient","title":"Recipient","decision":"world","rationale":"User selected world","adr":true}],"glossary":[{"term":"Greeting","definition":"A welcome"}],"plan":{"goal":"Greet world","scope":["Greeting"],"requirements":["Say hello to world"],"completion_checks":[{"id":"greeting","description":"Prints Hello, world!"}],"assumptions":[],"exclusions":[],"deferred":[],"sources":[]}})}.to_string();
        self.receipts
            .lock()
            .unwrap()
            .insert(id.into(), (input.into(), result.clone()));
        Ok(result)
    }
}

#[tokio::test]
async fn answers_restart_exact_approval_and_revision_are_durable() {
    use factory_runtime::control::Control;
    use std::{process::Command, sync::Arc};
    let data = tempfile::tempdir().unwrap();
    let root = tempfile::tempdir().unwrap();
    for args in [
        vec!["init", "-q"],
        vec!["config", "user.email", "test@example.com"],
        vec!["config", "user.name", "Test"],
    ] {
        assert!(
            Command::new("git")
                .args(args)
                .current_dir(root.path())
                .status()
                .unwrap()
                .success()
        );
    }
    std::fs::create_dir(root.path().join(".factory")).unwrap();
    std::fs::write(
        root.path().join(".factory/factory.yaml"),
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
    for args in [vec!["add", "."], vec!["commit", "-qm", "Initial"]] {
        assert!(
            Command::new("git")
                .args(args)
                .current_dir(root.path())
                .status()
                .unwrap()
                .success()
        );
    }
    let config = Config::new(data.path(), root.path()).unwrap();
    let worker = Arc::new(Interviewer::default());
    let runtime = Runtime::open_with_worker(
        config.clone(),
        worker.clone(),
        Arc::new(factory_runtime::NoFaults),
    )
    .await
    .unwrap();
    let control = Control::open(runtime.clone()).await.unwrap();
    let chosen=control.call("settings",json!({"session_id":"initiating-client","selection":{"shared":{"model":"selected-planner","reasoning_level":"high"}}})).await.unwrap();
    let start = control
        .call(
            "plan_start",
            json!({"request":"Greet","request_id":"journey","client_session_id":"initiating-client"}),
        )
        .await
        .unwrap();
    let id = start["session_id"].as_str().unwrap();
    let run = start["run_id"].as_str().unwrap();
    assert_eq!(
        runtime.resume(run).await.unwrap().status,
        factory_runtime::Status::WaitingForInput
    );
    let waiting = control
        .call("plan_status", json!({"session_id":id}))
        .await
        .unwrap();
    let answer = json!({"session_id":id,"interaction_id":waiting["pending"]["interaction_id"],"answers":{"recipient":"world"}});
    control.call("plan_answer", answer.clone()).await.unwrap();
    control.call("plan_answer", answer).await.unwrap();
    assert!(control.call("plan_answer",json!({"session_id":id,"interaction_id":waiting["pending"]["interaction_id"],"answers":{"recipient":"other"}})).await.is_err());
    let restarted = Runtime::open_with_worker(config, worker, Arc::new(factory_runtime::NoFaults))
        .await
        .unwrap();
    assert_eq!(
        restarted.resume(run).await.unwrap().status,
        factory_runtime::Status::Planned
    );
    let control = Control::open(restarted.clone()).await.unwrap();
    let planned = control
        .call("plan_status", json!({"session_id":id}))
        .await
        .unwrap();
    assert_eq!(planned["rounds"][0]["answers"]["recipient"], "world");
    assert!(
        control
            .call("plan_approve", json!({"session_id":id,"plan_hash":"stale"}))
            .await
            .is_err()
    );
    let approval = json!({"session_id":id,"plan_hash":planned["plan_hash"]});
    let (a, b) = tokio::join!(
        control.call_as("plan_approve", approval.clone(), "alice"),
        control.call_as("plan_approve", approval, "bob")
    );
    let a = a.unwrap();
    let b = b.unwrap();
    assert_eq!(a["implementation"]["run_id"], b["implementation"]["run_id"]);
    assert_eq!(restarted.list().await.unwrap().len(), 2);
    let implementation = restarted
        .status(a["implementation"]["run_id"].as_str().unwrap())
        .await
        .unwrap();
    let approved = implementation
        .input
        .implementation
        .unwrap()
        .approved_plan
        .unwrap();
    assert_eq!(approved.sha256, planned["plan_hash"]);
    assert_eq!(approved.completion_checks.len(), 3);
    std::fs::write(
        root.path().join(".factory/factory.yaml"),
        "invalid yaml for future sessions",
    )
    .unwrap();
    assert_eq!(
        control
            .call("settings", json!({"session_id":"initiating-client"}))
            .await
            .unwrap(),
        chosen
    );
    assert_eq!(implementation.input.planning, None);
    let revised = control
        .call(
            "plan_revise",
            json!({"session_id":id,"feedback":"Revisit recipient"}),
        )
        .await
        .unwrap();
    assert_eq!(revised["revision"], 2);
    let revised_run = restarted
        .status(revised["run_id"].as_str().unwrap())
        .await
        .unwrap();
    assert_eq!(
        revised_run
            .input
            .implementation
            .unwrap()
            .config
            .agent("plan")
            .model,
        "selected-planner"
    );

    assert_eq!(revised["history"][0]["plan_hash"], planned["plan_hash"]);
    assert!(
        control
            .call(
                "plan_approve",
                json!({"session_id":id,"plan_hash":planned["plan_hash"]})
            )
            .await
            .is_err()
    );
    assert_eq!(
        restarted
            .status(run)
            .await
            .unwrap()
            .workflow
            .unwrap()
            .planning
            .unwrap()
            .snapshot,
        Some(planned["snapshot"].clone())
    );
}

#[tokio::test]
async fn http_guards_and_polling_contract_are_preserved() {
    use axum::{
        body::Body,
        http::{Request, StatusCode},
    };
    use tower::ServiceExt;
    let data = tempfile::tempdir().unwrap();
    let workspace = tempfile::tempdir().unwrap();
    let runtime = Runtime::open(Config::new(data.path(), workspace.path()).unwrap())
        .await
        .unwrap();
    let control = factory_runtime::control::Control::open(runtime)
        .await
        .unwrap();
    let app = factory_runtime::http::router(control, "test-token".into(), "127.0.0.1:4317".into());
    let response = app
        .clone()
        .oneshot(
            Request::builder()
                .uri("/api/sessions")
                .header("host", "127.0.0.1:4317")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::UNAUTHORIZED);
    let response = app
        .clone()
        .oneshot(
            Request::builder()
                .uri("/api/sessions")
                .header("host", "evil.test")
                .header("x-factory-token", "test-token")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::MISDIRECTED_REQUEST);
    let response = app
        .clone()
        .oneshot(
            Request::builder()
                .uri("/api/sessions")
                .header("host", "127.0.0.1:4317")
                .header("x-factory-token", "test-token")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let bytes = axum::body::to_bytes(response.into_body(), 65536)
        .await
        .unwrap();
    let value: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(value["sessions"], json!([]));
    let response = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/api/control/plan_start")
                .header("host", "127.0.0.1:4317")
                .header("origin", "https://evil.test")
                .header("x-factory-token", "test-token")
                .header("content-type", "application/json")
                .body(Body::from("{}"))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::FORBIDDEN);
}

#[tokio::test]
async fn sse_reconnect_replays_then_observes_committed_live_events() {
    use axum::{body::Body, http::Request};
    use futures::StreamExt;
    use tower::ServiceExt;
    let data = tempfile::tempdir().unwrap();
    let workspace = tempfile::tempdir().unwrap();
    let runtime = Runtime::open(Config::new(data.path(), workspace.path()).unwrap())
        .await
        .unwrap();
    let run = runtime
        .start(factory_runtime::StartRequest::new(
            "events",
            "events-session",
            "Greeting",
        ))
        .await
        .unwrap();
    let control = factory_runtime::control::Control::open(runtime.clone())
        .await
        .unwrap();
    let app = factory_runtime::http::router(control, "test-token".into(), "127.0.0.1:4317".into());
    let connect = |cursor: &str| {
        Request::builder()
            .uri(format!("/api/runs/{}/events", run.run_id))
            .header("host", "127.0.0.1:4317")
            .header("authorization", "Bearer test-token")
            .header("last-event-id", cursor)
            .body(Body::empty())
            .unwrap()
    };
    let response = app.clone().oneshot(connect("0")).await.unwrap();
    assert_eq!(response.status(), 200);
    let mut stream = response.into_body().into_data_stream();
    let first = stream.next().await.unwrap().unwrap();
    assert!(String::from_utf8_lossy(&first).contains("id: 1"));
    runtime.resume(&run.run_id).await.unwrap();
    let live = tokio::time::timeout(std::time::Duration::from_secs(3), stream.next())
        .await
        .unwrap()
        .unwrap()
        .unwrap();
    assert!(String::from_utf8_lossy(&live).contains("id: 2"));
    drop(stream);
    let response = app.clone().oneshot(connect("1")).await.unwrap();
    let mut replay = response.into_body().into_data_stream();
    let event = replay.next().await.unwrap().unwrap();
    assert!(String::from_utf8_lossy(&event).contains("id: 2"));
    assert_eq!(
        runtime.status(&run.run_id).await.unwrap().status,
        factory_runtime::Status::WaitingForInput
    );
    assert_eq!(app.oneshot(connect("999")).await.unwrap().status(), 409);
}

#[tokio::test]
async fn python_snapshot_import_preserves_original_hash_and_is_repeatable() {
    let data = tempfile::tempdir().unwrap();
    // macOS temporary directories can be exposed through /var -> /private/var.
    // Exercise the same alias/canonical identity distinction on every platform.
    let workspace_root = tempfile::tempdir().unwrap();
    let aliases = tempfile::tempdir().unwrap();
    let workspace = aliases.path().join("workspace");
    std::os::unix::fs::symlink(workspace_root.path(), &workspace).unwrap();
    let runtime = Runtime::open(Config::new(data.path(), workspace.as_path()).unwrap())
        .await
        .unwrap();
    let control = factory_runtime::control::Control::open(runtime.clone())
        .await
        .unwrap();
    assert_ne!(workspace.as_path(), control.workspace());
    assert_eq!(
        control.workspace(),
        workspace_root.path().canonicalize().unwrap()
    );
    let mut archive: serde_json::Value =
        serde_json::from_str(include_str!("fixtures/python-planning.json")).unwrap();
    archive["workspace"] = json!(control.workspace());
    control.import_planning(archive.clone()).await.unwrap();
    let status = control
        .call("plan_status", json!({"session_id":"pln_saved"}))
        .await
        .unwrap();
    assert_eq!(status["status"], "planned");
    assert_eq!(
        status["plan_hash"],
        archive["sessions"][0]["revisions"][0]["plan_hash"]
    );
    let approved = control
        .call(
            "plan_approve",
            json!({"session_id":"pln_saved","plan_hash":status["plan_hash"]}),
        )
        .await
        .unwrap();
    let implementation = runtime
        .status(approved["implementation"]["run_id"].as_str().unwrap())
        .await
        .unwrap();
    assert_eq!(
        implementation
            .input
            .implementation
            .unwrap()
            .approved_plan
            .unwrap()
            .content,
        archive["sessions"][0]["revisions"][0]["snapshot_content"]
    );
    control.import_planning(archive.clone()).await.unwrap();
    assert_eq!(
        control
            .call("plan_status", json!({"session_id":"pln_saved"}))
            .await
            .unwrap()["implementation"]["run_id"],
        approved["implementation"]["run_id"]
    );
    archive["sessions"][0]["revisions"][0]["snapshot_content"] = json!("tampered");
    assert!(control.import_planning(archive).await.is_err());
    assert_eq!(runtime.list().await.unwrap().len(), 2);
}

#[tokio::test]
async fn lost_approval_response_recovers_the_saved_intent_without_dispatch_on_reads() {
    let data = tempfile::tempdir().unwrap();
    let workspace = tempfile::tempdir().unwrap();
    let config = Config::new(data.path(), workspace.path()).unwrap();
    let runtime = Runtime::open(config.clone()).await.unwrap();
    let control = factory_runtime::control::Control::open(runtime.clone())
        .await
        .unwrap();
    let mut archive: serde_json::Value =
        serde_json::from_str(include_str!("fixtures/python-planning.json")).unwrap();
    archive["workspace"] = json!(control.workspace());
    control.import_planning(archive).await.unwrap();
    let plan = control
        .call("plan_status", json!({"session_id":"pln_saved"}))
        .await
        .unwrap();
    let payload = json!({"session_id":"pln_saved","plan_hash":plan["plan_hash"]}).to_string();
    let output = std::process::Command::new(env!("CARGO_BIN_EXE_factory-runtime"))
        .args([
            data.path().to_str().unwrap(),
            workspace.path().to_str().unwrap(),
            "control",
            "plan_approve",
            &payload,
        ])
        .env("FACTORY_SLICE_CRASH_AT", "after_handoff_intent")
        .output()
        .unwrap();
    assert_eq!(
        output.status.code(),
        Some(86),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let saved = control
        .call("plan_status", json!({"session_id":"pln_saved"}))
        .await
        .unwrap();
    assert_eq!(
        saved["implementation"]["controller_state"],
        "handoff_pending"
    );
    assert_eq!(runtime.list().await.unwrap().len(), 1);
    control.recover().await.unwrap();
    let repeated = control
        .call("plan_approve", serde_json::from_str(&payload).unwrap())
        .await
        .unwrap();
    assert_eq!(
        repeated["implementation"]["run_id"],
        saved["implementation"]["run_id"]
    );
    assert_eq!(runtime.list().await.unwrap().len(), 2);
}

#[tokio::test]
async fn remote_browser_token_exchange_uses_secure_cookies_and_exact_https_origin() {
    use axum::{body::Body, http::Request};
    use tower::ServiceExt;
    let data = tempfile::tempdir().unwrap();
    let workspace = tempfile::tempdir().unwrap();
    let control = factory_runtime::control::Control::open(
        Runtime::open(Config::new(data.path(), workspace.path()).unwrap())
            .await
            .unwrap(),
    )
    .await
    .unwrap();
    let app = factory_runtime::http::router_for_origin(
        control,
        "test-token".into(),
        "factory.example".into(),
        "https://factory.example".into(),
    );
    let response = app
        .clone()
        .oneshot(
            Request::builder()
                .uri("/?token=test-token")
                .header("host", "factory.example")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), 303);
    assert!(
        response.headers()["set-cookie"]
            .to_str()
            .unwrap()
            .contains("; Secure")
    );
    let response = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/api/control/plan_start")
                .header("host", "factory.example")
                .header("origin", "http://factory.example")
                .header("x-factory-token", "test-token")
                .header("content-type", "application/json")
                .body(Body::from("{}"))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), 403);
}

#[tokio::test]
async fn importing_a_waiting_revision_retains_previous_settled_decisions() {
    let data = tempfile::tempdir().unwrap();
    let workspace = tempfile::tempdir().unwrap();
    let runtime = Runtime::open(Config::new(data.path(), workspace.path()).unwrap())
        .await
        .unwrap();
    let control = factory_runtime::control::Control::open(runtime.clone())
        .await
        .unwrap();
    let mut archive: serde_json::Value =
        serde_json::from_str(include_str!("fixtures/python-planning.json")).unwrap();
    archive["workspace"] = json!(control.workspace());
    let revision = &mut archive["sessions"][0]["revisions"][0];
    revision["status"] = json!("waiting_for_input");
    revision["snapshot"] = json!(null);
    revision["snapshot_content"] = json!(null);
    revision["plan_hash"] = json!(null);
    let previous = json!({"run_id":"earlier-revision","decisions":[{"id":"storage","decision":"SQLite"}],"glossary":[{"term":"Run","definition":"One saved workflow"}]});
    revision["source_record"]["planning"] = json!({"previous":previous});
    let questions = json!([{"id":"scope","question":"Keep scope?","options":["yes"]}]);
    revision["rounds"] = json!([{"interaction_id":"old:grill:1","questions":questions,"answers":null,"state":"waiting","envelope":{"version":1,"status":"questions","summary":"Revisit scope","questions":questions,"plan":null}}]);
    control.import_planning(archive).await.unwrap();
    let status = control
        .call("plan_status", json!({"session_id":"pln_saved"}))
        .await
        .unwrap();
    let run = runtime
        .status(status["run_id"].as_str().unwrap())
        .await
        .unwrap();
    assert_eq!(run.input.planning.unwrap().previous, Some(previous));
    assert_eq!(status["pending"]["questions"][0]["id"], "scope");
}

#[test]
fn public_proxy_requires_https_even_when_the_backend_binds_loopback() {
    use factory_runtime::http::public_origin;
    let local = "127.0.0.1:4317".parse().unwrap();
    assert!(public_origin(local, "factory.example", None).is_err());
    assert!(public_origin(local, "factory.example", Some("http://factory.example")).is_err());
    assert_eq!(
        public_origin(local, "factory.example", Some("https://factory.example")).unwrap(),
        "https://factory.example"
    );
    assert_eq!(
        public_origin(local, "localhost:4317", None).unwrap(),
        "http://localhost:4317"
    );
}
