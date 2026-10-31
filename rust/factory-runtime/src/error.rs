#[derive(Debug, thiserror::Error)]
pub enum Error {
    #[error("invalid configuration: {0}")]
    Config(String),
    #[error("stale or changed evidence: {0}")]
    Integrity(String),
    #[error("identity conflict: {0}")]
    Conflict(String),
    #[error("run not found: {0}")]
    NotFound(String),
    #[error("incompatible persisted schema: {0}")]
    Incompatible(String),
    #[error("workspace is owned by another executor")]
    Busy,
    #[error("cancelled")]
    Cancelled,
    #[error("execution deadline expired")]
    Deadline,
    #[error("worker requires reconciliation: {0}")]
    Worker(String),
    #[error("waiting for input: {0}")]
    Waiting(String),
    #[error(transparent)]
    Storage(#[from] sqlx::Error),
    #[error(transparent)]
    Io(#[from] std::io::Error),
    #[error(transparent)]
    Json(#[from] serde_json::Error),
    #[error(transparent)]
    Graph(#[from] adk_graph::error::GraphError),
    #[error(transparent)]
    Adk(#[from] adk_core::AdkError),
}
pub type Result<T> = std::result::Result<T, Error>;
