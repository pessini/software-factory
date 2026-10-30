use crate::{Error, Result};
use async_trait::async_trait;
use sqlx::SqlitePool;
use std::path::Path;
use tokio_util::sync::CancellationToken;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Reconciliation {
    Absent,
    Completed(String),
    Unknown(String),
}

/// Providers must distinguish proven absence from an uncertain dispatch. Only a
/// proven-absent operation may be dispatched again, with the same immutable key.
/// Dispatch futures must be cancellation-safe: dropping one must not leave an
/// unowned workspace writer. Effects completed before cancellation remain
/// discoverable through reconciliation. Protected process cleanup is part of #17.
#[async_trait]
pub trait Worker: Send + Sync {
    async fn reconcile(&self, operation_id: &str, request: &str) -> Result<Reconciliation>;
    async fn dispatch(
        &self,
        operation_id: &str,
        request: &str,
        cancel: CancellationToken,
    ) -> Result<String>;
}

/// A fake external system: its transaction is deliberately separate from the
/// Factory database/checkpointer. Each key produces exactly one durable receipt.
pub struct FakeWorker {
    pool: SqlitePool,
}
impl FakeWorker {
    pub async fn open(directory: &Path) -> Result<Self> {
        std::fs::create_dir_all(directory)?;
        let pool = crate::config::pool(&directory.join("fake-provider.sqlite")).await?;
        let mut tx = pool.begin_with("BEGIN IMMEDIATE").await?;
        let version: i64 = sqlx::query_scalar("PRAGMA user_version")
            .fetch_one(&mut *tx)
            .await?;
        if version > 1 {
            return Err(Error::Incompatible(
                "fake provider schema is newer than 1".into(),
            ));
        }
        if version == 0 {
            sqlx::query("CREATE TABLE receipts (operation_id TEXT PRIMARY KEY, request TEXT NOT NULL, result TEXT NOT NULL)").execute(&mut *tx).await?;
            sqlx::query("PRAGMA user_version = 1")
                .execute(&mut *tx)
                .await?;
        }
        tx.commit().await?;
        Ok(Self { pool })
    }
}
#[async_trait]
impl Worker for FakeWorker {
    async fn reconcile(&self, operation_id: &str, request: &str) -> Result<Reconciliation> {
        let receipt: Option<(String, String)> =
            sqlx::query_as("SELECT request, result FROM receipts WHERE operation_id = ?")
                .bind(operation_id)
                .fetch_optional(&self.pool)
                .await?;
        match receipt {
            Some((saved, result)) if saved == request => Ok(Reconciliation::Completed(result)),
            Some(_) => Err(Error::Conflict(
                "operation key has different provider input".into(),
            )),
            None => Ok(Reconciliation::Absent),
        }
    }
    async fn dispatch(
        &self,
        operation_id: &str,
        request: &str,
        cancel: CancellationToken,
    ) -> Result<String> {
        if cancel.is_cancelled() {
            return Err(Error::Cancelled);
        }
        let result = format!("fake worker completed: {request}");
        let mut tx = self.pool.begin_with("BEGIN IMMEDIATE").await?;
        sqlx::query("INSERT INTO receipts VALUES (?, ?, ?) ON CONFLICT DO NOTHING")
            .bind(operation_id)
            .bind(request)
            .bind(&result)
            .execute(&mut *tx)
            .await?;
        let (saved, result): (String, String) =
            sqlx::query_as("SELECT request, result FROM receipts WHERE operation_id = ?")
                .bind(operation_id)
                .fetch_one(&mut *tx)
                .await?;
        if saved != request {
            return Err(Error::Conflict(
                "operation key has different provider input".into(),
            ));
        }
        if cancel.is_cancelled() {
            return Err(Error::Cancelled);
        }
        tx.commit().await?;
        Ok(result)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CrashPoint {
    BeforeDispatch,
    AfterDispatch,
    AfterResult,
    AfterQueueFinish,
}

/// Injection point for process-death acceptance tests; production has no faults.
pub trait Faults: Send + Sync {
    fn hit(&self, point: CrashPoint);
}
pub struct NoFaults;
impl Faults for NoFaults {
    fn hit(&self, _: CrashPoint) {}
}
