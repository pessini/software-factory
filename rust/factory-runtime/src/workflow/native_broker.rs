//! Native transport beneath the shared durable broker and App Server protocol.
use super::*;
use std::os::fd::AsRawFd;

impl ExecutionWorker {
    pub fn open_native(config: Config, native: super::super::native::NativeConfig) -> Result<Self> {
        native.validate(&config)?;
        Self::open_backend(config, Backend::Native(native))
    }
    pub(super) fn approval_policy(&self) -> &str {
        match &self.backend {
            Backend::Docker(config) => &config.approval_policy,
            Backend::Native(config) => &config.approval_policy,
        }
    }
    pub(crate) fn persist_backend(&self) -> Result<()> {
        let (name, other, value) = match &self.backend {
            Backend::Docker(config) => (
                "docker-backend.json",
                "native-backend.json",
                serde_json::to_value(config)?,
            ),
            Backend::Native(config) => (
                "native-backend.json",
                "docker-backend.json",
                serde_json::to_value(config)?,
            ),
        };
        if self.config.data_dir().join(other).exists() {
            return Err(Error::Conflict(
                "another execution backend is pinned; use a separate data directory".into(),
            ));
        }
        let path = self.config.data_dir().join(name);
        if path.exists() {
            let saved: Value = serde_json::from_slice(&regular(&path)?)?;
            if saved != value {
                return Err(Error::Conflict(
                    "worker backend differs from saved controller backend".into(),
                ));
            }
        } else {
            atomic(&path, &serde_json::to_vec_pretty(&value)?)?;
        }
        Ok(())
    }
    pub(super) async fn verify_workspace(&self, receipt: &WorkerReceipt) -> Result<()> {
        let marker = Path::new(&receipt.request.workspace).join(".factory-rust.identity");
        if self.native().is_some() {
            if regular(&marker)? != self.identity {
                return Err(Error::Conflict("native workspace identity changed".into()));
            }
            return Ok(());
        }
        let verify = "import sys; assert open(sys.argv[1],'rb').read().hex()==sys.argv[2], 'FACTORY_MOUNT_MISMATCH'";
        self.output(&[
            "container".into(),
            "exec".into(),
            receipt.container_id.clone().unwrap(),
            "python3".into(),
            "-B".into(),
            "-c".into(),
            verify.into(),
            marker.to_string_lossy().into_owned(),
            self.identity.iter().map(|b| format!("{b:02x}")).collect(),
        ])
        .await?;
        Ok(())
    }
    fn cgroup(&self, receipt: &WorkerReceipt) -> Result<PathBuf> {
        let native = self
            .native()
            .ok_or_else(|| Error::Config("native transport unavailable".into()))?;
        Ok(native.cgroup_root.join(format!(
            "factory-{}-{}",
            &self.owner[..16],
            digest(receipt.operation_id.as_bytes())
        )))
    }
    fn cgroup_populated(path: &Path) -> Result<bool> {
        let events = fs::read_to_string(path.join("cgroup.events"))?;
        match events
            .lines()
            .find_map(|line| line.strip_prefix("populated "))
        {
            Some("0") => Ok(false),
            Some("1") => Ok(true),
            _ => Err(Error::Worker(
                "native cgroup population is unknown; retain workspace ownership".into(),
            )),
        }
    }
    pub(super) fn inspect_native(&self, receipt: &WorkerReceipt) -> Result<Option<Value>> {
        let path = self.cgroup(receipt)?;
        if !path.exists() {
            return Ok(None);
        }
        if path.is_symlink() {
            return Err(Error::Worker(
                "native cgroup identity is a symbolic link".into(),
            ));
        }
        Ok(Some(
            json!({"Id": path.display().to_string(), "State": {"Running": Self::cgroup_populated(&path)?}}),
        ))
    }
    pub(super) async fn stop_native(&self, receipt: &mut WorkerReceipt) -> Result<()> {
        let path = self.cgroup(receipt)?;
        if !path.exists() {
            if receipt.creation_requested && !receipt.stopped {
                return Err(Error::Worker(
                    "native cgroup disappeared; cleanup is unconfirmed, retain workspace ownership"
                        .into(),
                ));
            }
        } else {
            if path.is_symlink() {
                return Err(Error::Worker("native cgroup identity changed".into()));
            }
            // The cgroup is retained even when empty: receipts never infer cleanup
            // from a recycled host PID, process-group ID, or missing lock.
            fs::write(path.join("cgroup.kill"), b"1")?;
            let deadline = tokio::time::Instant::now() + Duration::from_secs(10);
            while Self::cgroup_populated(&path)? {
                if tokio::time::Instant::now() >= deadline {
                    return Err(Error::Worker(
                        "native process-tree cleanup unconfirmed".into(),
                    ));
                }
                tokio::time::sleep(Duration::from_millis(50)).await;
            }
        }
        receipt.stopped = true;
        self.save(receipt)
    }
    async fn native_protected(&self, request: &ExecutionRequest) -> Result<Vec<PathBuf>> {
        let snapshot =
            repository::snapshot(self.config.workspace(), &request.protected_paths).await?;
        let mut protected: Vec<PathBuf> = snapshot
            .protected
            .keys()
            .map(|p| self.config.workspace().join(p))
            .collect();
        protected.extend(
            snapshot
                .ignored
                .keys()
                .map(|p| self.config.workspace().join(p)),
        );
        protected.extend(
            [
                ".git",
                ".codex",
                ".agents",
                ".factory-rust.identity",
                ".factory-rust.claim",
                ".factory-rust.lock",
                ".factory-rust.service.lock",
            ]
            .map(|p| self.config.workspace().join(p)),
        );
        for flag in ["--git-dir", "--git-common-dir"] {
            let raw = repository::git(
                self.config.workspace(),
                &["rev-parse", "--path-format=absolute", flag],
            )
            .await?;
            protected.push(PathBuf::from(String::from_utf8_lossy(&raw).trim()));
        }
        protected.sort();
        protected.dedup();
        for path in &protected {
            if path.exists() || path.is_symlink() {
                if path.is_symlink() || path.canonicalize()? != *path {
                    return Err(Error::Config(
                        "native protected paths cannot contain symbolic links".into(),
                    ));
                }
            } else if !request.read_only() {
                // A mount cannot protect an absent path. Native builders use an
                // explicit leaf allowlist below, leaving parents read-only.
                continue;
            }
        }
        Ok(protected)
    }
    /// Build with a read-only workspace and bind only explicitly selected
    /// application leaves writable. Parent directories containing protected or
    /// absent policy names remain read-only, preventing rename/delete bypasses.
    fn native_writable(&self, protected: &[PathBuf]) -> Result<Vec<PathBuf>> {
        fn visit(path: &Path, protected: &[PathBuf], out: &mut Vec<PathBuf>) -> Result<()> {
            if protected.iter().any(|p| path.starts_with(p)) {
                return Ok(());
            }
            if protected.iter().any(|p| p.starts_with(path)) {
                if path.is_dir() && !path.is_symlink() {
                    for entry in fs::read_dir(path)? {
                        visit(&entry?.path(), protected, out)?;
                    }
                }
                return Ok(());
            }
            if path.is_symlink() {
                // Symlinks remain read-only. Their destinations are governed by
                // their own mount permissions in the private filesystem view.
                return Ok(());
            }
            out.push(path.to_path_buf());
            Ok(())
        }
        let mut writable = vec![];
        for path in &self.native().unwrap().writable_paths {
            visit(
                &self.config.workspace().join(path),
                protected,
                &mut writable,
            )?;
        }
        fn reject_hardlinks(path: &Path) -> Result<()> {
            use std::os::unix::fs::MetadataExt;
            let metadata = fs::symlink_metadata(path)?;
            if metadata.is_file() && metadata.nlink() > 1 {
                return Err(Error::Config(
                    "native writable application files cannot have hard links to another path"
                        .into(),
                ));
            }
            if metadata.is_dir() {
                for entry in fs::read_dir(path)? {
                    reject_hardlinks(&entry?.path())?;
                }
            }
            Ok(())
        }
        for path in &writable {
            reject_hardlinks(path)?;
        }
        writable.sort();
        writable.dedup();
        Ok(writable)
    }
    pub(super) async fn start_native(&self, receipt: &mut WorkerReceipt) -> Result<Child> {
        let native = self.native().unwrap();
        native.validate(&self.config)?;
        self.verify_workspace(receipt).await?;
        let protected = self.native_protected(&receipt.request).await?;
        let writable = if receipt.request.read_only() {
            vec![]
        } else {
            self.native_writable(&protected)?
        };
        let home = self
            .config
            .data_dir()
            .join(self.home_relative(&receipt.request));
        fs::create_dir_all(&home)?;
        let mut rules = serde_json::Map::new();
        rules.insert(":root".into(), json!("read"));
        rules.insert("/tmp".into(), json!("write"));
        rules.insert("/factory/provider".into(), json!("deny"));
        if !receipt.request.read_only() {
            // 0.154.0 treats every write root as a directory and cannot use
            // individual writable files as provider roots. The outer mount
            // namespace enforces the narrower reviewed leaf/file allowlist.
            rules.insert(receipt.request.workspace.clone(), json!("write"));
            for path in &protected {
                if path.exists() {
                    rules.insert(path.display().to_string(), json!("read"));
                }
            }
        }
        let policy = json!({"version":1,"private_workspace_root":true,"private_metadata_targets":[".codex",".agents"],"provider":"codex","provider_version":"0.154.0","filesystem":rules,"writable_paths":writable,"protected_paths":protected,"tool_network":false,"approvals":"deny_all","approval_policy":native.approval_policy,"quality_environment":{"PATH":"/usr/bin:/bin","HOME":"/tmp","UV_CACHE_DIR":"/tmp/uv-cache","PYTHONDONTWRITEBYTECODE":"1","GIT_OPTIONAL_LOCKS":"0"}});
        if receipt
            .effective_policy
            .as_ref()
            .is_some_and(|saved| saved != &policy)
        {
            return Err(Error::Conflict(
                "native session effective policy changed; create a new session".into(),
            ));
        }
        receipt.effective_policy = Some(policy);
        // Save launch intent/policy before creating a process. An interrupted
        // spawn is ambiguous and is never blindly resubmitted.
        receipt.creation_requested = true;
        self.save(receipt)?;
        let cgroup = self.cgroup(receipt)?;
        if cgroup.exists() {
            return Err(Error::Worker(
                "native operation cgroup already exists; reconcile the saved operation".into(),
            ));
        }
        fs::create_dir(&cgroup)?;
        if !cgroup.join("cgroup.kill").is_file() {
            return Err(Error::Config("native host lacks cgroup.kill".into()));
        }
        let procs = fs::OpenOptions::new()
            .write(true)
            .open(cgroup.join("cgroup.procs"))?;
        let fd = procs.as_raw_fd();
        let mut command = Command::new(&native.bubblewrap);
        command.env_clear().args([
            "--unshare-user",
            "--unshare-pid",
            "--unshare-ipc",
            "--unshare-uts",
            "--die-with-parent",
            "--new-session",
            "--cap-drop",
            "ALL",
        ]);
        for root in &native.runtime_roots {
            command.arg("--ro-bind").arg(root).arg(root);
        }
        // Recreate usr-merge compatibility links without exposing any additional
        // host directory. Non-usr-merged hosts must review explicit runtime roots.
        for (path, target) in [
            ("/bin", "usr/bin"),
            ("/sbin", "usr/sbin"),
            ("/lib", "usr/lib"),
            ("/lib64", "usr/lib64"),
        ] {
            if fs::read_link(path).ok().as_deref() == Some(Path::new(target)) {
                command.args(["--symlink", target, path]);
            }
        }

        // Only reviewed runtime directories and selected public resolver/CA files
        // are visible. No host home, /run sockets, controller data or host /proc.
        for path in [
            "/etc/ssl/certs",
            "/etc/resolv.conf",
            "/etc/hosts",
            "/etc/nsswitch.conf",
            "/etc/passwd",
            "/etc/group",
        ] {
            if Path::new(path).exists() {
                command.args(["--ro-bind", path, path]);
            }
        }
        command
            .args([
                "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--dir", "/factory",
            ])
            .arg("--tmpfs")
            .arg(self.config.workspace());
        // A private workspace root supplies empty metadata mount targets needed
        // by 0.154.0's inner sandbox, without creating anything on the host.
        // Every real entry remains a read-only bind until explicitly overlaid.
        for entry in fs::read_dir(self.config.workspace())? {
            let path = entry?.path();
            if path.is_symlink() {
                command
                    .arg("--symlink")
                    .arg(fs::read_link(&path)?)
                    .arg(&path);
            } else {
                command.arg("--ro-bind").arg(&path).arg(&path);
            }
        }
        for name in [".codex", ".agents"] {
            let path = self.config.workspace().join(name);
            if !path.exists() && !path.is_symlink() {
                command.arg("--dir").arg(path);
            }
        }
        for path in &writable {
            command.arg("--bind").arg(path).arg(path);
        }
        for path in [
            &native.executable,
            &native.executable.with_file_name("codex-code-mode-host"),
        ] {
            command.arg("--ro-bind").arg(path).arg(path);
        }

        // External linked-worktree metadata is read-only and never writable.
        for path in &protected {
            if path.exists() && !path.starts_with(self.config.workspace()) {
                command.arg("--ro-bind").arg(path).arg(path);
            }
        }
        command
            .arg("--ro-bind")
            .arg(&receipt.request.evidence_directory)
            .arg(&receipt.request.evidence_directory)
            .arg("--chdir")
            .arg(self.config.workspace())
            .env("PATH", "/usr/bin:/bin")
            .env("UV_CACHE_DIR", "/tmp/uv-cache")
            .env("PYTHONDONTWRITEBYTECODE", "1")
            .env("GIT_OPTIONAL_LOCKS", "0");
        match &receipt.request.execution {
            ExecutionKind::Quality {
                command: quality, ..
            } => {
                command
                    .arg("--remount-ro")
                    .arg(self.config.workspace())
                    .args(["--remount-ro", "/", "--unshare-net"])
                    .env("HOME", "/tmp")
                    .arg("--")
                    .args(&quality.argv);
            }
            ExecutionKind::Specialist { .. } => {
                command.arg("--bind").arg(&home).arg("/factory/provider");
                if let Some(auth) = &native.auth_file {
                    command
                        .arg("--ro-bind")
                        .arg(auth)
                        .arg("/factory/provider/auth.json");
                }
                let table = format!(
                    "{{{}}}",
                    rules
                        .iter()
                        .map(|(p, a)| format!("{}={}", json!(p), a))
                        .collect::<Vec<_>>()
                        .join(",")
                );
                command
                    .arg("--remount-ro")
                    .arg(self.config.workspace())
                    .args(["--remount-ro", "/"])
                    .env("HOME", "/factory/provider")
                    .env("CODEX_HOME", "/factory/provider")
                    .arg("--")
                    .arg(&native.executable)
                    .arg("app-server")
                    .args([
                        "-c",
                        &format!("permissions.factory.filesystem={table}"),
                        "-c",
                        "permissions.factory.network.enabled=false",
                        "-c",
                        &format!("approval_policy={}", json!(native.approval_policy)),
                        "-c",
                        "default_permissions=\"factory\"",
                        "-c",
                        "features.multi_agent=false",
                        "-c",
                        "features.apps=false",
                        "-c",
                        "web_search=\"disabled\"",
                    ]);
            }
        }
        command
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .kill_on_drop(true);
        // Async-signal-safe pre-exec placement: writing 0 migrates the calling
        // child itself. It cannot execute even one tool outside the cgroup.
        let quality = matches!(receipt.request.execution, ExecutionKind::Quality { .. });
        unsafe {
            command.pre_exec(move || {
                if libc::write(fd, b"0".as_ptr().cast(), 1) != 1 {
                    return Err(std::io::Error::last_os_error());
                }
                if quality && libc::dup2(1, 2) == -1 {
                    return Err(std::io::Error::last_os_error());
                }
                Ok(())
            });
        }
        let child = command.spawn()?;
        drop(procs);
        Ok(child)
    }
    fn quality_tail_path(&self, receipt: &WorkerReceipt) -> PathBuf {
        self.path(&receipt.operation_id)
            .with_extension("quality-tail")
    }
    pub(super) fn native_quality_logs(&self, receipt: &WorkerReceipt) -> Result<String> {
        let path = self.quality_tail_path(receipt);
        if !path.exists() {
            return Ok(String::new());
        }
        Ok(tail(
            &String::from_utf8_lossy(&regular(&path)?),
            QUALITY_OUTPUT_LIMIT,
        ))
    }
    pub(super) async fn read_native_quality(
        &self,
        receipt: &WorkerReceipt,
        mut reader: impl AsyncRead + Unpin,
    ) -> Result<String> {
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
            let bytes: Vec<_> = output.iter().copied().collect();
            atomic(&self.quality_tail_path(receipt), &bytes)?;
        }
        Ok(tail(
            &String::from_utf8_lossy(&output.into_iter().collect::<Vec<_>>()),
            QUALITY_OUTPUT_LIMIT,
        ))
    }
}

impl ExecutionWorker {
    /// Load exactly one reviewed backend. No backend inference or fallback.
    pub fn configured(config: Config) -> Result<Option<Self>> {
        let docker = config.data_dir().join("docker-backend.json");
        let native = config.data_dir().join("native-backend.json");
        if docker.exists() && native.exists() {
            return Err(Error::Conflict(
                "multiple execution backends configured; use separate controller storage".into(),
            ));
        }
        if native.exists() {
            return Self::open_native(config, serde_json::from_slice(&regular(&native)?)?)
                .map(Some);
        }
        if docker.exists() {
            return Self::open(config, serde_json::from_slice(&regular(&docker)?)?).map(Some);
        }
        Ok(None)
    }
    pub(crate) fn requires_daemon_broker(&self) -> bool {
        self.daemon_broker()
    }
    pub(crate) async fn probe_native(
        config: Config,
        native: super::super::native::NativeConfig,
    ) -> Result<()> {
        let worker = Self::open_native(config, native)?;
        let run_id = format!(
            "run-native-probe-{}",
            chrono::Utc::now().timestamp_nanos_opt().unwrap_or_default()
        );
        let evidence = worker.config.data_dir().join("evidence").join(&run_id);
        fs::create_dir_all(&evidence)?;
        let request = ExecutionRequest {
            version: 1,
            operation_id: format!("{run_id}-check"),
            run_id,
            workspace: worker.config.workspace().display().to_string(),
            evidence_directory: evidence.display().to_string(),
            protected_paths: vec![],
            deadline_at: chrono::Utc::now() + chrono::Duration::seconds(15),
            execution: ExecutionKind::Quality {
                command: super::super::QualityCommand {
                    name: "native-host-probe".into(),
                    argv: vec!["/usr/bin/true".into()],
                    timeout_seconds: 15,
                    repeat_on_recovery: false,
                },
                content_version: "native-host-probe-v1".into(),
            },
        };
        let provider_request = request.clone();
        let operation = request.operation_id.clone();
        let input = serde_json::to_string(&request)?;
        let output = worker
            .dispatch_local(&operation, &input, CancellationToken::new())
            .await?;
        let result: CheckResult = serde_json::from_str(&output)?;
        if !result.passed() {
            return Err(Error::Config(format!(
                "native namespace/cgroup probe failed: {}",
                result.output_tail
            )));
        }
        worker
            .probe_native_provider(provider_request.clone(), "planner")
            .await?;
        worker
            .probe_native_provider(provider_request, "builder")
            .await
    }
}

impl ExecutionWorker {
    pub(crate) async fn probe_native_provider(
        &self,
        mut request: ExecutionRequest,
        role: &str,
    ) -> Result<()> {
        request.operation_id.push_str(&format!("-provider-{role}"));
        request.execution = ExecutionKind::Specialist {
            role: role.into(),
            step: "catalog".into(),
            agent: super::super::AgentConfig {
                provider: "codex".into(),
                model: "probe-only".into(),
                reasoning_level: "low".into(),
                permission_mode: "auto".into(),
            },
            prompt: String::new(),
        };
        let input = serde_json::to_string(&request)?;
        let mut receipt = self.prepared_receipt(request, &input);
        self.save(&receipt)?;
        let result = tokio::time::timeout(Duration::from_secs(30), async {
            let child = self.start_process(&mut receipt).await?;
            let mut rpc = Rpc::new(child)?;
            let initialized=rpc.request("initialize",json!({"clientInfo":{"name":"factory-native-doctor","version":"1"},"capabilities":{"experimentalApi":true}})).await?;
            if !initialized["userAgent"].as_str().is_some_and(|s|s.contains("/0.154.0 ")) { return Err(Error::Config("native App Server protocol version mismatch".into())); }
            rpc.send(&json!({"method":"initialized"})).await?;
            let response=rpc.request("command/exec",json!({"command":["/usr/bin/true"],"cwd":receipt.request.workspace,"permissionProfile":"factory","timeoutMs":5000,"outputBytesCap":4096})).await?;
            if response["exitCode"] != 0 { return Err(Error::Config(format!("native provider tool sandbox probe failed: {}",response))); }
            let started=rpc.request("thread/start",json!({"cwd":receipt.request.workspace,"permissions":"factory","approvalPolicy":self.approval_policy(),"selectedCapabilityRoots":[],"allowProviderModelFallback":false})).await?;
            if started["activePermissionProfile"]["id"] != "factory" || started["sandbox"]["networkAccess"] != false {
                return Err(Error::Config("native provider session probe did not retain its permission profile".into()));
            }
            receipt.provider_thread_id=started["thread"]["id"].as_str().map(String::from);
            receipt.provider_policy=Some(json!({"profile":started["activePermissionProfile"],"sandbox":started["sandbox"]}));
            Ok(())
        }).await.map_err(|_|Error::Config("native provider tool sandbox probe timed out".into())).and_then(|r|r);
        receipt.error = result.as_ref().err().map(ToString::to_string);
        self.save(&receipt)?;
        self.stop_native(&mut receipt).await?;
        result
    }
}
