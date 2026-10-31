use async_trait::async_trait;
use factory_runtime::{
    Config, NoFaults, Reconciliation, Result, Runtime, StartRequest, Status, Worker,
    workflow::{CheckResult, ExecutionKind, ExecutionRequest, ImplementationInput, WorkflowConfig},
};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    path::Path,
    process::Command,
    sync::{Arc, Mutex},
};
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
fn repository() -> tempfile::TempDir {
    let root = tempfile::tempdir().unwrap();
    git(root.path(), &["init", "-q"]);
    git(root.path(), &["config", "user.email", "test@example.com"]);
    git(root.path(), &["config", "user.name", "Test"]);
    std::fs::write(root.path().join("greeting.txt"), "Hello\n").unwrap();
    git(root.path(), &["add", "greeting.txt"]);
    git(root.path(), &["commit", "-qm", "Initial greeting"]);
    root
}
fn configuration() -> WorkflowConfig {
    WorkflowConfig::from_yaml(r#"
version: 2
workflow: simple-sdlc
review_mode: single
worker_connection: {runtime: codex, model_connection: native, codex_version: '0.154.0'}
agents:
  planner: {provider: codex, model: gpt-5.4, reasoning_level: high, permission_mode: auto}
  builder: {provider: codex, model: gpt-5.4, reasoning_level: high, permission_mode: accept-edits}
  reviewer: {provider: codex, model: gpt-5.4, reasoning_level: high, permission_mode: auto}
quality:
  commands:
    - {name: greeting, argv: [python3, -B, -c, "assert open('greeting.txt').read() == 'Hello, world!\\n'"], repeat_on_recovery: true}
"#).unwrap()
}
fn request(id: &str) -> StartRequest {
    let mut input = StartRequest::new(id, "session", "Change the greeting to Hello, world!");
    input.implementation = Some(ImplementationInput {
        config: configuration(),
        required_criteria: vec![],
        approved_plan: None,
    });
    input
}
#[derive(Default)]
struct Provider {
    behavior: &'static str,
    receipts: Mutex<BTreeMap<String, (String, String)>>,
    calls: Mutex<Vec<ExecutionRequest>>,
}
fn context(prompt: &str) -> Value {
    let data = prompt
        .split("<factory_data>\n")
        .nth(1)
        .unwrap()
        .split("\n</factory_data>")
        .next()
        .unwrap();
    serde_json::from_str(data).unwrap()
}
#[async_trait]
impl Worker for Provider {
    async fn reconcile(&self, operation: &str, input: &str) -> Result<Reconciliation> {
        match self.receipts.lock().unwrap().get(operation) {
            Some((saved, result)) if saved == input => {
                Ok(Reconciliation::Completed(result.clone()))
            }
            Some(_) => panic!("input changed during replay"),
            None => Ok(Reconciliation::Absent),
        }
    }
    async fn dispatch(&self, operation: &str, input: &str, _: CancellationToken) -> Result<String> {
        let request: ExecutionRequest = serde_json::from_str(input)?;
        self.calls.lock().unwrap().push(request.clone());
        let result = match &request.execution {
            ExecutionKind::Specialist { step, prompt, .. } if step == "plan" && ((self.behavior=="questions" || (self.behavior=="fix_questions" && context(prompt)["original_request"].as_str().is_some_and(|r|r.starts_with("Apply the selected PR review fixes")))) && context(prompt)["saved_answers"].as_object().is_none_or(|m|m.is_empty())) => json!({"version":2,"status":"needs_input","summary":"Need recipient","acceptance_criteria":[],"implementation_steps":[],"test_strategy":"Check exact greeting","questions":[{"id":"recipient","question":"Who should be greeted?","options":["world"]}]}).to_string(),
            ExecutionKind::Specialist { step, .. } if step == "plan" => json!({"version":2,"status":"ready","summary":"Update the greeting","acceptance_criteria":[{"id":"greeting","description":"The greeting is Hello, world!"}],"implementation_steps":["Update greeting.txt"],"test_strategy":"Check the exact greeting"}).to_string(),
            ExecutionKind::Specialist { step,prompt,.. } if ["build","repair","revision"].contains(&step.as_str()) => {
                if context(prompt)["original_request"].as_str().is_some_and(|r|r.starts_with("Apply the selected PR review fixes")) {
                    std::fs::write(Path::new(&request.workspace).join("review-test.txt"),"Verified greeting\n")?;
                    json!({"status":"completed","summary":"Added review verification","changed_files":["review-test.txt"]}).to_string()
                }else{
                let greeting=if self.behavior=="failure" || (self.behavior=="repair" && step=="build") {"BROKEN\n"}else if self.behavior=="no_change" {"Hello\n"}else {"Hello, world!\n"};
                std::fs::write(Path::new(&request.workspace).join("greeting.txt"),greeting)?;
                json!({"status":"completed","summary":"Updated greeting","changed_files":if self.behavior=="no_change" {json!([])}else{json!(["greeting.txt"])}}).to_string()
                }
            }
            ExecutionKind::Quality { command,content_version } => {
                let output = Command::new(&command.argv[0]).args(&command.argv[1..]).current_dir(&request.workspace).output()?;
                serde_json::to_string(&CheckResult { name: command.name.clone(), argv:command.argv.clone(), exit_code:output.status.code(), timed_out:false, cancelled:false, output_tail:String::from_utf8_lossy(&output.stderr).into(), content_version:content_version.clone(),comparison:String::new() })?
            }
            ExecutionKind::Specialist { step,prompt,.. } if step.ends_with("_review") => {
                let context=context(prompt);let bundle=&context["bundle"];let role=step.trim_end_matches("_review");
                json!({"version":1,"reviewer":role,"content_version":bundle["content_version"],"summary":"Focused review passed","findings":[],"criteria":if role=="request" {json!([{"criterion_id":"greeting","status":"satisfied","evidence":"Exact check passed","evidence_refs":[bundle["checks"].clone()]}])}else{json!([])}}).to_string()
            }
            ExecutionKind::Specialist { step,prompt,.. } => {
                let context = context(prompt); let bundle = &context["bundle"];
                let reject=self.behavior=="revision" && context["previous_findings"].as_object().is_none_or(|h|h.is_empty());
                let findings=if reject {json!([{"id":"greeting-detail","criterion_id":"greeting","category":"correctness","severity":"warning","title":"Greeting needs verification","evidence":"Required detail absent","evidence_refs":[bundle["checks"].clone()],"required_change":"Verify greeting","confidence":"high"}])}else{json!([])};
                let resolutions:Vec<_>=context["previous_findings"].as_object().map(|h|h.keys().map(|id|json!({"finding_id":id,"status":"fixed","evidence":"Current exact check verifies the fix","evidence_refs":[bundle["checks"].clone()]})).collect()).unwrap_or_default();
                json!({"version":2,"content_version":bundle["content_version"],"approved":!reject,"summary":"Verified greeting","findings":findings,"criteria":bundle["context"]["plan"]["acceptance_criteria"].as_array().unwrap().iter().map(|c|json!({"criterion_id":c["id"],"status":"satisfied","evidence":"Current check and source verify completion","evidence_refs":[bundle["checks"].clone()]})).collect::<Vec<_>>(),"resolutions":resolutions,"assessments":if step=="judge" {json!([])}else{Value::Null}}).to_string()
            }
        };
        assert!(
            self.receipts
                .lock()
                .unwrap()
                .insert(operation.into(), (input.into(), result.clone()))
                .is_none(),
            "duplicate coding request"
        );
        Ok(result)
    }
}
#[tokio::test]
async fn implementation_runs_checks_and_review_before_a_verified_handoff() {
    let data = tempfile::tempdir().unwrap();
    let root = repository();
    let provider = Arc::new(Provider::default());
    let runtime = Runtime::open_with_worker(
        Config::new(data.path(), root.path()).unwrap(),
        provider.clone(),
        Arc::new(NoFaults),
    )
    .await
    .unwrap();
    let run = runtime.start(request("greeting")).await.unwrap();
    let result = runtime.resume(&run.run_id).await.unwrap();
    assert_eq!(result.status, Status::ReadyForReview, "{:?}", result.error);
    let state = result.workflow.as_ref().unwrap();
    assert!(state.test_passed && state.review_approved);
    assert_eq!(state.test_attempt_count, 1);
    assert_eq!(
        state.handoff.as_ref().unwrap()["changed_files"],
        json!(["greeting.txt"])
    );
    assert_eq!(
        std::fs::read_to_string(root.path().join("greeting.txt")).unwrap(),
        "Hello, world!\n"
    );
    let calls = provider.calls.lock().unwrap().len();
    assert_eq!(runtime.resume(&run.run_id).await.unwrap(), result);
    assert_eq!(provider.calls.lock().unwrap().len(), calls);
}

struct CrashAfterBuild(std::sync::atomic::AtomicU32);
impl factory_runtime::Faults for CrashAfterBuild {
    fn hit(&self, point: factory_runtime::CrashPoint) {
        if point == factory_runtime::CrashPoint::AfterDispatch
            && self.0.fetch_sub(1, std::sync::atomic::Ordering::SeqCst) == 1
        {
            panic!("simulated controller death after provider completion");
        }
    }
}
#[tokio::test]
async fn completed_provider_operation_is_reconciled_after_controller_death() {
    let data = tempfile::tempdir().unwrap();
    let root = repository();
    let provider = Arc::new(Provider::default());
    let config = Config::new(data.path(), root.path()).unwrap();
    let runtime = Runtime::open_with_worker(
        config.clone(),
        provider.clone(),
        Arc::new(CrashAfterBuild(std::sync::atomic::AtomicU32::new(3))),
    )
    .await
    .unwrap();
    let run = runtime.start(request("crash")).await.unwrap();
    let id = run.run_id.clone();
    assert!(
        tokio::spawn(async move { runtime.resume(&id).await })
            .await
            .is_err()
    );
    let runtime = Runtime::open_with_worker(config, provider.clone(), Arc::new(NoFaults))
        .await
        .unwrap();
    let deadline = tokio::time::Instant::now() + std::time::Duration::from_secs(8);
    let result = loop {
        let result = runtime.resume(&run.run_id).await.unwrap();
        if result.status.is_terminal() {
            break result;
        }
        assert!(
            tokio::time::Instant::now() < deadline,
            "{:?}: {:?}",
            result.status,
            result.error
        );
        tokio::time::sleep(std::time::Duration::from_millis(100)).await;
    };
    assert_eq!(result.status, Status::ReadyForReview, "{:?}", result.error);
    let calls = provider.calls.lock().unwrap();
    assert_eq!(
        calls
            .iter()
            .filter(|c| matches!(&c.execution,ExecutionKind::Specialist {step,..} if step=="build"))
            .count(),
        1
    );
}

struct UnconfirmedCleanup(Provider);
#[async_trait]
impl Worker for UnconfirmedCleanup {
    async fn reconcile(&self, id: &str, input: &str) -> Result<Reconciliation> {
        self.0.reconcile(id, input).await
    }
    async fn dispatch(&self, id: &str, input: &str, cancel: CancellationToken) -> Result<String> {
        self.0.dispatch(id, input, cancel).await
    }
    async fn stop_operation(&self, _: &str, _: &str) -> Result<()> {
        Err(factory_runtime::Error::Worker(
            "daemon unavailable; cleanup uncertain".into(),
        ))
    }
}
#[tokio::test]
async fn uncertain_cleanup_keeps_recovery_and_excludes_other_runs() {
    let data = tempfile::tempdir().unwrap();
    let root = repository();
    let provider = Arc::new(UnconfirmedCleanup(Provider::default()));
    let config = Config::new(data.path(), root.path()).unwrap();
    let runtime = Runtime::open_with_worker(
        config.clone(),
        provider,
        Arc::new(CrashAfterBuild(std::sync::atomic::AtomicU32::new(3))),
    )
    .await
    .unwrap();
    let run = runtime.start(request("cleanup")).await.unwrap();
    let id = run.run_id.clone();
    assert!(
        tokio::spawn(async move { runtime.resume(&id).await })
            .await
            .is_err()
    );
    let runtime = Runtime::open_with_worker(
        config,
        Arc::new(UnconfirmedCleanup(Provider::default())),
        Arc::new(NoFaults),
    )
    .await
    .unwrap();
    let stopped = runtime.cancel(&run.run_id).await.unwrap();
    assert_eq!(stopped.status, Status::RecoveryNeeded);
    let other = runtime.start(request("other")).await.unwrap();
    assert!(matches!(
        runtime.resume(&other.run_id).await,
        Err(factory_runtime::Error::Busy)
    ));
    assert_eq!(
        std::fs::read_to_string(root.path().join("greeting.txt")).unwrap(),
        "Hello, world!\n"
    );
}

#[tokio::test]
async fn focused_mode_requires_request_code_security_and_an_independent_judge() {
    let data = tempfile::tempdir().unwrap();
    let root = repository();
    let provider = Arc::new(Provider::default());
    let runtime = Runtime::open_with_worker(
        Config::new(data.path(), root.path()).unwrap(),
        provider.clone(),
        Arc::new(NoFaults),
    )
    .await
    .unwrap();
    let mut input = request("focused");
    input.implementation.as_mut().unwrap().config.review_mode =
        factory_runtime::workflow::ReviewMode::Focused;
    let run = runtime.start(input).await.unwrap();
    let result = runtime.resume(&run.run_id).await.unwrap();
    assert_eq!(result.status, Status::ReadyForReview, "{:?}", result.error);
    let calls = provider.calls.lock().unwrap();
    let steps: Vec<_> = calls
        .iter()
        .filter_map(|c| match &c.execution {
            ExecutionKind::Specialist { step, .. } => Some(step.as_str()),
            _ => None,
        })
        .collect();
    assert_eq!(
        steps,
        vec![
            "plan",
            "build",
            "request_review",
            "code_review",
            "security_review",
            "judge"
        ]
    );
}

#[tokio::test]
async fn risk_mode_saves_review_selection_and_requests_documentation_for_text_changes() {
    let data = tempfile::tempdir().unwrap();
    let root = repository();
    let provider = Arc::new(Provider::default());
    let runtime = Runtime::open_with_worker(
        Config::new(data.path(), root.path()).unwrap(),
        provider.clone(),
        Arc::new(NoFaults),
    )
    .await
    .unwrap();
    let mut input = request("risk");
    input.implementation.as_mut().unwrap().config.review_mode =
        factory_runtime::workflow::ReviewMode::RiskBased;
    let run = runtime.start(input).await.unwrap();
    let result = runtime.resume(&run.run_id).await.unwrap();
    assert_eq!(result.status, Status::ReadyForReview, "{:?}", result.error);
    let selection = result
        .workflow
        .as_ref()
        .unwrap()
        .review_selection
        .as_ref()
        .expect("saved risk selection");
    assert_eq!(selection.level, "trivial");
    assert_eq!(
        selection.required_reviewers,
        vec!["request", "documentation"]
    );
    let calls = provider.calls.lock().unwrap();
    let steps: Vec<_> = calls
        .iter()
        .filter_map(|c| match &c.execution {
            ExecutionKind::Specialist { step, .. } => Some(step.as_str()),
            _ => None,
        })
        .collect();
    assert_eq!(
        steps,
        vec![
            "plan",
            "build",
            "request_review",
            "documentation_review",
            "judge"
        ]
    );
}

#[tokio::test]
async fn planner_questions_survive_restart_without_another_provider_request() {
    let data = tempfile::tempdir().unwrap();
    let root = repository();
    let provider = Arc::new(Provider {
        behavior: "questions",
        ..Provider::default()
    });
    let config = Config::new(data.path(), root.path()).unwrap();
    let runtime = Runtime::open_with_worker(config.clone(), provider.clone(), Arc::new(NoFaults))
        .await
        .unwrap();
    let run = runtime.start(request("questions")).await.unwrap();
    let waiting = runtime.resume(&run.run_id).await.unwrap();
    assert_eq!(
        waiting.status,
        Status::WaitingForInput,
        "{:?}",
        waiting.error
    );
    assert_eq!(
        waiting
            .workflow
            .as_ref()
            .unwrap()
            .interaction
            .as_ref()
            .unwrap()
            .questions[0]
            .id,
        "recipient"
    );
    let calls = provider.calls.lock().unwrap().len();
    drop(runtime);
    let runtime = Runtime::open_with_worker(config, provider.clone(), Arc::new(NoFaults))
        .await
        .unwrap();
    assert_eq!(runtime.resume(&run.run_id).await.unwrap(), waiting);
    assert_eq!(provider.calls.lock().unwrap().len(), calls);
}

#[tokio::test]
async fn answering_saved_planner_questions_resumes_the_same_run_and_rejects_conflicts() {
    let data = tempfile::tempdir().unwrap();
    let root = repository();
    let provider = Arc::new(Provider {
        behavior: "questions",
        ..Provider::default()
    });
    let runtime = Runtime::open_with_worker(
        Config::new(data.path(), root.path()).unwrap(),
        provider.clone(),
        Arc::new(NoFaults),
    )
    .await
    .unwrap();
    let run = runtime.start(request("answers")).await.unwrap();
    let waiting = runtime.resume(&run.run_id).await.unwrap();
    let question = waiting.workflow.unwrap().interaction.unwrap();
    let answers = BTreeMap::from([("recipient".into(), "world".into())]);
    let answered = runtime
        .answer_questions(&run.run_id, &question.interaction_id, answers.clone())
        .await
        .unwrap();
    assert_eq!(
        runtime
            .answer_questions(&run.run_id, &question.interaction_id, answers)
            .await
            .unwrap(),
        answered
    );
    assert!(
        runtime
            .answer_questions(
                &run.run_id,
                &question.interaction_id,
                BTreeMap::from([("recipient".into(), "different".into())])
            )
            .await
            .is_err()
    );
    let result = runtime.resume(&run.run_id).await.unwrap();
    assert_eq!(result.status, Status::ReadyForReview, "{:?}", result.error);
    let calls = provider.calls.lock().unwrap();
    assert_eq!(
        calls
            .iter()
            .filter(|c| matches!(&c.execution,ExecutionKind::Specialist {step,..} if step=="plan"))
            .count(),
        2
    );
}

#[tokio::test]
async fn new_check_failures_repair_with_a_saved_bound_and_exhaustion_fails() {
    for (behavior, status, repairs) in [
        ("repair", Status::ReadyForReview, 1),
        ("failure", Status::Failed, 2),
    ] {
        let data = tempfile::tempdir().unwrap();
        let root = repository();
        let provider = Arc::new(Provider {
            behavior,
            ..Provider::default()
        });
        let runtime = Runtime::open_with_worker(
            Config::new(data.path(), root.path()).unwrap(),
            provider.clone(),
            Arc::new(NoFaults),
        )
        .await
        .unwrap();
        let mut input = request(behavior);
        input
            .implementation
            .as_mut()
            .unwrap()
            .config
            .quality
            .commands[0]
            .argv = vec![
            "python3".into(),
            "-B".into(),
            "-c".into(),
            "assert 'BROKEN' not in open('greeting.txt').read()".into(),
        ];
        let run = runtime.start(input).await.unwrap();
        let result = runtime.resume(&run.run_id).await.unwrap();
        assert_eq!(result.status, status, "{:?}", result.error);
        assert_eq!(result.workflow.as_ref().unwrap().test_repair_count, repairs);
        assert_eq!(
            result.workflow.as_ref().unwrap().test_attempt_count,
            repairs + 1
        );
        let count = provider.calls.lock().unwrap().len();
        assert_eq!(runtime.resume(&run.run_id).await.unwrap(), result);
        assert_eq!(provider.calls.lock().unwrap().len(), count);
    }
}
#[tokio::test]
async fn rejected_reviews_revise_retest_and_resolve_the_same_finding() {
    let data = tempfile::tempdir().unwrap();
    let root = repository();
    let provider = Arc::new(Provider {
        behavior: "revision",
        ..Provider::default()
    });
    let runtime = Runtime::open_with_worker(
        Config::new(data.path(), root.path()).unwrap(),
        provider,
        Arc::new(NoFaults),
    )
    .await
    .unwrap();
    let run = runtime.start(request("revision")).await.unwrap();
    let result = runtime.resume(&run.run_id).await.unwrap();
    assert_eq!(result.status, Status::ReadyForReview, "{:?}", result.error);
    let state = result.workflow.unwrap();
    assert_eq!(state.review_revision_count, 1);
    assert_eq!(state.test_attempt_count, 2);
    assert_eq!(state.review_attempt_count, 2);
    assert_eq!(state.history["greeting-detail"].status, "fixed");
}
#[tokio::test]
async fn unchanged_baseline_failure_blocks_without_a_builder_repair() {
    let data = tempfile::tempdir().unwrap();
    let root = repository();
    let provider = Arc::new(Provider::default());
    let runtime = Runtime::open_with_worker(
        Config::new(data.path(), root.path()).unwrap(),
        provider.clone(),
        Arc::new(NoFaults),
    )
    .await
    .unwrap();
    let mut input = request("baseline");
    input
        .implementation
        .as_mut()
        .unwrap()
        .config
        .quality
        .commands[0]
        .argv = vec![
        "python3".into(),
        "-B".into(),
        "-c".into(),
        "raise SystemExit(1)".into(),
    ];
    let run = runtime.start(input).await.unwrap();
    let result = runtime.resume(&run.run_id).await.unwrap();
    assert_eq!(result.status, Status::Blocked);
    assert_eq!(result.workflow.as_ref().unwrap().test_repair_count, 0);
    assert_eq!(
        result.workflow.as_ref().unwrap().checks[0].comparison,
        "unchanged_baseline_failure"
    );
}
#[tokio::test]
async fn verified_no_change_is_valid_and_preserves_unrelated_user_work_on_a_new_start() {
    let data = tempfile::tempdir().unwrap();
    let root = repository();
    let provider = Arc::new(Provider {
        behavior: "no_change",
        ..Provider::default()
    });
    let runtime = Runtime::open_with_worker(
        Config::new(data.path(), root.path()).unwrap(),
        provider.clone(),
        Arc::new(NoFaults),
    )
    .await
    .unwrap();
    let mut input = request("no-change");
    input
        .implementation
        .as_mut()
        .unwrap()
        .config
        .quality
        .commands[0]
        .argv = vec![
        "python3".into(),
        "-B".into(),
        "-c".into(),
        "assert open('greeting.txt').read() == 'Hello\\n'".into(),
    ];
    let run = runtime.start(input).await.unwrap();
    let result = runtime.resume(&run.run_id).await.unwrap();
    assert_eq!(result.status, Status::ReadyForReview, "{:?}", result.error);
    assert_eq!(
        result.workflow.as_ref().unwrap().handoff.as_ref().unwrap()["changed_files"],
        json!([])
    );
    std::fs::write(root.path().join("user-work.txt"), "unfinished\n").unwrap();
    let calls = provider.calls.lock().unwrap().len();
    let run = runtime.start(request("dirty")).await.unwrap();
    assert_eq!(
        runtime.resume(&run.run_id).await.unwrap().status,
        Status::Blocked
    );
    assert_eq!(provider.calls.lock().unwrap().len(), calls);
    assert_eq!(
        std::fs::read_to_string(root.path().join("user-work.txt")).unwrap(),
        "unfinished\n"
    );
}

#[tokio::test]
async fn human_wait_pauses_work_budget_but_retains_the_saved_overall_expiry() {
    let data = tempfile::tempdir().unwrap();
    let root = repository();
    let provider = Arc::new(Provider {
        behavior: "questions",
        ..Provider::default()
    });
    let runtime = Runtime::open_with_worker(
        Config::new(data.path(), root.path()).unwrap(),
        provider,
        Arc::new(NoFaults),
    )
    .await
    .unwrap();
    let mut input = request("wait-time");
    input.timeout_seconds = 2;
    let run = runtime.start(input).await.unwrap();
    let waiting = runtime.resume(&run.run_id).await.unwrap();
    assert_eq!(waiting.status, Status::WaitingForInput);
    tokio::time::sleep(std::time::Duration::from_millis(2200)).await;
    assert_eq!(
        runtime.resume(&run.run_id).await.unwrap().status,
        Status::WaitingForInput
    );
    let id = &waiting
        .workflow
        .as_ref()
        .unwrap()
        .interaction
        .as_ref()
        .unwrap()
        .interaction_id;
    let answered = runtime
        .answer_questions(
            &run.run_id,
            id,
            BTreeMap::from([("recipient".into(), "world".into())]),
        )
        .await
        .unwrap();
    assert!(answered.deadline_at > chrono::Utc::now());
    assert_eq!(
        answered.workflow.as_ref().unwrap().overall_deadline_at,
        waiting.workflow.as_ref().unwrap().overall_deadline_at
    );
}

#[derive(Default)]
struct RemoteState {
    branches: BTreeMap<String, String>,
    pull: Option<factory_runtime::workflow::delivery::Pull>,
    creates: u32,
    pushes: u32,
    lose_pull_response: bool,
    suppress_pull: bool,
    checks_status: Option<String>,
    threads: Vec<factory_runtime::workflow::delivery::ReviewThread>,
    comments: Vec<Value>,
}
#[derive(Default)]
struct FakeGitHub(Mutex<RemoteState>);
#[async_trait]
impl factory_runtime::workflow::delivery::Remote for FakeGitHub {
    async fn branch_head(&self, _: &str, branch: &str) -> Result<Option<String>> {
        Ok(self.0.lock().unwrap().branches.get(branch).cloned())
    }
    async fn push(
        &self,
        _: &str,
        branch: &str,
        commit: &str,
        previous: Option<&str>,
    ) -> Result<()> {
        let mut state = self.0.lock().unwrap();
        assert_eq!(state.branches.get(branch).map(String::as_str), previous);
        state.pushes += 1;
        state.branches.insert(branch.into(), commit.into());
        if let Some(pull) = &mut state.pull
            && pull.branch == branch
        {
            pull.head = commit.into();
        }
        Ok(())
    }
    async fn find_pull(
        &self,
        _: &str,
        _: &str,
    ) -> Result<Option<factory_runtime::workflow::delivery::Pull>> {
        Ok(self.0.lock().unwrap().pull.clone())
    }
    async fn create_pull(
        &self,
        _: &str,
        branch: &str,
        base: &str,
        title: &str,
        body: &str,
    ) -> Result<()> {
        let mut state = self.0.lock().unwrap();
        state.creates += 1;
        if state.suppress_pull {
            return Err(factory_runtime::Error::Worker(
                "uncertain PR creation".into(),
            ));
        }
        state.pull = Some(factory_runtime::workflow::delivery::Pull {
            number: 42,
            url: "https://github.com/test/fixture/pull/42".into(),
            branch: branch.into(),
            base: base.into(),
            head: state.branches[branch].clone(),
            draft: false,
            state: "open".into(),
            body: body.into(),
            title: title.into(),
        });
        if state.lose_pull_response {
            state.lose_pull_response = false;
            return Err(factory_runtime::Error::Worker(
                "PR creation response lost".into(),
            ));
        }
        Ok(())
    }
    async fn pull(&self, _: &str, _: u64) -> Result<factory_runtime::workflow::delivery::Pull> {
        Ok(self.0.lock().unwrap().pull.clone().unwrap())
    }
    async fn checks(
        &self,
        _: &str,
        commit: &str,
    ) -> Result<factory_runtime::workflow::delivery::Checks> {
        Ok(factory_runtime::workflow::delivery::Checks {
            commit: commit.into(),
            status: self
                .0
                .lock()
                .unwrap()
                .checks_status
                .clone()
                .unwrap_or_else(|| "passed".into()),
            checks: vec![json!({"name":"behavior","status":"passed"})],
        })
    }
    async fn review_threads(
        &self,
        _: &str,
        _: u64,
    ) -> Result<Vec<factory_runtime::workflow::delivery::ReviewThread>> {
        Ok(self.0.lock().unwrap().threads.clone())
    }
    async fn reply(
        &self,
        _: &str,
        _: u64,
        thread: &factory_runtime::workflow::delivery::ReviewThread,
        body: &str,
    ) -> Result<()> {
        let mut state = self.0.lock().unwrap();
        state
            .threads
            .iter_mut()
            .find(|t| t.id == thread.id)
            .unwrap()
            .comments
            .push(json!({"id":"reply-1","body":body}));
        Ok(())
    }
    async fn resolve(&self, id: &str) -> Result<()> {
        self.0
            .lock()
            .unwrap()
            .threads
            .iter_mut()
            .find(|t| t.id == id)
            .unwrap()
            .resolved = true;
        Ok(())
    }
    async fn comments(&self, _: &str, _: u64) -> Result<Vec<Value>> {
        Ok(self.0.lock().unwrap().comments.clone())
    }
    async fn comment(&self, _: &str, _: u64, body: &str) -> Result<()> {
        self.0
            .lock()
            .unwrap()
            .comments
            .push(json!({"id":"summary","body":body}));
        Ok(())
    }
}
fn authorization() -> factory_runtime::workflow::DeliveryPolicy {
    factory_runtime::workflow::DeliveryPolicy {
        mode: "pull_request".into(),
        repository: Some("test/fixture".into()),
        title: Some("Update the greeting".into()),
        issue: Some(17),
        ..Default::default()
    }
}
#[tokio::test]
async fn authorized_delivery_commits_exact_tested_bytes_and_reconciles_publication() {
    let data = tempfile::tempdir().unwrap();
    let root = repository();
    let runtime = Runtime::open_with_worker(
        Config::new(data.path(), root.path()).unwrap(),
        Arc::new(Provider::default()),
        Arc::new(NoFaults),
    )
    .await
    .unwrap();
    let run = runtime.start(request("delivery")).await.unwrap();
    let ready = runtime.resume(&run.run_id).await.unwrap();
    assert_eq!(ready.status, Status::ReadyForReview);
    let head = String::from_utf8(
        Command::new("git")
            .args(["rev-parse", "HEAD"])
            .current_dir(root.path())
            .output()
            .unwrap()
            .stdout,
    )
    .unwrap()
    .trim()
    .to_owned();
    let github = FakeGitHub::default();
    github
        .0
        .lock()
        .unwrap()
        .branches
        .insert("main".into(), head.clone());
    let delivered = runtime
        .deliver_with_remote(&run.run_id, &github, Some(authorization()))
        .await
        .unwrap();
    let delivery = &delivered.workflow.as_ref().unwrap().delivery;
    assert_eq!(delivery["status"], "delivered", "{delivery}");
    assert_eq!(delivery["pr_number"], 42);
    assert_eq!(delivered.status, Status::ReadyForReview);
    assert_eq!(
        std::fs::read_to_string(root.path().join("greeting.txt")).unwrap(),
        "Hello, world!\n"
    );
    let installed = String::from_utf8(
        Command::new("git")
            .args(["rev-parse", "HEAD"])
            .current_dir(root.path())
            .output()
            .unwrap()
            .stdout,
    )
    .unwrap()
    .trim()
    .to_owned();
    assert_ne!(installed, head);
    assert_eq!(delivery["commit"], installed);
    let repeated = runtime
        .deliver_with_remote(&run.run_id, &github, None)
        .await
        .unwrap();
    assert_eq!(
        repeated.workflow.as_ref().unwrap().delivery["status"],
        "delivered"
    );
    assert_eq!(github.0.lock().unwrap().creates, 1);
    assert_eq!(github.0.lock().unwrap().pushes, 1);
}

#[tokio::test]
async fn authorized_selected_review_fixes_keep_product_criteria_and_resolve_only_selected_threads()
{
    let data = tempfile::tempdir().unwrap();
    let root = repository();
    let provider = Arc::new(Provider::default());
    let github = Arc::new(FakeGitHub::default());
    let runtime = Runtime::open_with_worker(
        Config::new(data.path(), root.path()).unwrap(),
        provider.clone(),
        Arc::new(NoFaults),
    )
    .await
    .unwrap()
    .with_delivery_remote(github.clone());
    let run = runtime.start(request("review-fixes")).await.unwrap();
    runtime.resume(&run.run_id).await.unwrap();
    let head = String::from_utf8(
        Command::new("git")
            .args(["rev-parse", "HEAD"])
            .current_dir(root.path())
            .output()
            .unwrap()
            .stdout,
    )
    .unwrap()
    .trim()
    .to_owned();
    github
        .0
        .lock()
        .unwrap()
        .branches
        .insert("main".into(), head);
    let mut policy = authorization();
    policy.review_fixes = true;
    let delivered = runtime
        .deliver_with_remote(&run.run_id, github.as_ref(), Some(policy))
        .await
        .unwrap();
    assert_eq!(delivered.workflow.unwrap().delivery["status"], "delivered");
    for id in ["selected", "unselected"] {
        github
            .0
            .lock()
            .unwrap()
            .threads
            .push(factory_runtime::workflow::delivery::ReviewThread {
                id: id.into(),
                resolved: false,
                outdated: false,
                path: "greeting.txt".into(),
                line: Some(1),
                body: "Add verification of the greeting".into(),
                comment_id: 1,
                commit: None,
                comments: vec![
                    json!({"id":format!("{id}-comment"),"body":"Add verification of the greeting"}),
                ],
            });
    }
    github.0.lock().unwrap().checks_status = Some("pending".into());
    let fixed = runtime
        .fix_reviews_with_remote(&run.run_id, github.as_ref(), &["selected".into()])
        .await
        .unwrap();
    let delivery = &fixed.workflow.as_ref().unwrap().delivery;
    assert_eq!(delivery["status"], "ci_pending", "{delivery}");
    assert_eq!(delivery["review_fix"]["status"], "completed");
    let child = runtime
        .status(delivery["verified_run_id"].as_str().unwrap())
        .await
        .unwrap();
    let criteria = &child
        .workflow
        .as_ref()
        .unwrap()
        .plan
        .as_ref()
        .unwrap()
        .acceptance_criteria;
    assert_eq!(
        criteria.iter().map(|c| c.id.as_str()).collect::<Vec<_>>(),
        vec!["greeting", "pr-fix-001"]
    );
    {
        let state = github.0.lock().unwrap();
        assert!(
            state
                .threads
                .iter()
                .find(|t| t.id == "selected")
                .unwrap()
                .resolved
        );
        assert!(
            !state
                .threads
                .iter()
                .find(|t| t.id == "unselected")
                .unwrap()
                .resolved
        );
        assert_eq!(state.comments.len(), 1);
        assert_eq!(state.pushes, 2);
    }
    let commit = delivery["commit"].clone();
    let pending = runtime.resume(&run.run_id).await.unwrap();
    assert_eq!(pending.workflow.unwrap().delivery["status"], "ci_pending");
    github.0.lock().unwrap().checks_status = Some("passed".into());
    let refreshed = runtime.resume(&run.run_id).await.unwrap();
    let refreshed = &refreshed.workflow.as_ref().unwrap().delivery;
    assert_eq!(refreshed["status"], "delivered", "{refreshed}");
    assert_eq!(refreshed["commit"], commit);
    assert_eq!(github.0.lock().unwrap().pushes, 2);
    assert_eq!(github.0.lock().unwrap().creates, 1);
    let calls = provider.calls.lock().unwrap().len();
    runtime
        .fix_reviews_with_remote(&run.run_id, github.as_ref(), &["selected".into()])
        .await
        .unwrap();
    assert_eq!(provider.calls.lock().unwrap().len(), calls);
}

#[tokio::test]
async fn uncertain_publication_reconciles_existing_results_and_never_repeats_unknown_creations() {
    for visible in [true, false] {
        let data = tempfile::tempdir().unwrap();
        let root = repository();
        let runtime = Runtime::open_with_worker(
            Config::new(data.path(), root.path()).unwrap(),
            Arc::new(Provider::default()),
            Arc::new(NoFaults),
        )
        .await
        .unwrap();
        let run = runtime.start(request("uncertain-publish")).await.unwrap();
        runtime.resume(&run.run_id).await.unwrap();
        let head = String::from_utf8(
            Command::new("git")
                .args(["rev-parse", "HEAD"])
                .current_dir(root.path())
                .output()
                .unwrap()
                .stdout,
        )
        .unwrap()
        .trim()
        .to_owned();
        let github = FakeGitHub::default();
        {
            let mut state = github.0.lock().unwrap();
            state.branches.insert("main".into(), head);
            state.lose_pull_response = visible;
            state.suppress_pull = !visible;
        }
        let uncertain = runtime
            .deliver_with_remote(&run.run_id, &github, Some(authorization()))
            .await
            .unwrap();
        assert_eq!(
            uncertain.workflow.as_ref().unwrap().delivery["status"],
            "recovery_needed"
        );
        let recovered = runtime
            .deliver_with_remote(&run.run_id, &github, None)
            .await
            .unwrap();
        assert_eq!(
            recovered.workflow.as_ref().unwrap().delivery["status"],
            if visible {
                "delivered"
            } else {
                "recovery_needed"
            }
        );
        assert_eq!(github.0.lock().unwrap().creates, 1);
        assert_eq!(github.0.lock().unwrap().pushes, 1);
        if !visible {
            let other = runtime.start(request("foreign-writer")).await.unwrap();
            assert!(matches!(
                runtime.resume(&other.run_id).await,
                Err(factory_runtime::Error::Busy)
            ));
        }
    }
}
#[tokio::test]
async fn changed_user_files_invalidate_publication_and_are_preserved() {
    let data = tempfile::tempdir().unwrap();
    let root = repository();
    let runtime = Runtime::open_with_worker(
        Config::new(data.path(), root.path()).unwrap(),
        Arc::new(Provider::default()),
        Arc::new(NoFaults),
    )
    .await
    .unwrap();
    let run = runtime.start(request("stale-publish")).await.unwrap();
    runtime.resume(&run.run_id).await.unwrap();
    std::fs::write(root.path().join("user-work.txt"), "unfinished\n").unwrap();
    let github = FakeGitHub::default();
    let result = runtime
        .deliver_with_remote(&run.run_id, &github, Some(authorization()))
        .await
        .unwrap();
    let state = result.workflow.as_ref().unwrap();
    assert_eq!(state.delivery["status"], "blocked");
    assert!(!state.test_passed && !state.review_approved);
    assert_eq!(github.0.lock().unwrap().pushes, 0);
    assert_eq!(
        std::fs::read_to_string(root.path().join("user-work.txt")).unwrap(),
        "unfinished\n"
    );
}

#[tokio::test]
async fn saved_pull_request_policy_delivers_during_normal_execution() {
    let data = tempfile::tempdir().unwrap();
    let root = repository();
    let github = Arc::new(FakeGitHub::default());
    let head = String::from_utf8(
        Command::new("git")
            .args(["rev-parse", "HEAD"])
            .current_dir(root.path())
            .output()
            .unwrap()
            .stdout,
    )
    .unwrap()
    .trim()
    .to_owned();
    github
        .0
        .lock()
        .unwrap()
        .branches
        .insert("main".into(), head);
    let runtime = Runtime::open_with_worker(
        Config::new(data.path(), root.path()).unwrap(),
        Arc::new(Provider::default()),
        Arc::new(NoFaults),
    )
    .await
    .unwrap()
    .with_delivery_remote(github.clone());
    let mut input = request("auto-delivery");
    input.implementation.as_mut().unwrap().config.delivery = authorization();
    let run = runtime.start(input).await.unwrap();
    let result = runtime.resume(&run.run_id).await.unwrap();
    assert_eq!(result.status, Status::ReadyForReview);
    assert_eq!(
        result.workflow.as_ref().unwrap().delivery["status"],
        "delivered",
        "{:?}",
        result.workflow.as_ref().unwrap().delivery
    );
    assert_eq!(github.0.lock().unwrap().creates, 1);
    assert_eq!(github.0.lock().unwrap().pushes, 1);
}
#[tokio::test]
async fn explicit_resume_refreshes_failed_or_unavailable_ci_without_republishing() {
    for status in ["failed", "unavailable"] {
        let data = tempfile::tempdir().unwrap();
        let root = repository();
        let github = Arc::new(FakeGitHub::default());
        let head = String::from_utf8(
            Command::new("git")
                .args(["rev-parse", "HEAD"])
                .current_dir(root.path())
                .output()
                .unwrap()
                .stdout,
        )
        .unwrap();
        {
            let mut remote = github.0.lock().unwrap();
            remote.branches.insert("main".into(), head.trim().into());
            remote.checks_status = Some(status.into());
        }
        let runtime = Runtime::open_with_worker(
            Config::new(data.path(), root.path()).unwrap(),
            Arc::new(Provider::default()),
            Arc::new(NoFaults),
        )
        .await
        .unwrap()
        .with_delivery_remote(github.clone());
        let mut input = request("ci-refresh");
        input.implementation.as_mut().unwrap().config.delivery = authorization();
        let run = runtime.start(input).await.unwrap();
        let initial = runtime.resume(&run.run_id).await.unwrap();
        assert_eq!(
            initial.workflow.as_ref().unwrap().delivery["status"],
            format!("ci_{status}")
        );
        assert!(!initial.delivery_pending());
        github.0.lock().unwrap().checks_status = Some("passed".into());
        let refreshed = runtime.resume(&run.run_id).await.unwrap();
        assert_eq!(refreshed.workflow.unwrap().delivery["status"], "delivered");
        assert_eq!(github.0.lock().unwrap().pushes, 1);
        assert_eq!(github.0.lock().unwrap().creates, 1);
    }
}

#[tokio::test]
async fn cancelled_publication_reconciles_a_lost_response_without_further_mutation() {
    let data = tempfile::tempdir().unwrap();
    let root = repository();
    let github = Arc::new(FakeGitHub::default());
    let runtime = Runtime::open_with_worker(
        Config::new(data.path(), root.path()).unwrap(),
        Arc::new(Provider::default()),
        Arc::new(NoFaults),
    )
    .await
    .unwrap()
    .with_delivery_remote(github.clone());
    let run = runtime.start(request("cancel-reconcile")).await.unwrap();
    runtime.resume(&run.run_id).await.unwrap();
    let head = String::from_utf8(
        Command::new("git")
            .args(["rev-parse", "HEAD"])
            .current_dir(root.path())
            .output()
            .unwrap()
            .stdout,
    )
    .unwrap()
    .trim()
    .to_owned();
    {
        let mut state = github.0.lock().unwrap();
        state.branches.insert("main".into(), head);
        state.lose_pull_response = true;
    }
    let uncertain = runtime
        .deliver(&run.run_id, Some(authorization()))
        .await
        .unwrap();
    assert_eq!(
        uncertain.workflow.as_ref().unwrap().delivery["status"],
        "recovery_needed"
    );
    runtime.cancel_delivery(&run.run_id).await.unwrap();
    let reconciled = runtime.resume(&run.run_id).await.unwrap();
    assert_eq!(
        reconciled.workflow.as_ref().unwrap().delivery["status"],
        "cancelled",
        "{:?}",
        reconciled.workflow.as_ref().unwrap().delivery
    );
    assert_eq!(github.0.lock().unwrap().creates, 1);
    assert_eq!(github.0.lock().unwrap().pushes, 1);
    let other = runtime
        .start(request("after-cancelled-publish"))
        .await
        .unwrap();
    assert!(runtime.resume(&other.run_id).await.is_ok());
}
struct PendingPush {
    inner: FakeGitHub,
    entered: tokio::sync::Notify,
}
#[async_trait]
impl factory_runtime::workflow::delivery::Remote for PendingPush {
    async fn branch_head(&self, repo: &str, branch: &str) -> Result<Option<String>> {
        self.inner.branch_head(repo, branch).await
    }
    async fn push(&self, _: &str, _: &str, _: &str, _: Option<&str>) -> Result<()> {
        self.entered.notify_one();
        std::future::pending().await
    }
    async fn find_pull(
        &self,
        _: &str,
        _: &str,
    ) -> Result<Option<factory_runtime::workflow::delivery::Pull>> {
        Ok(None)
    }
    async fn create_pull(&self, _: &str, _: &str, _: &str, _: &str, _: &str) -> Result<()> {
        panic!("cancelled publication must not create a PR")
    }
    async fn pull(&self, _: &str, _: u64) -> Result<factory_runtime::workflow::delivery::Pull> {
        panic!("unexpected PR lookup")
    }
    async fn checks(
        &self,
        _: &str,
        _: &str,
    ) -> Result<factory_runtime::workflow::delivery::Checks> {
        panic!("unexpected CI lookup")
    }
}
#[tokio::test]
async fn cancellation_interrupts_an_active_publication_and_retains_uncertainty() {
    let data = tempfile::tempdir().unwrap();
    let root = repository();
    let remote = Arc::new(PendingPush {
        inner: FakeGitHub::default(),
        entered: tokio::sync::Notify::new(),
    });
    let runtime = Runtime::open_with_worker(
        Config::new(data.path(), root.path()).unwrap(),
        Arc::new(Provider::default()),
        Arc::new(NoFaults),
    )
    .await
    .unwrap()
    .with_delivery_remote(remote.clone());
    let run = runtime.start(request("pending-push")).await.unwrap();
    runtime.resume(&run.run_id).await.unwrap();
    let head = String::from_utf8(
        Command::new("git")
            .args(["rev-parse", "HEAD"])
            .current_dir(root.path())
            .output()
            .unwrap()
            .stdout,
    )
    .unwrap()
    .trim()
    .to_owned();
    remote
        .inner
        .0
        .lock()
        .unwrap()
        .branches
        .insert("main".into(), head);
    let active_runtime = runtime.clone();
    let id = run.run_id.clone();
    let active = tokio::spawn(async move {
        active_runtime
            .deliver(&id, Some(authorization()))
            .await
            .unwrap()
    });
    tokio::time::timeout(std::time::Duration::from_secs(5), remote.entered.notified())
        .await
        .unwrap();
    runtime.cancel_delivery(&run.run_id).await.unwrap();
    let result = tokio::time::timeout(std::time::Duration::from_secs(2), active)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(
        result.workflow.as_ref().unwrap().delivery["status"],
        "recovery_needed"
    );
    let other = runtime
        .start(request("cannot-overlap-publish"))
        .await
        .unwrap();
    assert!(matches!(
        runtime.resume(&other.run_id).await,
        Err(factory_runtime::Error::Busy)
    ));
}
#[tokio::test]
async fn unreferenced_git_objects_left_by_interrupted_preparation_do_not_block_delivery() {
    let data = tempfile::tempdir().unwrap();
    let root = repository();
    let runtime = Runtime::open_with_worker(
        Config::new(data.path(), root.path()).unwrap(),
        Arc::new(Provider::default()),
        Arc::new(NoFaults),
    )
    .await
    .unwrap();
    let run = runtime.start(request("orphan-commit")).await.unwrap();
    runtime.resume(&run.run_id).await.unwrap();
    let head = String::from_utf8(
        Command::new("git")
            .args(["rev-parse", "HEAD"])
            .current_dir(root.path())
            .output()
            .unwrap()
            .stdout,
    )
    .unwrap()
    .trim()
    .to_owned();
    let orphan = Command::new("git")
        .args([
            "commit-tree",
            "HEAD^{tree}",
            "-p",
            "HEAD",
            "-m",
            "Interrupted preparation",
        ])
        .current_dir(root.path())
        .output()
        .unwrap();
    assert!(orphan.status.success());
    let remote = FakeGitHub::default();
    remote
        .0
        .lock()
        .unwrap()
        .branches
        .insert("main".into(), head);
    let delivered = runtime
        .deliver_with_remote(&run.run_id, &remote, Some(authorization()))
        .await
        .unwrap();
    assert_eq!(
        delivered.workflow.as_ref().unwrap().delivery["status"],
        "delivered",
        "{:?}",
        delivered.workflow.as_ref().unwrap().delivery
    );
    let tree = Command::new("git")
        .args(["show", "HEAD:greeting.txt"])
        .current_dir(root.path())
        .output()
        .unwrap();
    assert_eq!(tree.stdout, b"Hello, world!\n");
    git(
        root.path(),
        &[
            "cat-file",
            "-e",
            String::from_utf8_lossy(&orphan.stdout).trim(),
        ],
    );
}

#[tokio::test]
async fn review_fix_waiting_claim_survives_other_selections_and_parent_delivery() {
    let data = tempfile::tempdir().unwrap();
    let root = repository();
    let provider = Arc::new(Provider {
        behavior: "fix_questions",
        ..Provider::default()
    });
    let remote = Arc::new(FakeGitHub::default());
    let runtime = Runtime::open_with_worker(
        Config::new(data.path(), root.path()).unwrap(),
        provider,
        Arc::new(NoFaults),
    )
    .await
    .unwrap()
    .with_delivery_remote(remote.clone());
    let run = runtime.start(request("fix-claim")).await.unwrap();
    runtime.resume(&run.run_id).await.unwrap();
    let head = String::from_utf8(
        Command::new("git")
            .args(["rev-parse", "HEAD"])
            .current_dir(root.path())
            .output()
            .unwrap()
            .stdout,
    )
    .unwrap()
    .trim()
    .to_owned();
    remote
        .0
        .lock()
        .unwrap()
        .branches
        .insert("main".into(), head);
    let mut policy = authorization();
    policy.review_fixes = true;
    runtime.deliver(&run.run_id, Some(policy)).await.unwrap();
    remote
        .0
        .lock()
        .unwrap()
        .threads
        .push(factory_runtime::workflow::delivery::ReviewThread {
            id: "selected".into(),
            resolved: false,
            outdated: false,
            path: "greeting.txt".into(),
            line: Some(1),
            body: "Add verification".into(),
            comment_id: 1,
            commit: None,
            comments: vec![json!({"id":"comment","body":"Add verification"})],
        });
    let waiting = runtime
        .fix_reviews(&run.run_id, &["selected".into()])
        .await
        .unwrap();
    let batch = &waiting.workflow.as_ref().unwrap().delivery["review_fix"];
    let child_id = batch["run_id"].as_str().unwrap().to_owned();
    let child = runtime.status(&child_id).await.unwrap();
    assert_eq!(child.status, Status::WaitingForInput);
    let bound = tokio::time::Instant::now() + std::time::Duration::from_secs(5);
    loop {
        match runtime
            .fix_reviews(&run.run_id, &["different".into()])
            .await
        {
            Err(factory_runtime::Error::Busy) => {
                assert!(tokio::time::Instant::now() < bound);
                tokio::time::sleep(std::time::Duration::from_millis(2)).await;
            }
            Err(factory_runtime::Error::Config(message)) => {
                assert!(message.contains("finish the saved fix batch"));
                break;
            }
            other => panic!("unexpected selection result: {other:?}"),
        }
    }
    let refreshed = loop {
        match runtime.deliver(&run.run_id, None).await {
            Err(factory_runtime::Error::Busy) => {
                assert!(tokio::time::Instant::now() < bound);
                tokio::time::sleep(std::time::Duration::from_millis(2)).await;
            }
            result => break result.unwrap(),
        }
    };
    assert_eq!(
        refreshed.workflow.as_ref().unwrap().delivery["status"],
        "fixes_incomplete"
    );
    let foreign = runtime.start(request("other-fix-writer")).await.unwrap();
    assert!(matches!(
        runtime.resume(&foreign.run_id).await,
        Err(factory_runtime::Error::Busy)
    ));
    let interaction = child
        .workflow
        .as_ref()
        .unwrap()
        .interaction
        .as_ref()
        .unwrap();
    runtime
        .answer_questions(
            &child_id,
            &interaction.interaction_id,
            BTreeMap::from([("recipient".into(), "world".into())]),
        )
        .await
        .unwrap();
    let result = loop {
        let result = runtime.resume(&run.run_id).await.unwrap();
        if result.workflow.as_ref().unwrap().delivery["status"] == "delivered" {
            break result;
        }
        assert!(
            tokio::time::Instant::now() < bound,
            "{:?}",
            result.workflow.as_ref().unwrap().delivery
        );
        tokio::time::sleep(std::time::Duration::from_millis(10)).await;
    };
    assert_eq!(
        result.workflow.as_ref().unwrap().delivery["status"],
        "delivered",
        "{:?}",
        result.workflow.as_ref().unwrap().delivery
    );
}
#[tokio::test]
async fn exhausted_adk_recovery_keeps_ownership_when_final_worker_cleanup_is_uncertain() {
    use adk_core::DurableRunStore;
    let data = tempfile::tempdir().unwrap();
    let root = repository();
    let provider = Arc::new(UnconfirmedCleanup(Provider::default()));
    let config = Config::new(data.path(), root.path()).unwrap();
    let runtime = Runtime::open_with_worker(
        config.clone(),
        provider.clone(),
        Arc::new(CrashAfterBuild(std::sync::atomic::AtomicU32::new(3))),
    )
    .await
    .unwrap();
    let run = runtime.start(request("last-attempt")).await.unwrap();
    // Public ADK leases simulate fifteen controller deaths before application
    // dispatch; the final controller then dies after a real worker result.
    let pool = sqlx::sqlite::SqlitePoolOptions::new()
        .connect_with(
            sqlx::sqlite::SqliteConnectOptions::new()
                .filename(data.path().join("factory-rust.sqlite")),
        )
        .await
        .unwrap();
    let queue = adk_session::SqliteRunStore::from_pool(pool);
    for expected in 1..=15 {
        let bound = tokio::time::Instant::now() + std::time::Duration::from_secs(3);
        loop {
            if let Some(claim) = queue
                .claim(
                    &run.run_id,
                    "crashed-controller",
                    std::time::Duration::from_millis(1),
                )
                .await
                .unwrap()
            {
                assert_eq!(claim.record.attempt, expected);
                break;
            }
            assert!(tokio::time::Instant::now() < bound);
            tokio::time::sleep(std::time::Duration::from_millis(2)).await;
        }
    }
    tokio::time::sleep(std::time::Duration::from_millis(5)).await;
    let id = run.run_id.clone();
    assert!(
        tokio::spawn(async move { runtime.resume(&id).await })
            .await
            .is_err()
    );
    let runtime = Runtime::open_with_worker(config, provider, Arc::new(NoFaults))
        .await
        .unwrap();
    let bound = tokio::time::Instant::now() + std::time::Duration::from_secs(8);
    let recovered = loop {
        let result = runtime.resume(&run.run_id).await.unwrap();
        if result.status == Status::RecoveryNeeded {
            break result;
        }
        assert!(
            tokio::time::Instant::now() < bound,
            "{:?}: {:?}",
            result.status,
            result.error
        );
        tokio::time::sleep(std::time::Duration::from_millis(100)).await;
    };
    assert_eq!(recovered.attempts, 16);
    let other = runtime.start(request("after-exhaustion")).await.unwrap();
    assert!(matches!(
        runtime.resume(&other.run_id).await,
        Err(factory_runtime::Error::Busy)
    ));
}
