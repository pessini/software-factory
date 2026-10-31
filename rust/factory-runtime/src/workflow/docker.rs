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
    io::{AsyncBufReadExt, AsyncWriteExt, BufReader},
    process::{Child, ChildStdin, ChildStdout, Command},
};
use tokio_util::sync::CancellationToken;

const OPERATION: &str = "software-factory.operation";
const OWNER: &str = "software-factory.controller";
const WORKSPACE: &str = "software-factory.workspace";
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct DockerConfig {
    pub executable: PathBuf,
    pub image: String,
    /// Paths as seen by the daemon, which can differ from controller paths.
    pub daemon_workspace: PathBuf,
    pub daemon_data_dir: PathBuf,
    /// Optional named volumes; only evidence/provider subdirectories are mounted.
    pub workspace_volume: Option<String>,
    pub data_volume: Option<String>,
    pub daemon_auth_file: Option<PathBuf>,
    pub uid: u32,
    pub gid: u32,
    #[serde(default = "seccomp_profile")]
    pub seccomp_profile: PathBuf,
    #[serde(default = "never")]
    pub approval_policy: String,
    pub seccomp_sha256: Option<String>,
}
fn seccomp_profile() -> PathBuf {
    PathBuf::from(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../containers/worker-seccomp.json"
    ))
}
fn never() -> String {
    "never".into()
}
impl DockerConfig {
    pub fn local(config: &Config, auth: Option<PathBuf>) -> Self {
        use std::os::unix::fs::MetadataExt;
        let meta = fs::metadata(config.workspace()).expect("validated workspace");
        Self {
            executable: "docker".into(),
            image: "software-factory-worker:0.154.0".into(),
            daemon_workspace: config.workspace().into(),
            daemon_data_dir: config.data_dir().into(),
            workspace_volume: None,
            data_volume: None,
            daemon_auth_file: auth,
            uid: meta.uid(),
            gid: meta.gid(),
            seccomp_profile: seccomp_profile(),
            approval_policy: never(),
            seccomp_sha256: None,
        }
    }
}
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WorkerReceipt {
    pub version: u32,
    pub operation_id: String,
    pub request_hash: String,
    pub request: ExecutionRequest,
    pub container_name: String,
    pub container_id: Option<String>,
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

pub struct DockerWorker {
    config: Config,
    docker: DockerConfig,
    owner: String,
    identity: Vec<u8>,
}
impl DockerWorker {
    pub fn open(config: Config, mut docker: DockerConfig) -> Result<Self> {
        for path in [&docker.daemon_workspace, &docker.daemon_data_dir] {
            if !path.is_absolute() || path.to_string_lossy().contains([',', '\n', '\0']) {
                return Err(Error::Config("Docker bind sources must be explicit absolute daemon-visible paths without commas".into()));
            }
        }
        if docker.uid == 0 || docker.gid == 0 {
            return Err(Error::Config(
                "protected Docker workers must use a non-root uid/gid".into(),
            ));
        }
        if docker.image.trim().is_empty() {
            return Err(Error::Config("worker image is required".into()));
        }
        if !["never", "on-request", "untrusted"].contains(&docker.approval_policy.as_str()) {
            return Err(Error::Config(
                "worker approval policy is invalid; all approval requests are denied".into(),
            ));
        }
        let profile = regular(&docker.seccomp_profile)?;
        let _: Value = serde_json::from_slice(&profile)?;
        let profile_hash = digest(&profile);
        if docker
            .seccomp_sha256
            .as_ref()
            .is_some_and(|saved| saved != &profile_hash)
        {
            return Err(Error::Config("saved worker seccomp profile changed".into()));
        }
        docker.seccomp_sha256 = Some(profile_hash);
        for name in [&docker.workspace_volume, &docker.data_volume]
            .into_iter()
            .flatten()
        {
            if name.is_empty()
                || !name
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b"._-".contains(&b))
            {
                return Err(Error::Config("invalid Docker volume name".into()));
            }
        }
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
            docker,
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
        Ok(Some(receipt))
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
    async fn output(&self, args: &[String]) -> Result<String> {
        let output = tokio::time::timeout(
            Duration::from_secs(30),
            Command::new(&self.docker.executable)
                .args(args)
                .kill_on_drop(true)
                .output(),
        )
        .await
        .map_err(|_| {
            Error::Worker("Docker control command timed out; reconcile its labels".into())
        })??;
        if !output.status.success() {
            return Err(Error::Worker(
                String::from_utf8_lossy(&output.stderr).trim().into(),
            ));
        }
        Ok(String::from_utf8_lossy(&output.stdout).into_owned())
    }
    async fn inspect(&self, receipt: &WorkerReceipt) -> Result<Option<Value>> {
        // `docker inspect` failures are not evidence of absence. A successful
        // label/name listing must prove absence first, including creation races.
        let ids = self
            .output(&[
                "container".into(),
                "ls".into(),
                "-aq".into(),
                "--no-trunc".into(),
                "--filter".into(),
                format!("name=^/{}$", receipt.container_name),
            ])
            .await?;
        let ids: Vec<_> = ids.lines().filter(|s| !s.is_empty()).collect();
        if ids.is_empty() {
            return Ok(None);
        }
        if ids.len() != 1 {
            return Err(Error::Worker("ambiguous Docker identity".into()));
        }
        let values: Vec<Value> = serde_json::from_str(
            &self
                .output(&["container".into(), "inspect".into(), ids[0].into()])
                .await?,
        )?;
        let value = values
            .first()
            .ok_or_else(|| Error::Worker("Docker returned no inspect record".into()))?;
        if values.len() != 1
            || value["Id"].as_str() != Some(ids[0])
            || receipt
                .container_id
                .as_deref()
                .is_some_and(|id| Some(id) != value["Id"].as_str())
            || value["Config"]["Labels"][OPERATION] != receipt.operation_id
            || value["Config"]["Labels"][OWNER] != self.owner
            || value["Config"]["Labels"][WORKSPACE]
                != digest(self.config.workspace().as_os_str().as_encoded_bytes())
        {
            return Err(Error::Worker(
                "container ID/labels differ from the saved operation; refusing to alter it".into(),
            ));
        }
        if receipt
            .image_id
            .as_deref()
            .is_some_and(|id| value["Image"].as_str() != Some(id))
        {
            return Err(Error::Worker(
                "container image differs from its saved receipt".into(),
            ));
        }
        Ok(Some(value.clone()))
    }
    async fn stop(&self, receipt: &mut WorkerReceipt) -> Result<()> {
        if let Some(current) = self.inspect(receipt).await? {
            receipt.container_id = current["Id"].as_str().map(String::from);
            self.save(receipt)?;
            if current["State"]["Running"] == true {
                // An attached App Server may exit between inspect and kill.
                // Only the following inspection decides whether cleanup succeeded.
                let _ = self
                    .output(&[
                        "container".into(),
                        "kill".into(),
                        "--signal".into(),
                        "KILL".into(),
                        receipt.container_id.clone().unwrap(),
                    ])
                    .await;
            }
            let after = self.inspect(receipt).await?.ok_or_else(|| {
                Error::Worker("container disappeared before cleanup confirmation".into())
            })?;
            if after["State"]["Running"] != false || after["State"]["Pid"] != 0 {
                return Err(Error::Worker(
                    "Docker process-tree cleanup could not be confirmed".into(),
                ));
            }
        } else if receipt.creation_requested && !receipt.stopped {
            return Err(Error::Worker("Docker creation or disappearance is uncertain; retain the workspace claim until its exact identity can be reconciled".into()));
        }
        receipt.stopped = true;
        self.save(receipt)
    }
    fn mount(
        &self,
        source: &Path,
        target: &str,
        read_only: bool,
        volume: Option<&str>,
        subpath: Option<&Path>,
    ) -> Result<String> {
        for path in [source.to_string_lossy().as_ref(), target] {
            if path.contains([',', '\n', '\0']) {
                return Err(Error::Config("Docker mount contains delimiters".into()));
            }
        }
        let mount = if let Some(volume) = volume {
            format!(
                "type=volume,src={volume},dst={target}{}",
                subpath
                    .map(|p| format!(",volume-subpath={}", p.display()))
                    .unwrap_or_default()
            )
        } else {
            format!("type=bind,src={},dst={target}", source.display())
        };
        Ok(format!(
            "{mount}{}",
            if read_only { ",readonly" } else { "" }
        ))
    }
    async fn create_args(&self, receipt: &WorkerReceipt) -> Result<Vec<String>> {
        let request = &receipt.request;
        let evidence_relative = PathBuf::from("evidence").join(&request.run_id);
        let home_relative = self.home_relative(request);
        fs::create_dir_all(self.config.data_dir().join(&home_relative))?;
        let mut args = vec![
            "container".into(),
            "create".into(),
            "--name".into(),
            receipt.container_name.clone(),
            "--label".into(),
            format!("{OPERATION}={}", receipt.operation_id),
            "--label".into(),
            format!("{OWNER}={}", self.owner),
            "--label".into(),
            format!(
                "{WORKSPACE}={}",
                digest(self.config.workspace().as_os_str().as_encoded_bytes())
            ),
            "--interactive".into(),
            "--init".into(),
            "--read-only".into(),
            "--cap-drop=ALL".into(),
            "--security-opt=no-new-privileges".into(),
            "--pids-limit=256".into(),
            "--memory=2g".into(),
            "--cpus=2".into(),
            "--user".into(),
            format!("{}:{}", self.docker.uid, self.docker.gid),
            "--tmpfs".into(),
            "/tmp:rw,nosuid,nodev,mode=1777,size=512m".into(),
            "--workdir".into(),
            request.workspace.clone(),
            "--env".into(),
            "CODEX_HOME=/factory/provider".into(),
            "--env".into(),
            "HOME=/factory/provider".into(),
            "--env".into(),
            "UV_CACHE_DIR=/tmp/uv-cache".into(),
            "--env".into(),
            "PYTHONDONTWRITEBYTECODE=1".into(),
            "--env".into(),
            "GIT_OPTIONAL_LOCKS=0".into(),
        ];
        args.extend([
            "--security-opt".into(),
            format!("seccomp={}", self.docker.seccomp_profile.display()),
        ]);
        args.extend([
            "--mount".into(),
            self.mount(
                &self.docker.daemon_workspace,
                &request.workspace,
                request.read_only(),
                self.docker.workspace_volume.as_deref(),
                None,
            )?,
        ]);
        args.extend([
            "--mount".into(),
            self.mount(
                &self.docker.daemon_data_dir.join(&evidence_relative),
                &request.evidence_directory,
                true,
                self.docker.data_volume.as_deref(),
                Some(&evidence_relative),
            )?,
        ]);
        args.extend([
            "--mount".into(),
            self.mount(
                &self.docker.daemon_data_dir.join(&home_relative),
                "/factory/provider",
                false,
                self.docker.data_volume.as_deref(),
                Some(&home_relative),
            )?,
        ]);
        // Bind .git itself, worktree/common metadata and every existing protected
        // path over the writable workspace. Missing paths are caught after the turn.
        let mut protected: Vec<PathBuf> = request
            .protected_paths
            .iter()
            .map(|p| Path::new(&request.workspace).join(p))
            .collect();
        protected.extend(
            [
                ".factory-rust.identity",
                ".factory-rust.claim",
                ".factory-rust.lock",
            ]
            .map(|p| self.config.workspace().join(p)),
        );
        protected.push(Path::new(&request.workspace).join(".git"));
        for flag in ["--git-dir", "--git-common-dir"] {
            let raw = repository::git(
                self.config.workspace(),
                &["rev-parse", "--path-format=absolute", flag],
            )
            .await?;
            protected.push(PathBuf::from(String::from_utf8_lossy(&raw).trim()));
        }
        let ignored = repository::git(
            self.config.workspace(),
            &[
                "ls-files",
                "--others",
                "--ignored",
                "--exclude-standard",
                "--directory",
                "-z",
            ],
        )
        .await?;
        protected.extend(
            ignored
                .split(|b| *b == 0)
                .filter(|s| !s.is_empty())
                .map(|p| {
                    self.config
                        .workspace()
                        .join(String::from_utf8_lossy(p).trim_end_matches('/'))
                }),
        );
        protected.sort();
        protected.dedup();
        for path in &protected {
            if !path.exists() && !path.is_symlink() {
                continue;
            }
            if path.is_symlink() {
                return Err(Error::Config(
                    "protected mount cannot be a symbolic link".into(),
                ));
            }
            let source = if let Ok(relative) = path.strip_prefix(self.config.workspace()) {
                self.docker.daemon_workspace.join(relative)
            } else {
                // External linked-worktree metadata requires an explicit same-path
                // bind. Refuse a remapped/named workspace until operator maps it.
                if self.docker.workspace_volume.is_some()
                    || self.docker.daemon_workspace != self.config.workspace()
                {
                    return Err(Error::Config(
                        "external Git metadata needs same-path daemon-visible binds".into(),
                    ));
                }
                path.clone()
            };
            let relative = path.strip_prefix(self.config.workspace()).ok();
            args.extend([
                "--mount".into(),
                self.mount(
                    &source,
                    &path.to_string_lossy(),
                    true,
                    relative.and(self.docker.workspace_volume.as_deref()),
                    relative,
                )?,
            ]);
        }
        match &request.execution {
            ExecutionKind::Quality { command, .. } => {
                let wrapper = "import os,sys,json; root,token,argv=json.loads(sys.argv[1]); identity=open(os.path.join(root,'.factory-rust.identity'),'rb').read(); assert identity.hex()==token, 'FACTORY_MOUNT_MISMATCH'; os.execvp(argv[0],argv)";
                args.extend([
                    "--network=none".into(),
                    "--entrypoint".into(),
                    "python3".into(),
                    self.docker.image.clone(),
                    "-B".into(),
                    "-c".into(),
                    wrapper.into(),
                    json!([
                        request.workspace,
                        self.identity
                            .iter()
                            .map(|b| format!("{b:02x}"))
                            .collect::<String>(),
                        command.argv
                    ])
                    .to_string(),
                ]);
            }
            ExecutionKind::Specialist { .. } => {
                if let Some(auth) = &self.docker.daemon_auth_file {
                    args.extend([
                        "--mount".into(),
                        self.mount(auth, "/factory/provider/auth.json", true, None, None)?,
                    ]);
                }
                let mut rules = serde_json::Map::new();
                rules.insert(":root".into(), json!("read"));
                rules.insert(
                    request.workspace.clone(),
                    json!(if request.read_only() { "read" } else { "write" }),
                );
                rules.insert("/tmp".into(), json!("write"));
                rules.insert("/factory/provider/auth.json".into(), json!("deny"));
                for path in protected {
                    if path.exists() {
                        rules.insert(path.to_string_lossy().into_owned(), json!("read"));
                    }
                }
                let table = format!(
                    "{{{}}}",
                    rules
                        .iter()
                        .map(|(p, a)| format!("{}={}", json!(p), a))
                        .collect::<Vec<_>>()
                        .join(",")
                );
                args.push(self.docker.image.clone());
                args.extend([
                    "-c".into(),
                    format!("permissions.factory.filesystem={table}"),
                    "-c".into(),
                    "permissions.factory.network.enabled=false".into(),
                    "-c".into(),
                    format!("approval_policy={}", json!(self.docker.approval_policy)),
                    "-c".into(),
                    "default_permissions=\"factory\"".into(),
                    "-c".into(),
                    "features.multi_agent=false".into(),
                    "-c".into(),
                    "features.apps=false".into(),
                    "-c".into(),
                    "web_search=\"disabled\"".into(),
                ]);
            }
        }
        Ok(args)
    }
    fn home_relative(&self, request: &ExecutionRequest) -> PathBuf {
        let role = match &request.execution {
            ExecutionKind::Specialist { role, .. } => role.as_str(),
            _ => "quality",
        };
        PathBuf::from("provider-homes")
            .join(digest(format!("{}:{role}", request.run_id).as_bytes()))
    }
    fn previous_thread(&self, request: &ExecutionRequest) -> Result<Option<String>> {
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
    async fn run(&self, receipt: &mut WorkerReceipt, cancel: CancellationToken) -> Result<String> {
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
        let child = Command::new(&self.docker.executable)
            .args(["container", "start", "--attach", "--interactive", &id])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .kill_on_drop(true)
            .spawn()?;
        let execution = receipt.request.execution.clone();
        let outcome = match execution {
            ExecutionKind::Quality {
                command,
                content_version,
            } => {
                let output = child.wait_with_output().await?;
                let current = self
                    .inspect(receipt)
                    .await?
                    .ok_or_else(|| Error::Worker("quality container disappeared".into()))?;
                serde_json::to_string(&CheckResult {
                    name: command.name,
                    argv: command.argv,
                    exit_code: current["State"]["ExitCode"]
                        .as_i64()
                        .and_then(|n| i32::try_from(n).ok())
                        .or(output.status.code()),
                    timed_out: false,
                    cancelled: false,
                    output_tail: tail(&String::from_utf8_lossy(&output.stdout), 65536),
                    content_version,
                    comparison: String::new(),
                })
                .map_err(Error::from)
            }
            ExecutionKind::Specialist { agent, prompt, .. } => {
                let mut rpc = Rpc::new(child)?;
                let result=async {
                    let initialized=rpc.request("initialize",json!({"clientInfo":{"name":"software-factory-rust-worker","version":"1"},"capabilities":{"experimentalApi":true}})).await?;
                    let marker=Path::new(&receipt.request.workspace).join(".factory-rust.identity");
                    let verify="import sys; assert open(sys.argv[1],'rb').read().hex()==sys.argv[2], 'FACTORY_MOUNT_MISMATCH'";
                    self.output(&["container".into(),"exec".into(),receipt.container_id.clone().unwrap(),"python3".into(),"-B".into(),"-c".into(),verify.into(),marker.to_string_lossy().into_owned(),self.identity.iter().map(|b|format!("{b:02x}")).collect()]).await?;
                    if !initialized["userAgent"].as_str().is_some_and(|s|s.contains("/0.154.0 ")) {return Err(Error::Config("worker App Server version differs from pinned 0.154.0".into()));}
                    rpc.send(&json!({"method":"initialized"})).await?;
                    let previous=self.previous_thread(&receipt.request)?;
                    let mut params=json!({"model":agent.model,"cwd":receipt.request.workspace,"permissions":"factory","approvalPolicy":self.docker.approval_policy,"config":{"model_reasoning_effort":agent.reasoning_level}});
                    let method=if let Some(previous)=previous {params["threadId"]=json!(previous);"thread/resume"}else {params["selectedCapabilityRoots"]=json!([]);params["allowProviderModelFallback"]=json!(false);"thread/start"};
                    let started=rpc.request(method,params).await?;
                    if started["model"]!=agent.model || started.get("reasoningEffort").is_some_and(|v|!v.is_null() && *v!=agent.reasoning_level) {return Err(Error::Config("provider substituted the saved model or effort".into()));}
                    if started["approvalPolicy"]!=self.docker.approval_policy{return Err(Error::Config("provider changed the recorded approval policy".into()));}
                    receipt.reported=started["reasoningEffort"].as_str().map(|_|agent.clone());
                    receipt.provider_thread_id=started["thread"]["id"].as_str().map(String::from);
                    if receipt.provider_thread_id.is_none(){return Err(Error::Worker("thread response has no provider identity".into()));}
                    self.save(receipt)?;
                    receipt.turn_requested=true;self.save(receipt)?;
                    let turn=rpc.request("turn/start",json!({"threadId":receipt.provider_thread_id,"clientUserMessageId":receipt.operation_id,"input":[{"type":"text","text":prompt}],"model":agent.model,"effort":agent.reasoning_level,"permissions":"factory","approvalPolicy":self.docker.approval_policy})).await?;
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
                let output = self
                    .output(&[
                        "container".into(),
                        "logs".into(),
                        "--tail".into(),
                        "2000".into(),
                        receipt.container_id.clone().unwrap(),
                    ])
                    .await?;
                Ok(Some(serde_json::to_string(&CheckResult {
                    name: command.name.clone(),
                    argv: command.argv.clone(),
                    exit_code: current["State"]["ExitCode"]
                        .as_i64()
                        .and_then(|n| i32::try_from(n).ok()),
                    timed_out: false,
                    cancelled: false,
                    output_tail: tail(&output, 65536),
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
impl Worker for DockerWorker {
    async fn model_catalog(&self) -> Result<Vec<super::config::ModelOption>> {
        self.catalog().await
    }
    async fn reconcile(&self, operation: &str, input: &str) -> Result<Reconciliation> {
        let request = self.decode(operation, input)?;
        if self.broker_lock(operation)?.is_none() {
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
            let provisional = WorkerReceipt {
                version: 1,
                operation_id: operation.into(),
                request_hash: digest(input.as_bytes()),
                request,
                container_name: self.container_name(operation),
                container_id: None,
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
            };
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
            receipt.output = self.recover_output(&mut receipt).await?;
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
        }
        Ok(())
    }
}
struct Rpc {
    child: Child,
    input: ChildStdin,
    lines: tokio::io::Lines<BufReader<ChildStdout>>,
    sequence: u64,
    events: VecDeque<Value>,
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
            lines: BufReader::new(stdout).lines(),
            sequence: 0,
            events: VecDeque::new(),
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
        let line = self.lines.next_line().await?.ok_or_else(|| {
            Error::Worker("provider connection closed before its saved turn completed".into())
        })?;
        if line.len() > 16 * 1024 * 1024 {
            return Err(Error::Worker("provider event exceeds limit".into()));
        }
        Ok(serde_json::from_str(&line)?)
    }
    async fn next(&mut self) -> Result<Value> {
        match self.events.pop_front() {
            Some(event) => Ok(event),
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
            self.events.push_back(event);
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

impl DockerWorker {
    /// Catalog inspection makes no coding turn and never selects a fallback.
    pub async fn catalog(&self) -> Result<Vec<super::config::ModelOption>> {
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
        let mut receipt = WorkerReceipt {
            version: 1,
            operation_id: request.operation_id.clone(),
            request_hash: digest(serde_json::to_string(&request)?.as_bytes()),
            container_name: self.container_name(&request.operation_id),
            request,
            container_id: None,
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
        };
        self.save(&receipt)?;
        let result=tokio::time::timeout(Duration::from_secs(60),async {
            let args=self.create_args(&receipt).await?;receipt.creation_requested=true;self.save(&receipt)?;
            let id=self.output(&args).await?.trim().to_owned();receipt.container_id=Some(id.clone());self.save(&receipt)?;
            let child=Command::new(&self.docker.executable).args(["container","start","--attach","--interactive",&id]).stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::null()).kill_on_drop(true).spawn()?;
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
        if let Some(id) = &receipt.container_id {
            self.output(&["container".into(), "rm".into(), id.clone()])
                .await?;
        }
        result
    }
}

impl DockerWorker {
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
            let receipt = WorkerReceipt {
                version: 1,
                operation_id: operation.into(),
                request_hash: digest(input.as_bytes()),
                request,
                container_name: self.container_name(operation),
                container_id: None,
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
            };
            self.save(&receipt)?;
        }
        let receipt = self.receipt(operation)?.unwrap();
        if receipt.creation_requested || receipt.output.is_some() || receipt.stopped {
            return Ok(());
        }
        let backend = self.config.data_dir().join("docker-backend.json");
        if backend.exists() {
            let existing: DockerConfig = serde_json::from_slice(&regular(&backend)?)?;
            if existing != self.docker {
                return Err(Error::Conflict(
                    "worker backend differs from the saved controller backend".into(),
                ));
            }
        } else {
            atomic(&backend, &serde_json::to_vec_pretty(&self.docker)?)?;
        }
        drop(lease);
        let current = std::env::current_exe()?;
        let executable = if current.file_name().is_some_and(|s| s == "factory-runtime") {
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
        let startup = tokio::time::Instant::now() + Duration::from_secs(10);
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
            if self.broker_lock(operation)?.is_some() && tokio::time::Instant::now() > startup {
                return Err(Error::Worker(
                    "worker broker exited; reconcile its exact Docker receipt".into(),
                ));
            }
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
    }
}

impl DockerWorker {
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
        let mut receipt = WorkerReceipt {
            version: 1,
            operation_id: operation.into(),
            request_hash: digest(input.as_bytes()),
            request,
            container_name: self.container_name(operation),
            container_id: None,
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
        };
        self.save(&receipt)?;
        let control = receipt.request.clone();
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
                        if chrono::Utc::now()>=expiry{break Err(Error::Deadline);}
                    }
                }
            }
        };
        // The run future must be dropped before borrowing its receipt for cleanup.
        let mut receipt = self
            .receipt(operation)?
            .ok_or_else(|| Error::Worker("worker receipt disappeared".into()))?;
        match &result {
            Ok(output) => receipt.output = Some(output.clone()),
            Err(error) => receipt.error = Some(error.to_string()),
        };
        self.save(&receipt)?;
        self.stop(&mut receipt).await?;
        result
    }
}

impl DockerWorker {
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

impl DockerWorker {
    pub fn backend(&self) -> &DockerConfig {
        &self.docker
    }
    pub async fn pin_image(mut docker: DockerConfig) -> Result<DockerConfig> {
        let result = tokio::time::timeout(
            Duration::from_secs(30),
            Command::new(&docker.executable)
                .args(["image", "inspect", "--format", "{{.Id}}", &docker.image])
                .kill_on_drop(true)
                .output(),
        )
        .await
        .map_err(|_| Error::Worker("worker image inspection timed out".into()))??;
        if !result.status.success() {
            return Err(Error::Config(
                "pinned worker image must already be installed in the selected daemon".into(),
            ));
        }
        let image = String::from_utf8_lossy(&result.stdout).trim().to_owned();
        if image.len() != 71
            || !image.starts_with("sha256:")
            || !image[7..].bytes().all(|b| b.is_ascii_hexdigit())
        {
            return Err(Error::Config(
                "Docker did not report an immutable worker image identity".into(),
            ));
        }
        docker.image = image;
        Ok(docker)
    }
}
