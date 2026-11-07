//! Client presentation ledger. Saved chats reference service IDs, never checkpoints.
use crate::{
    Error, Result,
    client::{Client, unique_id},
    runtime::digest,
};
use serde_json::{Value, json};
use sqlx::SqlitePool;

pub struct Chats {
    pool: SqlitePool,
}
impl Chats {
    pub async fn open() -> Result<Self> {
        use std::{os::unix::fs::PermissionsExt, path::PathBuf};
        let home = std::env::var_os("FACTORY_CLIENT_HOME")
            .map(PathBuf::from)
            .or_else(|| {
                std::env::var_os("HOME")
                    .map(|p| PathBuf::from(p).join(".local/state/software-factory/clients"))
            })
            .ok_or_else(|| {
                Error::Config("set FACTORY_CLIENT_HOME to a private client state directory".into())
            })?;
        std::fs::create_dir_all(&home)?;
        let home = home.canonicalize()?;
        use std::os::unix::fs::MetadataExt;
        if std::fs::metadata(&home)?.uid() != unsafe { libc::geteuid() } {
            return Err(Error::Config(
                "client storage must be owned by the current user".into(),
            ));
        }
        std::fs::set_permissions(&home, std::fs::Permissions::from_mode(0o700))?;
        if [
            "presentation.sqlite",
            "presentation.sqlite-wal",
            "presentation.sqlite-shm",
        ]
        .iter()
        .any(|name| home.join(name).is_symlink())
        {
            return Err(Error::Config(
                "client database files cannot be symbolic links".into(),
            ));
        }
        let pool = crate::config::pool(&home.join("presentation.sqlite")).await?;
        let mut tx = pool.begin_with("BEGIN IMMEDIATE").await?;
        let version: i64 = sqlx::query_scalar("PRAGMA user_version")
            .fetch_one(&mut *tx)
            .await?;
        if version > 1 {
            return Err(Error::Incompatible(
                "client presentation version is newer than 1".into(),
            ));
        }
        sqlx::raw_sql("CREATE TABLE IF NOT EXISTS chats (id TEXT PRIMARY KEY, origin TEXT NOT NULL, workspace TEXT NOT NULL, run_id TEXT); CREATE TABLE IF NOT EXISTS messages (sequence INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, document TEXT NOT NULL); PRAGMA user_version=1;").execute(&mut *tx).await?;
        tx.commit().await?;
        Ok(Self { pool })
    }
    pub async fn panel_session(&self, client: &Client, reference: Option<&str>) -> Result<String> {
        if reference.is_none() {
            let latest: Option<String> = sqlx::query_scalar(
                "SELECT id FROM chats WHERE origin=? AND workspace=? ORDER BY rowid DESC LIMIT 1",
            )
            .bind(&client.origin)
            .bind(client.workspace.to_string_lossy().as_ref())
            .fetch_optional(&self.pool)
            .await?;
            if let Some(id) = latest {
                return Ok(id);
            }
        }
        self.session(client, reference).await
    }
    pub async fn session(&self, client: &Client, reference: Option<&str>) -> Result<String> {
        let id = reference
            .map(|r| {
                format!(
                    "bb-{}",
                    digest(
                        format!("{}\0{}\0{r}", client.origin, client.workspace.display())
                            .as_bytes()
                    )
                )
            })
            .unwrap_or_else(|| format!("session_{}", unique_id()));
        sqlx::query("INSERT INTO chats VALUES (?,?,?,NULL) ON CONFLICT DO NOTHING")
            .bind(&id)
            .bind(&client.origin)
            .bind(client.workspace.to_string_lossy().as_ref())
            .execute(&self.pool)
            .await?;
        self.run(client, &id).await?;
        Ok(id)
    }
    pub async fn run(&self, client: &Client, id: &str) -> Result<Option<String>> {
        let row: Option<(String, String, Option<String>)> =
            sqlx::query_as("SELECT origin,workspace,run_id FROM chats WHERE id=?")
                .bind(id)
                .fetch_optional(&self.pool)
                .await?;
        let (origin,workspace,run)=row.ok_or_else(||Error::NotFound("unknown Rust chat; old Python chats must be recovered separately; use /attach for a saved Rust run".into()))?;
        if origin != client.origin || workspace != client.workspace.to_string_lossy() {
            return Err(Error::Conflict(
                "chat belongs to another service or workspace".into(),
            ));
        }
        Ok(run)
    }
    async fn attach(&self, client: &Client, id: &str, run: &str) -> Result<()> {
        self.run(client, id).await?;
        sqlx::query("UPDATE chats SET run_id=? WHERE id=?")
            .bind(run)
            .bind(id)
            .execute(&self.pool)
            .await?;
        Ok(())
    }
    async fn message(&self, id: &str, kind: &str, text: &str) -> Result<Value> {
        let update = json!({"sessionUpdate":kind,"content":{"type":"text","text":text}});
        sqlx::query("INSERT INTO messages(session_id,document) VALUES (?,?)")
            .bind(id)
            .bind(serde_json::to_string(&update)?)
            .execute(&self.pool)
            .await?;
        Ok(notification(id, update))
    }
    pub async fn call(
        &self,
        client: &Client,
        method: &str,
        params: &Value,
    ) -> Result<(Value, Vec<Value>)> {
        let mut updates = vec![];
        let text = |key: &str| {
            params[key]
                .as_str()
                .filter(|s| !s.is_empty())
                .map(String::from)
                .ok_or_else(|| Error::Config(format!("{key} is required")))
        };
        match method {
            "initialize" => Ok((
                json!({"protocolVersion":1,"agentCapabilities":{"loadSession":true,"promptCapabilities":{"embeddedContext":false,"image":false,"audio":false},"mcpCapabilities":{"http":false,"sse":false}},"agentInfo":{"name":"software-factory","title":"Software Factory","version":env!("CARGO_PKG_VERSION")},"authMethods":[]}),
                updates,
            )),
            "session/new" | "session/load" => {
                if std::path::Path::new(&text("cwd")?).canonicalize()? != client.workspace {
                    return Err(Error::Conflict(
                        "session workspace differs from configured workspace".into(),
                    ));
                }
                client.verify().await?;
                let id = if method == "session/new" {
                    self.session(client, std::env::var("BB_THREAD_ID").ok().as_deref())
                        .await?
                } else {
                    text("sessionId")?
                };
                self.run(client, &id).await?;
                if method == "session/load" {
                    let history: Vec<String> = sqlx::query_scalar(
                        "SELECT document FROM messages WHERE session_id=? ORDER BY sequence",
                    )
                    .bind(&id)
                    .fetch_all(&self.pool)
                    .await?;
                    for item in history {
                        updates.push(notification(&id, serde_json::from_str(&item)?));
                    }
                }
                let options = client.settings(&id, &json!({"action":"read"})).await?;
                Ok((
                    json!({"sessionId":id,"configOptions":options["configOptions"]}),
                    updates,
                ))
            }
            "session/set_config_option" => {
                let id = text("sessionId")?;
                self.run(client, &id).await?;
                let options=client.settings(&id,&json!({"action":"change","config_id":text("configId")?,"value":text("value")?})).await?;
                Ok((json!({"configOptions":options["configOptions"]}), updates))
            }
            "session/prompt" => {
                let id = text("sessionId")?;
                let run = self.run(client, &id).await?;
                let blocks = params["prompt"]
                    .as_array()
                    .ok_or_else(|| Error::Config("prompt must be text blocks".into()))?;
                let mut parts = vec![];
                for block in blocks {
                    if block["type"] != "text" {
                        return Err(Error::Config("Factory accepts text prompts only".into()));
                    }
                    parts.push(
                        block["text"]
                            .as_str()
                            .ok_or_else(|| Error::Config("text is required".into()))?,
                    );
                }
                let request = parts.join("\n");
                let mut request = request.trim();
                if let Some(wrapped) = request.strip_prefix("<system_instructions>\n") {
                    request = wrapped
                        .split_once("\n</system_instructions>\n")
                        .map(|(_, body)| body.trim())
                        .ok_or_else(|| {
                            Error::Config("BB instruction wrapper is incomplete".into())
                        })?;
                }
                let sender = regex::Regex::new(r"^\[bb message from thread:[A-Za-z0-9_-]+\]\n\n")
                    .expect("valid BB metadata pattern");
                let request = sender.replace(request, "");
                let request = request.trim();
                if request.starts_with("[bb system]\n") {
                    return Ok((json!({"stopReason":"end_turn"}), updates));
                }
                if request.is_empty() {
                    return Err(Error::Config("request must not be empty".into()));
                }
                self.message(&id, "user_message_chunk", request).await?;
                let result = self.prompt(client, &id, run.as_deref(), request).await;
                let body = match result {
                    Ok(value) => serde_json::to_string_pretty(&value)?,
                    Err(error) => format!("Factory command failed: {error}"),
                };
                updates.push(self.message(&id, "agent_message_chunk", &body).await?);
                Ok((json!({"stopReason":"end_turn"}), updates))
            }
            "session/cancel" => Ok((json!({}), updates)), // presentation only; /cancel owns the saved stop request
            _ => Err(Error::Config("method not found".into())),
        }
    }
    async fn prompt(
        &self,
        client: &Client,
        session: &str,
        run: Option<&str>,
        request: &str,
    ) -> Result<Value> {
        let (command, argument) = request.split_once(' ').unwrap_or((request, ""));
        let command = command.trim_start_matches('/').to_ascii_lowercase();
        let command = command.as_str();
        let (target, rest) = argument.split_once(' ').unwrap_or((argument, ""));
        if matches!(
            command,
            "plan"
                | "plans"
                | "plan-status"
                | "plan-answer"
                | "plan-revise"
                | "plan-approve"
                | "plan-cancel"
                | "plan-resume"
        ) {
            let (operation, value) = match command {
                "plan" => (
                    "plan_start",
                    json!({"request":argument,"request_id":unique_id(),"client_session_id":session}),
                ),
                "plans" => ("plan_list", json!({})),
                "plan-status" => ("plan_status", json!({"session_id":target})),
                "plan-revise" => ("plan_revise", json!({"session_id":target,"feedback":rest})),
                "plan-approve" => (
                    "plan_approve",
                    json!({"session_id":target,"plan_hash":rest}),
                ),
                "plan-cancel" => ("plan_cancel", json!({"session_id":target})),
                "plan-resume" => ("plan_resume", json!({"session_id":target})),
                "plan-answer" => {
                    let current = client
                        .call("plan_status", json!({"session_id":target}))
                        .await?;
                    (
                        "plan_answer",
                        json!({"session_id":target,"interaction_id":current["pending"]["interaction_id"],"answers":serde_json::from_str::<Value>(rest)?}),
                    )
                }
                _ => return Err(Error::Config("unknown planning command".into())),
            };
            return client.call(operation, value).await;
        }
        let value = match command {
            "attach" => client.call("status", json!({"run_id":argument})).await?,
            "new" => {
                client
                    .call(
                        "start",
                        json!({"request":argument,"request_id":unique_id(),"session_id":session}),
                    )
                    .await?
            }
            _ if run.is_none()
                && !matches!(
                    command,
                    "status" | "inspect" | "continue" | "resume" | "retry" | "cancel" | "answer"
                ) =>
            {
                client
                    .call(
                        "start",
                        json!({"request":request,"request_id":unique_id(),"session_id":session}),
                    )
                    .await?
            }
            _ => {
                let run =
                    run.ok_or_else(|| Error::Config("use /attach RUN_ID or /new REQUEST".into()))?;
                match command {
                    "status" | "inspect" => client.call("status", json!({"run_id":run})).await?,
                    "continue" | "resume" | "retry" => {
                        client.call("resume", json!({"run_id":run})).await?
                    }
                    "cancel" => client.call("cancel", json!({"run_id":run})).await?,
                    "answer" => {
                        let answers = if rest.trim_start().starts_with('{') {
                            serde_json::from_str::<Value>(rest)?
                        } else {
                            let saved = client.call("status", json!({"run_id":run})).await?;
                            let question = saved["interactions"]
                                .as_array()
                                .and_then(|a| a.iter().find(|q| q["interaction_id"] == target))
                                .ok_or_else(|| Error::Config("unknown saved interaction".into()))?;
                            let key = question["questions"][0]["id"]
                                .as_str()
                                .ok_or_else(|| Error::Config("missing saved question".into()))?;
                            json!({key:rest})
                        };
                        client
                            .call(
                                "answer",
                                json!({"run_id":run,"interaction_id":target,"answers":answers}),
                            )
                            .await?
                    }
                    _ => {
                        return Err(Error::Config(
                            "use /status, /continue, /answer ID ANSWER, /cancel, or /new REQUEST"
                                .into(),
                        ));
                    }
                }
            }
        };
        if let Some(run) = value["run_id"].as_str() {
            self.attach(client, session, run).await?;
        }
        Ok(value)
    }
}
fn notification(id: &str, update: Value) -> Value {
    json!({"jsonrpc":"2.0","method":"session/update","params":{"sessionId":id,"update":update}})
}
