//! Linux native execution. Namespace isolation and cgroup v2 cleanup are required;
//! provider permissions alone are never treated as host isolation.
use super::artifacts::regular;
use crate::{Config, Error, Result, runtime::digest};
use serde::{Deserialize, Serialize};
use std::{
    path::{Path, PathBuf},
    time::Duration,
};
use tokio::process::Command;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct NativeConfig {
    pub executable: PathBuf,
    pub bubblewrap: PathBuf,
    pub cgroup_root: PathBuf,
    pub runtime_roots: Vec<PathBuf>,
    /// Existing workspace-relative application paths reviewed for builder writes.
    #[serde(default)]
    pub writable_paths: Vec<PathBuf>,
    pub auth_file: Option<PathBuf>,
    pub approval_policy: String,
    #[serde(default)]
    pub executable_sha256: Option<String>,
    #[serde(default)]
    pub bubblewrap_sha256: Option<String>,
    #[serde(default)]
    pub code_mode_host_sha256: Option<String>,
}
impl NativeConfig {
    pub async fn pin(mut self, config: &Config) -> Result<Self> {
        if !cfg!(all(target_os = "linux", target_arch = "x86_64")) {
            return Err(Error::Config(
                "native execution requires Linux namespaces and delegated cgroup v2".into(),
            ));
        }
        self.validate_runtime_roots(config)?;
        self.executable = self.executable.canonicalize()?;
        self.bubblewrap = self.bubblewrap.canonicalize()?;
        let saved_path = config.data_dir().join("native-backend.json");
        if saved_path.exists() {
            let saved: Self = serde_json::from_slice(&regular(&saved_path)?)?;
            if saved.executable_sha256.is_none()
                || saved.code_mode_host_sha256.is_none()
                || saved.bubblewrap_sha256.is_none()
            {
                return Err(Error::Incompatible(
                    "saved native backend is missing pinned binary identities".into(),
                ));
            }
            // Original operator JSON usually omits generated hashes. Those
            // omissions mean reuse the recorded identities, never repin bytes.
            if self.executable_sha256.is_none() {
                self.executable_sha256 = saved.executable_sha256.clone();
            }
            if self.code_mode_host_sha256.is_none() {
                self.code_mode_host_sha256 = saved.code_mode_host_sha256.clone();
            }
            if self.bubblewrap_sha256.is_none() {
                self.bubblewrap_sha256 = saved.bubblewrap_sha256.clone();
            }
            if self != saved {
                return Err(Error::Conflict("native backend is already pinned; configuration changes require separate storage".into()));
            }
        }
        // An existing identity is checked before invoking even --version.
        pin_hash(&self.executable, &mut self.executable_sha256)?;
        let output = super::process::output(
            {
                let mut c = Command::new(&self.executable);
                c.arg("--version").env_clear();
                c
            },
            None,
            Duration::from_secs(10),
        )
        .await?;
        if !output.status.success()
            || String::from_utf8_lossy(&output.stdout).trim() != "codex-cli 0.154.0"
        {
            return Err(Error::Config(
                "native provider must be Codex 0.154.0; no provider/version fallback is supported"
                    .into(),
            ));
        }
        pin_hash(
            &self.executable.with_file_name("codex-code-mode-host"),
            &mut self.code_mode_host_sha256,
        )
        .map_err(|error| {
            Error::Config(format!(
                "native Codex requires its matching codex-code-mode-host companion: {error}"
            ))
        })?;
        pin_hash(&self.bubblewrap, &mut self.bubblewrap_sha256)?;
        self.validate(config)?;
        super::broker::ExecutionWorker::probe_native(config.clone(), self.clone()).await?;
        Ok(self)
    }
    fn validate_runtime_roots(&self, config: &Config) -> Result<()> {
        for root in &self.runtime_roots {
            // Other mount trees (including home directories and Unix sockets)
            // are not supported toolchain locations. Provider binaries are
            // mounted as individual verified files, never their parent home.
            let supported =
                root.starts_with("/usr") || (root.starts_with("/opt") && root != Path::new("/opt"));
            if !supported
                || !root.is_absolute()
                || !root.is_dir()
                || root.canonicalize()? != *root
                || config.data_dir().starts_with(root)
                || config.workspace().starts_with(root)
                || root.starts_with(config.data_dir())
                || root.starts_with(config.workspace())
                || self
                    .auth_file
                    .as_ref()
                    .is_some_and(|auth| auth.starts_with(root))
            {
                return Err(Error::Config("native runtime roots must be reviewed canonical /usr or /opt toolchain directories excluding workspace, controller data and credentials".into()));
            }
        }
        Ok(())
    }

    pub(crate) fn validate(&self, config: &Config) -> Result<()> {
        if !cfg!(all(target_os = "linux", target_arch = "x86_64"))
            || !["never", "on-request"].contains(&self.approval_policy.as_str())
        {
            return Err(Error::Config("native execution requires Linux and never/on-request with all escalation requests denied".into()));
        }
        if unsafe { libc::geteuid() } == 0 {
            return Err(Error::Config(
                "native workers require a non-root operator".into(),
            ));
        }
        let code_mode_host = self.executable.with_file_name("codex-code-mode-host");
        for (path, saved) in [
            (&self.executable, &self.executable_sha256),
            (&code_mode_host, &self.code_mode_host_sha256),
            (&self.bubblewrap, &self.bubblewrap_sha256),
        ] {
            if !path.is_absolute() || saved.as_ref() != Some(&digest(&regular(path)?)) {
                return Err(Error::Config(
                    "native executable identity changed or is not pinned".into(),
                ));
            }
        }
        if !regular(&self.executable)?.starts_with(b"\x7fELF") {
            return Err(Error::Config("native Codex must be the pinned standalone Linux ELF binary, not a package-manager shim".into()));
        }
        self.validate_runtime_roots(config)?;
        for path in &self.writable_paths {
            if path.as_os_str().is_empty()
                || path
                    .components()
                    .any(|c| !matches!(c, std::path::Component::Normal(_)))
                || !config.workspace().join(path).exists()
                || config.workspace().join(path).canonicalize()? != config.workspace().join(path)
            {
                return Err(Error::Config("native writable_paths must name existing canonical application paths relative to the workspace".into()));
            }
        }
        if let Some(auth) = &self.auth_file
            && (!auth.is_absolute()
                || auth.canonicalize()? != *auth
                || !auth.is_file()
                || auth.starts_with(config.workspace()))
        {
            return Err(Error::Config(
                "native auth_file must be an absolute canonical regular file".into(),
            ));
        }
        if !self.cgroup_root.is_absolute()
            || self.cgroup_root.canonicalize()? != self.cgroup_root
            || !self.cgroup_root.join("cgroup.controllers").is_file()
            || !self.cgroup_root.join("cgroup.procs").is_file()
        {
            return Err(Error::Config(
                "native mode requires a delegated cgroup v2 directory with cgroup.kill support"
                    .into(),
            ));
        }
        // Confirm the path really resides on cgroup2, rather than accepting a
        // directory containing files named like control files.
        #[cfg(target_os = "linux")]
        {
            use std::os::unix::ffi::OsStrExt;
            let path = std::ffi::CString::new(self.cgroup_root.as_os_str().as_bytes())
                .map_err(|_| Error::Config("invalid cgroup path".into()))?;
            let mut stat = std::mem::MaybeUninit::<libc::statfs>::uninit();
            if unsafe { libc::statfs(path.as_ptr(), stat.as_mut_ptr()) } != 0
                || unsafe { stat.assume_init() }.f_type != 0x63677270
            {
                return Err(Error::Config(
                    "native cleanup requires a real cgroup v2 filesystem".into(),
                ));
            }
        }
        Ok(())
    }
}
fn pin_hash(path: &Path, saved: &mut Option<String>) -> Result<()> {
    let hash = digest(&regular(path)?);
    if saved.as_ref().is_some_and(|old| old != &hash) {
        return Err(Error::Conflict(
            "native binary differs from its saved identity".into(),
        ));
    }
    *saved = Some(hash);
    Ok(())
}
