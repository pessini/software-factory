use super::{Artifact, config::safe_relative};
use crate::{Error, Result, Runtime, runtime::digest};
use serde::Serialize;
use std::{
    fs::{self, File, OpenOptions},
    io::Write,
    os::unix::fs::{OpenOptionsExt, PermissionsExt},
    path::Path,
};

pub(crate) fn regular(path: &Path) -> Result<Vec<u8>> {
    if path.ancestors().any(|p| p.is_symlink()) {
        return Err(Error::Config(
            "controller path contains a symbolic link".into(),
        ));
    }
    let meta = fs::symlink_metadata(path)?;
    if !meta.is_file() {
        return Err(Error::Config(
            "controller record is not a regular file".into(),
        ));
    }
    Ok(fs::read(path)?)
}
pub(crate) fn atomic(path: &Path, bytes: &[u8]) -> Result<()> {
    if path.ancestors().any(|p| p.is_symlink()) {
        return Err(Error::Config(
            "controller path contains a symbolic link".into(),
        ));
    }
    let parent = path.parent().unwrap();
    if !parent.exists() {
        fs::create_dir_all(parent)?;
        fs::set_permissions(parent, fs::Permissions::from_mode(0o700))?;
    }
    let temporary = path.with_extension("tmp");
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&temporary)
        .or_else(|e| {
            if e.kind() != std::io::ErrorKind::AlreadyExists {
                return Err(e);
            }
            // Only controller-owned directories can contain a stale crash temporary.
            fs::remove_file(&temporary)?;
            OpenOptions::new()
                .write(true)
                .create_new(true)
                .mode(0o600)
                .open(&temporary)
        })?;
    file.write_all(bytes)?;
    file.sync_all()?;
    fs::rename(&temporary, path)?;
    File::open(parent)?.sync_all()?;
    Ok(())
}
pub(crate) async fn verify(runtime: &Runtime, id: &str) -> Result<()> {
    let run = runtime.status(id).await?;
    for (path, artifact) in &run.workflow.unwrap().artifacts {
        safe_relative(path)?;
        let bytes = regular(&runtime.config.data_dir.join("evidence").join(id).join(path))
            .map_err(|e| Error::Integrity(e.to_string()))?;
        if bytes.len() as u64 != artifact.bytes || digest(&bytes) != artifact.sha256 {
            return Err(Error::Integrity(format!(
                "authoritative evidence changed: {path}"
            )));
        }
    }
    Ok(())
}
pub(crate) async fn write(
    runtime: &Runtime,
    id: &str,
    path: &str,
    value: &impl Serialize,
) -> Result<String> {
    safe_relative(path)?;
    verify(runtime, id).await?;
    let bytes = serde_json::to_vec_pretty(value)?;
    let record = Artifact {
        sha256: digest(&bytes),
        bytes: bytes.len() as u64,
    };
    let destination = runtime.config.data_dir.join("evidence").join(id).join(path);
    if let Some(old) = runtime
        .status(id)
        .await?
        .workflow
        .unwrap()
        .artifacts
        .get(path)
    {
        if old != &record {
            return Err(Error::Config(format!(
                "immutable evidence differs during replay: {path}"
            )));
        }
    } else {
        atomic(&destination, &bytes)?;
        runtime
            .change(id, |run| {
                run.workflow
                    .as_mut()
                    .unwrap()
                    .artifacts
                    .insert(path.into(), record.clone());
                Ok(())
            })
            .await?;
    }
    let run = runtime.status(id).await?;
    let manifest = serde_json::json!({"version":2,"run_id":id,"workspace":runtime.config.workspace,"files":run.workflow.unwrap().artifacts});
    atomic(
        &runtime
            .config
            .data_dir
            .join("evidence")
            .join(id)
            .join("manifest.json"),
        &serde_json::to_vec_pretty(&manifest)?,
    )?;
    Ok(path.into())
}
