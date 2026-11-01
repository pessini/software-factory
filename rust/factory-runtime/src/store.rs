use crate::{Error, Result, Run};
use async_trait::async_trait;
use sqlx::{Row, SqlitePool};

/// Domain persistence is independent of graph checkpoint and provider transactions.
#[async_trait]
pub trait FactoryStore: Send + Sync {
    async fn create(&self, run: &Run) -> Result<Run>;
    async fn load(&self, run_id: &str) -> Result<Run>;
    async fn list(&self) -> Result<Vec<Run>>;
    /// Compare-and-swap prevents an executor from overwriting an answer or cancellation.
    async fn save(&self, expected_revision: u64, run: &Run) -> Result<bool>;
}

pub struct SqliteFactoryStore {
    pool: SqlitePool,
}
impl SqliteFactoryStore {
    pub async fn new(pool: SqlitePool) -> Result<Self> {
        let mut tx = pool.begin_with("BEGIN IMMEDIATE").await?;
        sqlx::query(
            "CREATE TABLE IF NOT EXISTS factory_slice_migrations (version INTEGER PRIMARY KEY)",
        )
        .execute(&mut *tx)
        .await?;
        let version: i64 =
            sqlx::query_scalar("SELECT COALESCE(MAX(version), 0) FROM factory_slice_migrations")
                .fetch_one(&mut *tx)
                .await?;
        if version > 1 {
            return Err(Error::Incompatible(format!(
                "Factory database migration {version}; this binary supports 1"
            )));
        }
        if version == 0 {
            sqlx::raw_sql(include_str!("../migrations/001.sql"))
                .execute(&mut *tx)
                .await?;
        }
        tx.commit().await?;
        Ok(Self { pool })
    }
}

fn decode(document: &str) -> Result<Run> {
    // Check the envelope before decoding fields that could change in a later schema.
    let value: serde_json::Value = serde_json::from_str(document)?;
    if value["schema_version"] != crate::contracts::SCHEMA_VERSION
        || ![
            crate::contracts::GRAPH_VERSION,
            crate::workflow::GRAPH_VERSION,
            crate::planning::GRAPH_VERSION,
        ]
        .iter()
        .any(|v| value["graph_version"] == *v)
    {
        return Err(Error::Incompatible(
            "unsupported state/graph version; use the previous binary or an explicit migration"
                .into(),
        ));
    }
    let run: Run = serde_json::from_value(value)?;
    run.check_version()?;
    Ok(run)
}

#[async_trait]
impl FactoryStore for SqliteFactoryStore {
    async fn create(&self, run: &Run) -> Result<Run> {
        let mut tx = self.pool.begin_with("BEGIN IMMEDIATE").await?;
        sqlx::query("INSERT INTO factory_slice_sessions VALUES (?, ?) ON CONFLICT DO NOTHING")
            .bind(&run.input.session_id)
            .bind(&run.workspace_id)
            .execute(&mut *tx)
            .await?;
        let workspace: String = sqlx::query_scalar(
            "SELECT workspace_id FROM factory_slice_sessions WHERE session_id = ?",
        )
        .bind(&run.input.session_id)
        .fetch_one(&mut *tx)
        .await?;
        if workspace != run.workspace_id {
            return Err(Error::Conflict(
                "session belongs to a different workspace".into(),
            ));
        }
        sqlx::query(
            "INSERT INTO factory_slice_runs VALUES (?, ?, ?, ?) ON CONFLICT(request_id) DO NOTHING",
        )
        .bind(&run.run_id)
        .bind(&run.input.request_id)
        .bind(run.revision as i64)
        .bind(serde_json::to_string(run)?)
        .execute(&mut *tx)
        .await?;
        let document: String =
            sqlx::query_scalar("SELECT document FROM factory_slice_runs WHERE request_id = ?")
                .bind(&run.input.request_id)
                .fetch_one(&mut *tx)
                .await?;
        let saved = decode(&document)?;
        if saved.input != run.input || saved.workspace_id != run.workspace_id {
            return Err(Error::Conflict(
                "request ID already has different input".into(),
            ));
        }
        tx.commit().await?;
        Ok(saved)
    }
    async fn load(&self, run_id: &str) -> Result<Run> {
        let document: Option<String> =
            sqlx::query_scalar("SELECT document FROM factory_slice_runs WHERE run_id = ?")
                .bind(run_id)
                .fetch_optional(&self.pool)
                .await?;
        decode(&document.ok_or_else(|| Error::NotFound(run_id.into()))?)
    }
    async fn list(&self) -> Result<Vec<Run>> {
        sqlx::query("SELECT document FROM factory_slice_runs ORDER BY rowid")
            .fetch_all(&self.pool)
            .await?
            .iter()
            .map(|r| decode(r.get("document")))
            .collect()
    }
    async fn save(&self, expected_revision: u64, run: &Run) -> Result<bool> {
        run.check_version()?;
        Ok(sqlx::query("UPDATE factory_slice_runs SET revision = ?, document = ? WHERE run_id = ? AND revision = ?")
            .bind(run.revision as i64).bind(serde_json::to_string(run)?).bind(&run.run_id).bind(expected_revision as i64)
            .execute(&self.pool).await?.rows_affected() == 1)
    }
}
