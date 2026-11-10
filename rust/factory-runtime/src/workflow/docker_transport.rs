//! Docker-specific provider configuration, mounts, process control and cleanup.
use super::*;

/// Trusted daemon-owned broker, separate from the replaceable HTTP controller.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct BrokerConfig {
    pub image: String,
    pub socket_gid: u32,
}
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
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub broker: Option<BrokerConfig>,
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
            broker: None,
        }
    }
}

impl ExecutionWorker {
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
        if docker
            .broker
            .as_ref()
            .is_some_and(|broker| broker.image.trim().is_empty())
        {
            return Err(Error::Config("daemon broker image is required".into()));
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
        Self::open_backend(config, Backend::Docker(docker))
    }
    pub(super) async fn output(&self, args: &[String]) -> Result<String> {
        let output = tokio::time::timeout(
            Duration::from_secs(30),
            Command::new(&self.docker().executable)
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
    pub(super) async fn quality_logs_docker(&self, receipt: &WorkerReceipt) -> Result<String> {
        tokio::time::timeout(Duration::from_secs(30), async {
            let mut child = Command::new(&self.docker().executable)
                .args([
                    "container",
                    "logs",
                    "--tail",
                    "2000",
                    receipt.container_id.as_deref().unwrap(),
                ])
                .stdin(Stdio::null())
                .stdout(Stdio::piped())
                .stderr(Stdio::piped())
                .kill_on_drop(true)
                .spawn()?;
            let (output, error) = tokio::try_join!(
                read_quality_tail(child.stdout.take().unwrap()),
                read_quality_tail(child.stderr.take().unwrap()),
            )?;
            if !child.wait().await?.success() {
                return Err(Error::Worker(error));
            }
            Ok(output)
        })
        .await
        .map_err(|_| Error::Worker("Docker quality logs timed out".into()))?
    }
    pub(super) async fn inspect_docker(&self, receipt: &WorkerReceipt) -> Result<Option<Value>> {
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
    pub(super) async fn stop_docker(&self, receipt: &mut WorkerReceipt) -> Result<()> {
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
    pub(super) fn mount(
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
    pub(super) async fn create_args(&self, receipt: &WorkerReceipt) -> Result<Vec<String>> {
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
            format!("{}:{}", self.docker().uid, self.docker().gid),
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
            format!("seccomp={}", self.docker().seccomp_profile.display()),
        ]);
        args.extend([
            "--mount".into(),
            self.mount(
                &self.docker().daemon_workspace,
                &request.workspace,
                request.read_only(),
                self.docker().workspace_volume.as_deref(),
                None,
            )?,
        ]);
        args.extend([
            "--mount".into(),
            self.mount(
                &self.docker().daemon_data_dir.join(&evidence_relative),
                &request.evidence_directory,
                true,
                self.docker().data_volume.as_deref(),
                Some(&evidence_relative),
            )?,
        ]);
        args.extend([
            "--mount".into(),
            self.mount(
                &self.docker().daemon_data_dir.join(&home_relative),
                "/factory/provider",
                false,
                self.docker().data_volume.as_deref(),
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
                ".factory-rust.service.lock",
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
                self.docker().daemon_workspace.join(relative)
            } else {
                // External linked-worktree metadata requires an explicit same-path
                // bind. Refuse a remapped/named workspace until operator maps it.
                if self.docker().workspace_volume.is_some()
                    || self.docker().daemon_workspace != self.config.workspace()
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
                    relative.and(self.docker().workspace_volume.as_deref()),
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
                    self.docker().image.clone(),
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
                if let Some(auth) = &self.docker().daemon_auth_file {
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
                args.push(self.docker().image.clone());
                args.extend([
                    "-c".into(),
                    format!("permissions.factory.filesystem={table}"),
                    "-c".into(),
                    "permissions.factory.network.enabled=false".into(),
                    "-c".into(),
                    format!("approval_policy={}", json!(self.approval_policy())),
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
    pub fn backend(&self) -> &DockerConfig {
        self.docker()
    }
    pub async fn pin_image(mut docker: DockerConfig) -> Result<DockerConfig> {
        let references = std::iter::once(&mut docker.image)
            .chain(docker.broker.iter_mut().map(|broker| &mut broker.image));
        for reference in references {
            let result = tokio::time::timeout(
                Duration::from_secs(30),
                Command::new(&docker.executable)
                    .args([
                        "image",
                        "inspect",
                        "--format",
                        "{{.Id}}",
                        reference.as_str(),
                    ])
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
            *reference = image;
        }
        Ok(docker)
    }
}
