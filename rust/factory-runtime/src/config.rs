use crate::{Error, Result};
use sqlx::sqlite::{SqliteConnectOptions, SqliteJournalMode, SqlitePoolOptions, SqliteSynchronous};
use std::{
    path::{Path, PathBuf},
    time::Duration,
};

#[derive(Debug, Clone)]
pub struct Config {
    pub(crate) data_dir: PathBuf,
    pub(crate) workspace: PathBuf,
}

impl Config {
    pub fn new(data_dir: impl AsRef<Path>, workspace: impl AsRef<Path>) -> Result<Self> {
        let data_dir = data_dir.as_ref();
        let workspace = workspace.as_ref();
        if !data_dir.is_absolute()
            || !workspace.is_absolute()
            || data_dir.to_string_lossy().contains(":memory:")
        {
            return Err(Error::Config(
                "storage and workspace must be absolute file paths".into(),
            ));
        }
        let existed = data_dir.exists();
        std::fs::create_dir_all(data_dir)?;
        if !existed {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(data_dir, std::fs::Permissions::from_mode(0o700))?;
        }
        let data_dir = data_dir.canonicalize()?;
        let workspace = workspace.canonicalize()?;
        if !workspace.is_dir()
            || data_dir.starts_with(&workspace)
            || workspace.starts_with(&data_dir)
        {
            return Err(Error::Config(
                "controller storage and workspace must be separate directories".into(),
            ));
        }
        Ok(Self {
            data_dir,
            workspace,
        })
    }
    pub fn data_dir(&self) -> &Path {
        &self.data_dir
    }
    pub fn workspace(&self) -> &Path {
        &self.workspace
    }
}

pub(crate) async fn pool(path: &Path) -> Result<sqlx::SqlitePool> {
    let options = SqliteConnectOptions::new()
        .filename(path)
        .create_if_missing(true)
        .journal_mode(SqliteJournalMode::Wal)
        .synchronous(SqliteSynchronous::Full)
        .foreign_keys(true)
        .busy_timeout(Duration::from_secs(5));
    Ok(SqlitePoolOptions::new()
        .max_connections(4)
        .connect_with(options)
        .await?)
}
