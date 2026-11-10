//! Controller-only Docker broker. No daemon socket, controller database, receipts,
//! or host PID namespace is exposed to specialist/quality containers.
use super::{
    CheckResult, ExecutionKind, ExecutionRequest,
    artifacts::{atomic, regular},
    repository,
};
use crate::{Config, Error, Reconciliation, Result, Worker, runtime::digest};
use async_trait::async_trait;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{
    collections::VecDeque,
    fs,
    path::{Path, PathBuf},
    process::Stdio,
    time::Duration,
};
use tokio::{
    io::{AsyncBufReadExt, AsyncRead, AsyncReadExt, AsyncWriteExt, BufReader},
    process::{Child, ChildStdin, ChildStdout, Command},
};
use tokio_util::sync::CancellationToken;

const QUALITY_OUTPUT_LIMIT: usize = 65_536;
const OPERATION: &str = "software-factory.operation";
const OWNER: &str = "software-factory.controller";
const WORKSPACE: &str = "software-factory.workspace";
#[path = "docker_broker.rs"]
mod daemon_broker;

pub use docker_transport::{BrokerConfig, DockerConfig};

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WorkerReceipt {
    pub version: u32,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub backend_identity: Option<Value>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub effective_policy: Option<Value>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub provider_policy: Option<Value>,
    pub operation_id: String,
    pub request_hash: String,
    pub request: ExecutionRequest,
    pub container_name: String,
    pub container_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub broker_container_id: Option<String>,
    pub image_id: Option<String>,
    pub reported: Option<super::AgentConfig>,
    pub creation_requested: bool,
    pub provider_thread_id: Option<String>,
    pub execution_id: Option<String>,
    pub turn_requested: bool,
    pub output: Option<String>,
    pub error: Option<String>,
    pub stopped: bool,
    pub pending_input: Option<PendingInput>,
    #[serde(default)]
    pub waited_millis: u64,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PendingInput {
    pub rpc_id: Value,
    pub interaction: super::SavedInteraction,
    pub delivered: bool,
    pub started_at: chrono::DateTime<chrono::Utc>,
}

pub struct ExecutionWorker {
    config: Config,
    backend: Backend,
    owner: String,
    identity: Vec<u8>,
}
enum Backend {
    Docker(DockerConfig),
    Native(super::native::NativeConfig),
}
#[path = "docker_transport.rs"]
mod docker_transport;
#[path = "native_broker.rs"]
mod native_transport;

impl ExecutionWorker {
    fn docker(&self) -> &DockerConfig {
        match &self.backend {
            Backend::Docker(config) => config,
            Backend::Native(_) => unreachable!("Docker-only transport"),
        }
    }
    fn native(&self) -> Option<&super::native::NativeConfig> {
        match &self.backend {
            Backend::Native(config) => Some(config),
            _ => None,
        }
    }
    fn daemon_broker(&self) -> bool {
        matches!(&self.backend, Backend::Docker(config) if config.broker.is_some())
    }
    fn open_backend(config: Config, backend: Backend) -> Result<Self> {
        let owner = digest(config.data_dir().as_os_str().as_encoded_bytes());
        let marker = config.workspace().join(".factory-rust.identity");
        let record = config.data_dir().join("docker-workspace.identity");
        let identity = if record.exists() {
            regular(&record)?
        } else if marker.exists() {
            regular(&marker)?
        } else {
            format!(
                "factory-workspace-v1:{}",
                digest(
                    format!(
                        "{}:{}",
                        config.workspace().display(),
                        chrono::Utc::now().timestamp_nanos_opt().unwrap_or_default()
                    )
                    .as_bytes()
                )
            )
            .into_bytes()
        };
        if !identity.starts_with(b"factory-workspace-v1:") {
            return Err(Error::Conflict(
                "workspace Docker identity is invalid; existing file preserved".into(),
            ));
        }
        if marker.exists() {
            if regular(&marker)? != identity {
                return Err(Error::Conflict("workspace Docker identity changed".into()));
            }
        } else {
            atomic(&marker, &identity)?;
        }
        if !record.exists() {
            atomic(&record, &identity)?;
        }
        Ok(Self {
            config,
            backend,
            owner,
            identity,
        })
    }
    fn container_name(&self, operation: &str) -> String {
        format!(
            "factory-{}",
            &digest(format!("{}:{operation}", self.owner).as_bytes())[..32]
        )
    }
    fn path(&self, operation: &str) -> PathBuf {
        self.config
            .data_dir()
            .join("worker-records")
            .join(format!("{}.json", digest(operation.as_bytes())))
    }
    pub fn receipt(&self, operation: &str) -> Result<Option<WorkerReceipt>> {
        let path = self.path(operation);
        if !path.exists() && !path.is_symlink() {
            return Ok(None);
        }
        let receipt: WorkerReceipt = serde_json::from_slice(&regular(&path)?)?;
        if receipt.version != 1 || receipt.operation_id != operation {
            return Err(Error::Incompatible(
                "worker receipt identity/version mismatch".into(),
            ));
        }
        if receipt.backend_identity != self.execution_identity()
            && !(receipt.backend_identity.is_none() && self.native().is_none())
        {
            return Err(Error::Conflict(
                "worker receipt backend/provider/policy changed".into(),
            ));
        }
        Ok(Some(receipt))
    }
    fn prepared_receipt(&self, request: ExecutionRequest, input: &str) -> WorkerReceipt {
        WorkerReceipt {
            version: 1,
            backend_identity: self.execution_identity(),
            effective_policy: None,
            provider_policy: None,
            operation_id: request.operation_id.clone(),
            request_hash: digest(input.as_bytes()),
            container_name: self.container_name(&request.operation_id),
            request,
            container_id: None,
            broker_container_id: None,
            image_id: None,
            reported: None,
            creation_requested: false,
            provider_thread_id: None,
            execution_id: None,
            turn_requested: false,
            output: None,
            error: None,
            stopped: false,
            pending_input: None,
            waited_millis: 0,
        }
    }
    fn save(&self, receipt: &WorkerReceipt) -> Result<()> {
        atomic(
            &self.path(&receipt.operation_id),
            &serde_json::to_vec_pretty(receipt)?,
        )
    }
    fn decode(&self, operation: &str, input: &str) -> Result<ExecutionRequest> {
        let request: ExecutionRequest = serde_json::from_str(input)?;
        if request.version != 1
            || request.operation_id != operation
            || Path::new(&request.workspace) != self.config.workspace()
            || Path::new(&request.evidence_directory)
                != self
                    .config
                    .data_dir()
                    .join("evidence")
                    .join(&request.run_id)
            || !request.run_id.starts_with("run-")
            || !request
                .run_id
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b == b'-')
        {
            return Err(Error::Conflict(
                "worker request is not bound to this controller workspace".into(),
            ));
        }
        match &request.execution {
            ExecutionKind::Specialist { agent, role, .. } => {
                agent.validate()?;
                if ![
                    "planner",
                    "builder",
                    "reviewer",
                    "request_reviewer",
                    "code_reviewer",
                    "security_reviewer",
                    "documentation_reviewer",
                    "judge",
                ]
                .contains(&role.as_str())
                {
                    return Err(Error::Config("unknown specialist role".into()));
                }
            }
            ExecutionKind::Quality { command, .. } => command.validate()?,
        }
        Ok(request)
    }
    async fn quality_logs(&self, receipt: &WorkerReceipt) -> Result<String> {
        if self.native().is_some() {
            self.native_quality_logs(receipt)
        } else {
            self.quality_logs_docker(receipt).await
        }
    }
    async fn inspect(&self, receipt: &WorkerReceipt) -> Result<Option<Value>> {
        if self.native().is_some() {
            self.inspect_native(receipt)
        } else {
            self.inspect_docker(receipt).await
        }
    }
    async fn stop(&self, receipt: &mut WorkerReceipt) -> Result<()> {
        if self.native().is_some() {
            self.stop_native(receipt).await
        } else {
            self.stop_docker(receipt).await
        }
    }
    fn home_relative(&self, request: &ExecutionRequest) -> PathBuf {
        let role = match &request.execution {
            ExecutionKind::Specialist { role, .. } => role.as_str(),
            _ => "quality",
        };
        PathBuf::from("provider-homes")
            .join(digest(format!("{}:{role}", request.run_id).as_bytes()))
    }
    fn previous_thread(&self, receipt: &WorkerReceipt) -> Result<Option<String>> {
        let request = &receipt.request;
        let root = self.config.data_dir().join("worker-records");
        let mut thread = None;
        if !root.exists() {
            return Ok(None);
        }
        for path in fs::read_dir(root)? {
            let path = path?.path();
            if path.extension().is_none_or(|s| s != "json") {
                continue;
            }
            let saved: WorkerReceipt = serde_json::from_slice(&regular(&path)?)?;
            if saved.request.run_id == request.run_id
                && self.home_relative(&saved.request) == self.home_relative(request)
                && saved.provider_thread_id.is_some()
                && (self.native().is_none()
                    || (saved.backend_identity == receipt.backend_identity
                        && saved.effective_policy == receipt.effective_policy
                        && matches!((&saved.request.execution, &request.execution), (ExecutionKind::Specialist { agent: a, .. }, ExecutionKind::Specialist { agent: b, .. }) if a == b)))
            {
                if !saved.stopped {
                    return Err(Error::Worker(
                        "previous worker sharing provider home is not confirmed stopped".into(),
                    ));
                }
                if thread
                    .as_ref()
                    .is_some_and(|id| Some(id) != saved.provider_thread_id.as_ref())
                {
                    return Err(Error::Worker(
                        "provider continuation has conflicting thread identities".into(),
                    ));
                }
                thread = saved.provider_thread_id;
            }
        }
        Ok(thread)
    }
    async fn start_process(&self, receipt: &mut WorkerReceipt) -> Result<Child> {
        if self.native().is_some() {
            return self.start_native(receipt).await;
        }
        let args = self.create_args(receipt).await?;
        receipt.creation_requested = true;
        self.save(receipt)?;
        let id = self.output(&args).await?.trim().to_owned();
        if id.len() != 64 || !id.bytes().all(|b| b.is_ascii_hexdigit()) {
            return Err(Error::Worker(
                "Docker create returned an invalid container ID".into(),
            ));
        }
        receipt.container_id = Some(id.clone());
        self.save(receipt)?;
        let inspected = self
            .inspect(receipt)
            .await?
            .ok_or_else(|| Error::Worker("new Docker container cannot be inspected".into()))?;
        receipt.image_id = inspected["Image"].as_str().map(String::from);
        self.save(receipt)?;
        let child = Command::new(&self.docker().executable)
            .args(["container", "start", "--attach", "--interactive", &id])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .kill_on_drop(true)
            .spawn()?;
        Ok(child)
    }
    async fn run(&self, receipt: &mut WorkerReceipt, cancel: CancellationToken) -> Result<String> {
        let mut child = self.start_process(receipt).await?;
        let execution = receipt.request.execution.clone();
        let outcome = match execution {
            ExecutionKind::Quality {
                command,
                content_version,
            } => {
                drop(child.stdin.take());
                let output = if self.native().is_some() {
                    self.read_native_quality(receipt, child.stdout.take().unwrap())
                        .await?
                } else {
                    read_quality_tail(child.stdout.take().unwrap()).await?
                };
                let status = child.wait().await?;
                let current = if self.native().is_none() {
                    self.inspect(receipt)
                        .await?
                        .ok_or_else(|| Error::Worker("quality container disappeared".into()))?
                } else {
                    json!({"State":{"ExitCode": status.code()}})
                };
                serde_json::to_string(&CheckResult {
                    name: command.name,
                    argv: command.argv,
                    exit_code: current["State"]["ExitCode"]
                        .as_i64()
                        .and_then(|n| i32::try_from(n).ok())
                        .or(status.code()),
                    timed_out: false,
                    cancelled: false,
                    output_tail: output,
                    content_version,
                    comparison: String::new(),
                })
                .map_err(Error::from)
            }
            ExecutionKind::Specialist { agent, prompt, .. } => {
                let mut rpc = Rpc::new(child)?;
                let result=async {
                    let initialized=rpc.request("initialize",json!({"clientInfo":{"name":"software-factory-rust-worker","version":"1"},"capabilities":{"experimentalApi":true}})).await?;
                    self.verify_workspace(receipt).await?;
                    if !initialized["userAgent"].as_str().is_some_and(|s|s.contains("/0.154.0 ")) {return Err(Error::Config("worker App Server version differs from pinned 0.154.0".into()));}
                    rpc.send(&json!({"method":"initialized"})).await?;
                    let previous=self.previous_thread(receipt)?;
                    let mut params=json!({"model":agent.model,"cwd":receipt.request.workspace,"permissions":"factory","approvalPolicy":self.approval_policy(),"config":{"model_reasoning_effort":agent.reasoning_level}});
                    let method=if let Some(previous)=previous {params["threadId"]=json!(previous);"thread/resume"}else {params["selectedCapabilityRoots"]=json!([]);params["allowProviderModelFallback"]=json!(false);"thread/start"};
                    let started=rpc.request(method,params).await?;
                    if started["model"]!=agent.model || started.get("reasoningEffort").is_some_and(|v|!v.is_null() && *v!=agent.reasoning_level) {return Err(Error::Config("provider substituted the saved model or effort".into()));}
                    if started["approvalPolicy"]!=self.approval_policy(){return Err(Error::Config("provider changed the recorded approval policy".into()));}
                    if self.native().is_some() {
                        if started["activePermissionProfile"]["id"] != "factory"
                            || !matches!(started["sandbox"]["type"].as_str(), Some("readOnly" | "workspaceWrite"))
                            || started["sandbox"]["networkAccess"] != false
                            || started["cwd"] != receipt.request.workspace {
                            return Err(Error::Config("native provider did not retain the required filesystem/network permission profile".into()));
                        }
                        receipt.provider_policy = Some(json!({"profile":started["activePermissionProfile"],"sandbox":started["sandbox"],"cwd":started["cwd"],"approval_policy":started["approvalPolicy"]}));
                    }
                    receipt.reported=started["reasoningEffort"].as_str().map(|_|agent.clone());
                    receipt.provider_thread_id=started["thread"]["id"].as_str().map(String::from);
                    if receipt.provider_thread_id.is_none(){return Err(Error::Worker("thread response has no provider identity".into()));}
                    self.save(receipt)?;
                    receipt.turn_requested=true;self.save(receipt)?;
                    let turn=rpc.request("turn/start",json!({"threadId":receipt.provider_thread_id,"clientUserMessageId":receipt.operation_id,"input":[{"type":"text","text":prompt}],"model":agent.model,"effort":agent.reasoning_level,"permissions":"factory","approvalPolicy":self.approval_policy()})).await?;
                    receipt.execution_id=turn["turn"]["id"].as_str().map(String::from);
                    if receipt.execution_id.is_none(){return Err(Error::Worker("turn response has no execution identity".into()));}
                    self.save(receipt)?;
                    loop {
                        self.deliver_answer(receipt,&mut rpc).await?;
                        let event=tokio::select! {event=rpc.next()=>event?,_=tokio::time::sleep(Duration::from_millis(200))=>continue};
                        if event["method"]=="item/tool/requestUserInput" && event["params"]["threadId"].as_str()==receipt.provider_thread_id.as_deref() && event["params"]["turnId"].as_str()==receipt.execution_id.as_deref() {
                            self.save_question(receipt,&event).await?;continue;
                        }
                        if event.get("id").is_some() {rpc.reject(&event).await?;continue;}
                        if let Some(output)=completed_output(&event,receipt)? {break Ok(output);}
                    }
                }.await;
                // Keep the attach process alive until controller cleanup has killed
                // the actual container, including every descendant in its cgroup.
                drop(rpc);
                result
            }
        };
        if cancel.is_cancelled() {
            return Err(Error::Cancelled);
        }
        outcome
    }
    async fn recover_output(&self, receipt: &mut WorkerReceipt) -> Result<Option<String>> {
        if self.native().is_some() {
            return Ok(None);
        }
        let Some(current) = self.inspect(receipt).await? else {
            return Ok(None);
        };
        receipt.container_id = current["Id"].as_str().map(String::from);
        self.save(receipt)?;
        match &receipt.request.execution {
            ExecutionKind::Quality {
                command,
                content_version,
            } => {
                if current["State"]["Running"] == true || current["State"]["Status"] == "created" {
                    return Ok(None);
                }
                let output = self.quality_logs(receipt).await?;
                Ok(Some(serde_json::to_string(&CheckResult {
                    name: command.name.clone(),
                    argv: command.argv.clone(),
                    exit_code: current["State"]["ExitCode"]
                        .as_i64()
                        .and_then(|n| i32::try_from(n).ok()),
                    timed_out: false,
                    cancelled: false,
                    output_tail: output,
                    content_version: content_version.clone(),
                    comparison: String::new(),
                })?))
            }
            ExecutionKind::Specialist { .. } => {
                let logs = self
                    .output(&[
                        "container".into(),
                        "logs".into(),
                        receipt.container_id.clone().unwrap(),
                    ])
                    .await?;
                let mut output = None;
                for line in logs.lines() {
                    if let Ok(event) = serde_json::from_str::<Value>(line) {
                        // Outgoing requests use fixed RPC sequence: initialize=1,
                        // thread start/resume=2, turn start=3. Recover only those exact
                        // responses; never use a provider's latest conversation text.
                        if event["id"] == 2 && receipt.provider_thread_id.is_none() {
                            receipt.provider_thread_id =
                                event["result"]["thread"]["id"].as_str().map(String::from);
                        }
                        if event["id"] == 3
                            && receipt.execution_id.is_none()
                            && receipt.turn_requested
                        {
                            receipt.execution_id =
                                event["result"]["turn"]["id"].as_str().map(String::from);
                        }
                        if let Some(done) = completed_output(&event, receipt)? {
                            output = Some(done);
                        }
                    }
                }
                Ok(output)
            }
        }
    }
}
fn tail(text: &str, limit: usize) -> String {
    let start = text.len().saturating_sub(limit);
    text.char_indices()
        .find(|(i, _)| *i >= start)
        .map(|(i, _)| text[i..].to_owned())
        .unwrap_or_default()
}
/// Drain every byte so the producer cannot block, retaining a fixed-size byte
/// tail even for a single arbitrarily long line or incomplete UTF-8 sequence.
async fn read_quality_tail(mut reader: impl AsyncRead + Unpin) -> std::io::Result<String> {
    let mut output = VecDeque::with_capacity(QUALITY_OUTPUT_LIMIT);
    let mut chunk = [0; 8192];
    loop {
        let read = reader.read(&mut chunk).await?;
        if read == 0 {
            break;
        }
        let discard = (output.len() + read).saturating_sub(QUALITY_OUTPUT_LIMIT);
        output.drain(..discard);
        output.extend(&chunk[..read]);
    }
    let bytes: Vec<_> = output.into_iter().collect();
    Ok(tail(&String::from_utf8_lossy(&bytes), QUALITY_OUTPUT_LIMIT))
}
fn completed_output(event: &Value, receipt: &WorkerReceipt) -> Result<Option<String>> {
    if event["method"] != "turn/completed"
        || event["params"]["threadId"].as_str() != receipt.provider_thread_id.as_deref()
        || event["params"]["turn"]["id"].as_str() != receipt.execution_id.as_deref()
        || receipt.execution_id.is_none()
    {
        return Ok(None);
    }
    let turn = &event["params"]["turn"];
    if turn["status"] != "completed" {
        return Err(Error::Worker(format!(
            "provider turn did not complete: {}",
            turn["status"]
        )));
    }
    let text = turn["items"]
        .as_array()
        .map(|items| {
            items
                .iter()
                .filter(|i| i["type"] == "agentMessage" && i["phase"] != "commentary")
                .filter_map(|i| i["text"].as_str())
                .collect::<Vec<_>>()
                .join("\n")
        })
        .unwrap_or_default();
    if text.is_empty() {
        return Err(Error::Worker(
            "completed provider turn has no final output".into(),
        ));
    }
    Ok(Some(text))
}
#[async_trait]
impl Worker for ExecutionWorker {
    fn execution_identity(&self) -> Option<Value> {
        Some(match &self.backend {
            Backend::Docker(config) => {
                json!({"backend":"docker", "provider":"codex", "version":"0.154.0", "configuration":config})
            }
            Backend::Native(config) => {
                json!({"backend":"native", "provider":"codex", "version":"0.154.0", "policy":"linux-bwrap-cgroup-v1", "configuration":config})
            }
        })
    }

    async fn model_catalog(&self) -> Result<Vec<super::config::ModelOption>> {
        self.catalog().await
    }
    async fn reconcile(&self, operation: &str, input: &str) -> Result<Reconciliation> {
        let request = self.decode(operation, input)?;
        let broker_running = if self.daemon_broker() {
            if let Some(receipt) = self.receipt(operation)? {
                if receipt.request != request || receipt.request_hash != digest(input.as_bytes()) {
                    return Err(Error::Conflict(
                        "worker receipt input/settings changed".into(),
                    ));
                }
                receipt.broker_container_id.is_some()
                    && self
                        .inspect_daemon_broker(&receipt)
                        .await?
                        .is_some_and(|state| state["State"]["Running"] == true)
            } else {
                false
            }
        } else {
            self.broker_lock(operation)?.is_none()
        };
        if broker_running {
            return match self
                .poll_broker(operation, input, CancellationToken::new())
                .await
            {
                Ok(output) => Ok(Reconciliation::Completed(output)),
                Err(Error::Waiting(reason)) => Ok(Reconciliation::Waiting(reason)),
                Err(error) => Err(error),
            };
        }
        let Some(mut receipt) = self.receipt(operation)? else {
            let provisional = self.prepared_receipt(request, input);
            return Ok(if self.inspect(&provisional).await?.is_none() {
                Reconciliation::Absent
            } else {
                Reconciliation::Unknown("container exists without its authoritative receipt".into())
            });
        };
        if receipt.request_hash != digest(input.as_bytes()) || receipt.request != request {
            return Err(Error::Conflict(
                "worker receipt input/settings changed".into(),
            ));
        }
        if receipt.output.is_none() {
            loop {
                receipt.output = self.recover_output(&mut receipt).await?;
                if receipt.output.is_some() {
                    break;
                }
                // A containerized controller loses its broker on recreation,
                // while the exact daemon-owned worker can still be finishing.
                // Observe that same worker until its saved deadline; never
                // interpret a single unfinished log read as a lost execution.
                let running = self
                    .inspect(&receipt)
                    .await?
                    .is_some_and(|state| state["State"]["Running"] == true);
                if !running
                    || receipt
                        .pending_input
                        .as_ref()
                        .is_some_and(|input| !input.delivered)
                {
                    break;
                }
                if self.stop_requested(&receipt.request).await? {
                    return Err(Error::Cancelled);
                }
                let operation_deadline = receipt.request.deadline_at
                    + chrono::Duration::milliseconds(receipt.waited_millis as i64);
                let deadline = self
                    .control_run(&receipt.request)
                    .await?
                    .map(|run| run.deadline_at.min(operation_deadline))
                    .unwrap_or(operation_deadline);
                if chrono::Utc::now() >= deadline {
                    return Err(Error::Deadline);
                }
                tokio::time::sleep(Duration::from_millis(200)).await;
            }
            self.save(&receipt)?;
        }
        if let Some(output) = receipt.output.clone() {
            self.stop(&mut receipt).await?;
            return Ok(Reconciliation::Completed(output));
        }
        if !receipt.creation_requested {
            return Ok(Reconciliation::Absent);
        }
        Ok(Reconciliation::Unknown(
            "saved provider execution has no confirmed result; it will not be submitted again"
                .into(),
        ))
    }
    async fn dispatch(
        &self,
        operation: &str,
        input: &str,
        cancel: CancellationToken,
    ) -> Result<String> {
        self.launch(operation, input).await?;
        self.poll_broker(operation, input, cancel).await
    }
    async fn stop_operation(&self, operation: &str, input: &str) -> Result<()> {
        let request = self.decode(operation, input)?;
        atomic(&self.path(operation).with_extension("stop"), b"stop")?;
        let limit = tokio::time::Instant::now() + Duration::from_secs(30);
        let _fence = loop {
            if let Some(lock) = self.broker_lock(operation)? {
                break lock;
            }
            if tokio::time::Instant::now() >= limit {
                return Err(Error::Worker(
                    "broker has not confirmed process-tree cleanup".into(),
                ));
            }
            tokio::time::sleep(Duration::from_millis(100)).await;
        };
        if let Some(mut receipt) = self.receipt(operation)? {
            if receipt.request != request || receipt.request_hash != digest(input.as_bytes()) {
                return Err(Error::Conflict("cleanup request input changed".into()));
            }
            self.stop(&mut receipt).await?;
            if self.daemon_broker() {
                self.stop_daemon_broker(&receipt).await?;
            }
        }
        Ok(())
    }
}
struct Rpc {
    child: Child,
    input: ChildStdin,
    lines: BufReader<ChildStdout>,
    buffer: Vec<u8>,
    sequence: u64,
    events: VecDeque<(Value, usize)>,
    events_bytes: usize,
}
impl Rpc {
    fn new(mut child: Child) -> Result<Self> {
        let input = child
            .stdin
            .take()
            .ok_or_else(|| Error::Worker("worker stdin missing".into()))?;
        let stdout = child
            .stdout
            .take()
            .ok_or_else(|| Error::Worker("worker stdout missing".into()))?;
        Ok(Self {
            child,
            input,
            lines: BufReader::new(stdout),
            buffer: Vec::new(),
            sequence: 0,
            events: VecDeque::new(),
            events_bytes: 0,
        })
    }
    async fn send(&mut self, value: &Value) -> Result<()> {
        self.input
            .write_all(format!("{value}\n").as_bytes())
            .await?;
        self.input.flush().await?;
        Ok(())
    }
    async fn read(&mut self) -> Result<Value> {
        const LIMIT: usize = 16 * 1024 * 1024;
        let remaining = LIMIT + 1 - self.buffer.len();
        let read = (&mut self.lines)
            .take(remaining as u64)
            .read_until(b'\n', &mut self.buffer)
            .await?;
        if self.buffer.len() > LIMIT {
            return Err(Error::Worker("provider event exceeds limit".into()));
        }
        if read == 0 || self.buffer.last() != Some(&b'\n') {
            return Err(Error::Worker(
                "provider connection closed before its saved turn completed".into(),
            ));
        }
        let event = serde_json::from_slice(&self.buffer)?;
        self.buffer.clear();
        Ok(event)
    }
    async fn next(&mut self) -> Result<Value> {
        match self.events.pop_front() {
            Some((event, bytes)) => {
                self.events_bytes -= bytes;
                Ok(event)
            }
            None => self.read().await,
        }
    }
    async fn request(&mut self, method: &str, params: Value) -> Result<Value> {
        self.sequence += 1;
        let id = self.sequence;
        self.send(&json!({"id":id,"method":method,"params":params}))
            .await?;
        loop {
            let event = self.read().await?;
            if event["id"] == id && event.get("method").is_none() {
                if event.get("error").is_some() {
                    return Err(Error::Worker(format!(
                        "provider RPC error: {}",
                        event["error"]
                    )));
                }
                return Ok(event["result"].clone());
            }
            if event.get("id").is_some()
                && event.get("method").is_some()
                && event["method"] != "item/tool/requestUserInput"
            {
                self.reject(&event).await?;
            } else {
                let bytes = serde_json::to_vec(&event)?.len();
                if self.events.len() >= 4096 || self.events_bytes + bytes > 16 * 1024 * 1024 {
                    return Err(Error::Worker(
                        "provider pending event limit exceeded".into(),
                    ));
                }
                self.events_bytes += bytes;
                self.events.push_back((event, bytes));
            }
        }
    }
    async fn reject(&mut self, event: &Value) -> Result<()> {
        self.send(&json!({"id":event["id"],"error":{"code":-32601,"message":"Factory does not authorize this provider request"}})).await
    }
}
impl Drop for Rpc {
    fn drop(&mut self) {
        let _ = self.child.start_kill();
    }
}

impl ExecutionWorker {
    /// Catalog inspection makes no coding turn and never selects a fallback.
    pub async fn catalog(&self) -> Result<Vec<super::config::ModelOption>> {
        // Catalog RPCs bypass the run supervisor. Bound them across HTTP calls
        // and maintenance CLI processes sharing this controller data directory.
        let catalog_lock = fs::OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(self.config.data_dir().join("docker-catalog.lock"))?;
        catalog_lock.try_lock().map_err(|_| Error::Busy)?;
        let records = self.config.data_dir().join("worker-records");
        if records.exists() {
            for entry in fs::read_dir(records)? {
                let path = entry?.path();
                if path.extension().is_none_or(|extension| extension != "json") {
                    continue;
                }
                let mut saved: WorkerReceipt = serde_json::from_slice(&regular(&path)?)?;
                if matches!(&saved.request.execution,
                    ExecutionKind::Specialist { step, .. } if step == "catalog")
                    && !saved.stopped
                {
                    // A controller death can leave a catalog outside supervision.
                    // Confirm its exact labeled identity stopped before replacing it.
                    self.stop(&mut saved).await?;
                }
            }
        }
        let run_id = format!(
            "run-catalog-{}",
            chrono::Utc::now().timestamp_nanos_opt().unwrap_or_default()
        );
        let evidence = self.config.data_dir().join("evidence").join(&run_id);
        fs::create_dir_all(&evidence)?;
        let request = ExecutionRequest {
            version: 1,
            operation_id: format!("{run_id}-models"),
            run_id,
            workspace: self.config.workspace().to_string_lossy().into_owned(),
            evidence_directory: evidence.to_string_lossy().into_owned(),
            protected_paths: vec![],
            deadline_at: chrono::Utc::now() + chrono::Duration::seconds(60),
            execution: ExecutionKind::Specialist {
                role: "planner".into(),
                step: "catalog".into(),
                agent: super::AgentConfig {
                    provider: "codex".into(),
                    model: "catalog-only".into(),
                    reasoning_level: "low".into(),
                    permission_mode: "auto".into(),
                },
                prompt: String::new(),
            },
        };
        let encoded = serde_json::to_string(&request)?;
        let mut receipt = self.prepared_receipt(request, &encoded);
        self.save(&receipt)?;
        let result=tokio::time::timeout(Duration::from_secs(60),async {
            let child=self.start_process(&mut receipt).await?;
            let mut rpc=Rpc::new(child)?;
            let initialized=rpc.request("initialize",json!({"clientInfo":{"name":"software-factory-models","version":"1"},"capabilities":{"experimentalApi":true}})).await?;
            if !initialized["userAgent"].as_str().is_some_and(|s|s.contains("/0.154.0 ")){return Err(Error::Config("catalog App Server version mismatch".into()));}
            rpc.send(&json!({"method":"initialized"})).await?;
            let mut catalog=vec![];let mut cursor=Value::Null;
            loop {
                let response=rpc.request("model/list",json!({"limit":100,"cursor":cursor})).await?;
                let values=response["data"].as_array().ok_or_else(||Error::Worker("model/list returned no catalog".into()))?;
                for model in values {
                    let id=model["model"].as_str().or_else(||model["id"].as_str()).ok_or_else(||Error::Worker("catalog model has no ID".into()))?;
                    let efforts=model["supportedReasoningEfforts"].as_array().map(|list|list.iter().filter_map(|e|e["reasoningEffort"].as_str().map(String::from)).collect()).unwrap_or_default();
                    catalog.push(super::config::ModelOption {provider:"codex".into(),model:id.into(),name:model["displayName"].as_str().unwrap_or(id).into(),efforts,default_effort:model["defaultReasoningEffort"].as_str().unwrap_or("high").into(),is_default:model["isDefault"]==true,permission_modes:vec!["auto".into(),"accept-edits".into()]});
                }
                cursor=response["nextCursor"].clone();if cursor.is_null(){break;}
            }
            receipt.output=Some(serde_json::to_string(&catalog)?);self.save(&receipt)?;Ok(catalog)
        }).await.map_err(|_|Error::Worker("catalog inspection timed out".into())).and_then(|r|r);
        self.stop(&mut receipt).await?;
        if self.native().is_none()
            && let Some(id) = &receipt.container_id
        {
            self.output(&["container".into(), "rm".into(), id.clone()])
                .await?;
        }
        result
    }
}

impl ExecutionWorker {
    fn broker_path(&self, operation: &str) -> PathBuf {
        self.path(operation).with_extension("lock")
    }
    fn broker_lock(&self, operation: &str) -> Result<Option<fs::File>> {
        let path = self.broker_path(operation);
        fs::create_dir_all(path.parent().unwrap())?;
        let file = fs::OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(path)?;
        match file.try_lock() {
            Ok(()) => Ok(Some(file)),
            Err(std::fs::TryLockError::WouldBlock) => Ok(None),
            Err(std::fs::TryLockError::Error(error)) => Err(error.into()),
        }
    }
    async fn control_run(&self, request: &ExecutionRequest) -> Result<Option<crate::Run>> {
        let database = self.config.data_dir().join("factory-rust.sqlite");
        if !database.exists() {
            return Ok(None);
        }
        let pool = sqlx::sqlite::SqlitePoolOptions::new()
            .max_connections(1)
            .connect_with(
                sqlx::sqlite::SqliteConnectOptions::new()
                    .filename(database)
                    .read_only(true),
            )
            .await?;
        let document: Option<String> =
            sqlx::query_scalar("SELECT document FROM factory_slice_runs WHERE run_id = ?")
                .bind(&request.run_id)
                .fetch_optional(&pool)
                .await?;
        pool.close().await;
        document
            .map(|d| serde_json::from_str(&d))
            .transpose()
            .map_err(Error::from)
    }
    async fn stop_requested(&self, request: &ExecutionRequest) -> Result<bool> {
        if self
            .path(&request.operation_id)
            .with_extension("stop")
            .exists()
        {
            return Ok(true);
        }
        if let Some(run) = self.control_run(request).await? {
            if run.cancel_requested {
                return Ok(true);
            }
            if let Some(parent) = run.workflow.as_ref().and_then(|s| s.parent_run_id.as_ref()) {
                let mut parent_request = request.clone();
                parent_request.run_id = parent.clone();
                if self.control_run(&parent_request).await?.is_some_and(|r| {
                    r.workflow
                        .as_ref()
                        .is_some_and(|s| s.delivery["cancel_requested"] == true)
                }) {
                    return Ok(true);
                }
            }
        }
        Ok(false)
    }
    /// Independent controller-owned broker process. Its receipt lock fences
    /// duplicate launches; provider containers never see its storage or daemon.
    pub async fn broker(&self, operation: &str) -> Result<()> {
        let Some(_lease) = self.broker_lock(operation)? else {
            return Ok(());
        };
        let receipt = self
            .receipt(operation)?
            .ok_or_else(|| Error::Worker("broker has no saved launch intent".into()))?;
        if receipt.creation_requested || receipt.output.is_some() || receipt.stopped {
            return Ok(());
        }
        let encoded = serde_json::to_string(&receipt.request)?;
        let _ = self
            .dispatch_local(operation, &encoded, CancellationToken::new())
            .await;
        Ok(())
    }
    async fn launch(&self, operation: &str, input: &str) -> Result<()> {
        let request = self.decode(operation, input)?;
        let Some(lease) = self.broker_lock(operation)? else {
            return Ok(());
        };
        if self.receipt(operation)?.is_none() {
            let receipt = self.prepared_receipt(request, input);
            self.save(&receipt)?;
        }
        let receipt = self.receipt(operation)?.unwrap();
        if receipt.creation_requested || receipt.output.is_some() || receipt.stopped {
            return Ok(());
        }
        self.persist_backend()?;
        drop(lease);
        if self.daemon_broker() {
            return self.launch_daemon_broker(receipt).await;
        }
        let current = std::env::current_exe()?;
        let executable = if current
            .file_name()
            .is_some_and(|s| s == "factory-runtime" || s == "software-factory")
        {
            current
        } else {
            current
                .parent()
                .and_then(Path::parent)
                .ok_or_else(|| Error::Config("broker executable unavailable".into()))?
                .join("factory-runtime")
        };
        let log = self.path(operation).with_extension("broker.log");
        let log = fs::OpenOptions::new().create(true).append(true).open(log)?;
        let mut command = Command::new(executable);
        command
            .arg(self.config.data_dir())
            .arg(self.config.workspace())
            .args(["worker-broker", operation])
            .stdin(Stdio::null())
            .stdout(Stdio::from(log.try_clone()?))
            .stderr(Stdio::from(log))
            .process_group(0)
            .kill_on_drop(false);
        let mut child = command.spawn()?;
        // Reap while this controller lives; dropping this task on process death
        // leaves the independent broker and its Docker identity intact.
        tokio::spawn(async move {
            let _ = child.wait().await;
        });
        Ok(())
    }
    async fn poll_broker(
        &self,
        operation: &str,
        input: &str,
        cancel: CancellationToken,
    ) -> Result<String> {
        let request = self.decode(operation, input)?;
        let mut next_liveness = tokio::time::Instant::now() + Duration::from_secs(10);
        loop {
            if cancel.is_cancelled() || self.stop_requested(&request).await? {
                atomic(&self.path(operation).with_extension("stop"), b"stop")?;
                self.stop_operation(operation, input).await?;
                return Err(Error::Cancelled);
            }
            let receipt = self
                .receipt(operation)?
                .ok_or_else(|| Error::Worker("worker launch receipt missing".into()))?;
            if receipt.stopped {
                if let Some(output) = receipt.output {
                    return Ok(output);
                }
                return Err(match receipt.error.as_deref() {
                    Some("cancelled") => Error::Cancelled,
                    Some("execution deadline expired") => Error::Deadline,
                    _ => Error::Worker(
                        receipt
                            .error
                            .unwrap_or_else(|| "broker stopped without a provider result".into()),
                    ),
                });
            }
            if let Some(run) = self.control_run(&request).await?
                && run.status == crate::Status::WaitingForInput
                && run
                    .workflow
                    .as_ref()
                    .is_some_and(|s| s.interaction.is_some())
            {
                return Err(Error::Waiting("provider questions saved".into()));
            }
            if tokio::time::Instant::now() > next_liveness {
                let alive = if self.daemon_broker() {
                    self.inspect_daemon_broker(&receipt)
                        .await?
                        .is_some_and(|state| state["State"]["Running"] == true)
                } else {
                    self.broker_lock(operation)?.is_none()
                };
                // Completion can be committed while the liveness read is in
                // flight. Never turn that newer confirmed result into recovery.
                let latest = self
                    .receipt(operation)?
                    .ok_or_else(|| Error::Worker("worker launch receipt missing".into()))?;
                next_liveness = tokio::time::Instant::now() + Duration::from_secs(5);
                if latest.stopped {
                    continue;
                }
                if !alive {
                    return Err(Error::Worker(
                        "worker broker exited; reconcile its exact Docker receipt".into(),
                    ));
                }
            }
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
    }
}

impl ExecutionWorker {
    async fn dispatch_local(
        &self,
        operation: &str,
        input: &str,
        cancel: CancellationToken,
    ) -> Result<String> {
        let request = self.decode(operation, input)?;
        if let Some(saved) = self.receipt(operation)?
            && saved.creation_requested
        {
            return Err(Error::Worker(
                "operation already started; reconcile its exact container".into(),
            ));
        }
        if cancel.is_cancelled() || self.stop_requested(&request).await? {
            return Err(Error::Cancelled);
        }
        let broker_container_id = self
            .receipt(operation)?
            .and_then(|saved| saved.broker_container_id);
        let mut receipt = self.prepared_receipt(request, input);
        receipt.broker_container_id = broker_container_id;
        self.save(&receipt)?;
        let control = receipt.request.clone();
        let mut command_timed_out = false;
        let result = {
            let work = self.run(&mut receipt, cancel.clone());
            tokio::pin!(work);
            let mut tick = tokio::time::interval(Duration::from_millis(200));
            loop {
                tokio::select! {
                    biased;
                    _=cancel.cancelled()=>break Err(Error::Cancelled),
                    result=&mut work=>break result,
                    _=tick.tick()=>{
                        if self.stop_requested(&control).await? {break Err(Error::Cancelled);}
                        let saved=self.receipt(operation)?.unwrap();
                        let run=self.control_run(&control).await?;
                        let waiting=saved.pending_input.as_ref().is_some_and(|p|!p.delivered);
                        let expiry=if waiting {run.as_ref().and_then(|r|r.workflow.as_ref().and_then(|w|w.overall_deadline_at)).unwrap_or(control.deadline_at)}else{(control.deadline_at+chrono::Duration::milliseconds(saved.waited_millis as i64)).min(run.as_ref().map(|r|r.deadline_at).unwrap_or(control.deadline_at+chrono::Duration::milliseconds(saved.waited_millis as i64)))};
                        let now = chrono::Utc::now();
                        if now>=expiry {
                            command_timed_out = matches!(control.execution, ExecutionKind::Quality { .. })
                                && run.as_ref().is_none_or(|run| now < run.deadline_at);
                            break Err(Error::Deadline);
                        }
                    }
                }
            }
        };
        // The run future must be dropped before borrowing its receipt for cleanup.
        let mut receipt = self
            .receipt(operation)?
            .ok_or_else(|| Error::Worker("worker receipt disappeared".into()))?;
        let result = if command_timed_out {
            // Prepare a failed check, but never return it until the exact container
            // and all of its descendants have been confirmed stopped below.
            let ExecutionKind::Quality {
                command,
                content_version,
            } = &control.execution
            else {
                unreachable!()
            };
            match self.quality_logs(&receipt).await {
                Ok(output_tail) => serde_json::to_string(&CheckResult {
                    name: command.name.clone(),
                    argv: command.argv.clone(),
                    exit_code: None,
                    timed_out: true,
                    cancelled: false,
                    output_tail,
                    content_version: content_version.clone(),
                    comparison: String::new(),
                })
                .map_err(Error::from),
                Err(error) => Err(error),
            }
        } else {
            result
        };
        match &result {
            Ok(output) => receipt.output = Some(output.clone()),
            Err(error) => receipt.error = Some(error.to_string()),
        };
        self.save(&receipt)?;
        self.stop(&mut receipt).await?;
        result
    }
}

impl ExecutionWorker {
    async fn save_question(&self, receipt: &mut WorkerReceipt, event: &Value) -> Result<()> {
        use crate::FactoryStore;
        let questions = event["params"]["questions"]
            .as_array()
            .ok_or_else(|| Error::Worker("provider question has no questions".into()))?;
        let mut parsed = vec![];
        let mut ids = std::collections::BTreeSet::new();
        for question in questions {
            let id = question["id"]
                .as_str()
                .filter(|s| !s.is_empty())
                .ok_or_else(|| Error::Worker("provider question ID missing".into()))?;
            if !ids.insert(id) {
                return Err(Error::Worker("provider repeated a question ID".into()));
            }
            parsed.push(super::Question {
                id: id.into(),
                question: question["question"]
                    .as_str()
                    .filter(|s| !s.is_empty())
                    .ok_or_else(|| Error::Worker("provider question text missing".into()))?
                    .into(),
                options: question["options"]
                    .as_array()
                    .map(|a| {
                        a.iter()
                            .filter_map(|o| o["label"].as_str().map(String::from))
                            .collect()
                    })
                    .unwrap_or_default(),
                recommendation: None,
                context: None,
            });
        }
        if parsed.is_empty()
            || event.get("id").is_none()
            || receipt.pending_input.as_ref().is_some_and(|p| !p.delivered)
        {
            return Err(Error::Worker(
                "overlapping or unidentified provider question".into(),
            ));
        }
        let item = event["params"]["itemId"]
            .as_str()
            .ok_or_else(|| Error::Worker("provider input item identity missing".into()))?;
        let interaction = super::SavedInteraction {
            interaction_id: format!("{}:input:{item}", receipt.operation_id),
            questions: parsed,
            answers: None,
        };
        receipt.pending_input = Some(PendingInput {
            rpc_id: event["id"].clone(),
            interaction: interaction.clone(),
            delivered: false,
            started_at: chrono::Utc::now(),
        });
        self.save(receipt)?;
        let pool = crate::config::pool(&self.config.data_dir().join("factory-rust.sqlite")).await?;
        let store = crate::SqliteFactoryStore::new(pool).await?;
        for _ in 0..32 {
            let mut run = store.load(&receipt.request.run_id).await?;
            let revision = run.revision;
            let state = run.workflow.as_mut().ok_or_else(|| {
                Error::Worker("provider question has no implementation workflow".into())
            })?;
            if let Some(existing) = &state.interaction
                && existing != &interaction
            {
                return Err(Error::Worker(
                    "another specialist question is already pending".into(),
                ));
            }
            state.interaction = Some(interaction.clone());
            state.wait_started_at.get_or_insert(chrono::Utc::now());
            run.status = crate::Status::WaitingForInput;
            run.revision += 1;
            if store.save(revision, &run).await? {
                return Ok(());
            }
        }
        Err(Error::Worker("provider question could not be saved".into()))
    }
    async fn deliver_answer(&self, receipt: &mut WorkerReceipt, rpc: &mut Rpc) -> Result<()> {
        let Some(pending) = receipt.pending_input.clone() else {
            return Ok(());
        };
        if pending.delivered {
            return Ok(());
        }
        let Some(run) = self.control_run(&receipt.request).await? else {
            return Err(Error::Worker(
                "saved provider interaction is unavailable".into(),
            ));
        };
        let answer = run
            .workflow
            .as_ref()
            .and_then(|s| s.interactions.get(&pending.interaction.interaction_id))
            .and_then(|i| i.answers.clone());
        if let Some(answers) = answer {
            receipt.pending_input.as_mut().unwrap().delivered = true;
            receipt.waited_millis = receipt.waited_millis.saturating_add(
                (chrono::Utc::now() - pending.started_at)
                    .num_milliseconds()
                    .max(0) as u64,
            );
            self.save(receipt)?;
            let answers: serde_json::Map<_, _> = answers
                .into_iter()
                .map(|(id, answer)| (id, json!({"answers":[answer]})))
                .collect();
            rpc.send(&json!({"id":pending.rpc_id,"result":{"answers":answers}}))
                .await?;
        }
        Ok(())
    }
}

#[cfg(test)]
mod quality_tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

    async fn fixture(
        mode: &str,
        run_expired: bool,
    ) -> (
        tempfile::TempDir,
        tempfile::TempDir,
        ExecutionWorker,
        ExecutionRequest,
    ) {
        let data = tempfile::tempdir().unwrap();
        let workspace = tempfile::tempdir().unwrap();
        assert!(
            std::process::Command::new("git")
                .args(["init", "-q"])
                .arg(workspace.path())
                .status()
                .unwrap()
                .success()
        );
        let config = Config::new(data.path(), workspace.path()).unwrap();
        let script = data.path().join("docker");
        fs::write(
            &script,
            include_str!("../../tests/fixtures/docker-quality.py"),
        )
        .unwrap();
        fs::set_permissions(&script, fs::Permissions::from_mode(0o700)).unwrap();
        fs::write(data.path().join("mode"), mode).unwrap();
        let mut docker = DockerConfig::local(&config, None);
        docker.executable = script;
        docker.uid = 1000;
        docker.gid = 1000;
        let run_id = if run_expired {
            let runtime = crate::Runtime::open(config.clone()).await.unwrap();
            let mut input = crate::StartRequest::new("expired", "session", "fixture");
            input.timeout_seconds = 60;
            runtime.start(input).await.unwrap().run_id
        } else {
            "run-quality".into()
        };
        let request = ExecutionRequest {
            version: 1,
            operation_id: format!("{run_id}-check"),
            run_id: run_id.clone(),
            workspace: config.workspace().to_string_lossy().into_owned(),
            evidence_directory: config
                .data_dir()
                .join("evidence")
                .join(&run_id)
                .to_string_lossy()
                .into_owned(),
            protected_paths: vec![],
            deadline_at: chrono::Utc::now()
                + chrono::Duration::seconds(if run_expired { 60 } else { 1 }),
            execution: ExecutionKind::Quality {
                command: super::super::QualityCommand {
                    name: "fixture".into(),
                    argv: vec!["python3".into(), "-c".into(), "pass".into()],
                    timeout_seconds: 1,
                    repeat_on_recovery: true,
                },
                content_version: "content".into(),
            },
        };
        let worker = ExecutionWorker::open(config, docker).unwrap();
        (data, workspace, worker, request)
    }

    #[tokio::test]
    async fn command_timeout_is_a_failed_check_after_confirmed_cleanup() {
        let (_data, _workspace, worker, request) = fixture("timeout", false).await;
        let input = serde_json::to_string(&request).unwrap();
        let output = worker
            .dispatch_local(&request.operation_id, &input, CancellationToken::new())
            .await
            .unwrap();
        let check: CheckResult = serde_json::from_str(&output).unwrap();
        assert!(check.timed_out);
        assert!(!check.passed());
        assert_eq!(check.content_version, "content");
        assert!(check.output_tail.contains("before timeout"));
        let receipt = worker.receipt(&request.operation_id).unwrap().unwrap();
        assert!(receipt.stopped);
        assert!(receipt.error.is_none());
        assert_eq!(
            worker
                .reconcile(&request.operation_id, &input)
                .await
                .unwrap(),
            Reconciliation::Completed(output)
        );
    }

    #[tokio::test]
    async fn run_expiry_stays_a_deadline_and_unconfirmed_cleanup_stays_an_error() {
        for (mode, run_expired) in [("run-expired", true), ("cleanup-fails", false)] {
            let (_data, _workspace, worker, request) = fixture(mode, run_expired).await;
            let error = worker
                .dispatch_local(
                    &request.operation_id,
                    &serde_json::to_string(&request).unwrap(),
                    CancellationToken::new(),
                )
                .await
                .unwrap_err();
            if run_expired {
                assert!(matches!(error, Error::Deadline), "{error:?}");
            } else {
                assert!(
                    error.to_string().contains("cleanup could not be confirmed"),
                    "{error}"
                );
            }
        }
    }

    #[tokio::test]
    async fn quality_output_and_recovered_logs_keep_only_the_tail() {
        let (_data, _workspace, worker, mut request) = fixture("output", false).await;
        request.deadline_at += chrono::Duration::seconds(10);
        let output = worker
            .dispatch_local(
                &request.operation_id,
                &serde_json::to_string(&request).unwrap(),
                CancellationToken::new(),
            )
            .await
            .unwrap();
        let check: CheckResult = serde_json::from_str(&output).unwrap();
        assert!(check.passed());
        assert!(check.output_tail.len() <= 65536);
        assert!(check.output_tail.ends_with("last: é☃"));
        let mut receipt = worker.receipt(&request.operation_id).unwrap().unwrap();
        let recovered: CheckResult =
            serde_json::from_str(&worker.recover_output(&mut receipt).await.unwrap().unwrap())
                .unwrap();
        assert_eq!(recovered.output_tail, check.output_tail);
    }
}
