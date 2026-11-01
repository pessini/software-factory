//! Single versioned command boundary used by HTTP, native CLI and transport adapters.
use crate::planning::PlanningInput;
use crate::workflow::{ApprovedPlan, ImplementationInput, WorkflowConfig, config::Selection};
use crate::{Error, Result, Run, Runtime, StartRequest, Status, runtime::digest};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sqlx::SqlitePool;

#[derive(Clone)]
pub struct Control {
    pub runtime: Runtime,
    pool: SqlitePool,
}
#[derive(Clone, Serialize, Deserialize)]
struct SavedSettings {
    version: u32,
    config: WorkflowConfig,
    selection: Selection,
}
#[derive(Clone, Serialize, Deserialize)]
struct Revision {
    input: StartRequest,
    handoff: Option<Handoff>,
}
impl Revision {
    fn run_id(&self) -> String {
        self.input.run_id()
    }
}
#[derive(Clone, Serialize, Deserialize)]
struct Handoff {
    input: StartRequest,
    plan_hash: String,
    actor: String,
    approved_at: String,
    decision_id: String,
}
#[derive(Clone, Serialize, Deserialize)]
struct Session {
    version: u32,
    session_id: String,
    workspace_id: String,
    created_at: String,
    revisions: Vec<Revision>,
}
impl Control {
    pub async fn open(runtime: Runtime) -> Result<Self> {
        let pool =
            crate::config::pool(&runtime.config.data_dir.join("factory-controls.sqlite")).await?;
        let mut tx = pool.begin_with("BEGIN IMMEDIATE").await?;
        let version: i64 = sqlx::query_scalar("PRAGMA user_version")
            .fetch_one(&mut *tx)
            .await?;
        if version > 1 {
            return Err(Error::Incompatible("control database newer than 1".into()));
        }
        if version == 0 {
            sqlx::raw_sql("CREATE TABLE planning_sessions (session_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, document TEXT NOT NULL); CREATE TABLE settings (session_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, document TEXT NOT NULL); PRAGMA user_version=1;").execute(&mut *tx).await?;
        }
        tx.commit().await?;
        Ok(Self { runtime, pool })
    }
    pub fn workspace(&self) -> &std::path::Path {
        self.runtime.config.workspace()
    }
    async fn load(&self, id: &str) -> Result<Session> {
        let value: Option<String> = sqlx::query_scalar(
            "SELECT document FROM planning_sessions WHERE session_id=? AND workspace_id=?",
        )
        .bind(id)
        .bind(&self.runtime.workspace_id)
        .fetch_optional(&self.pool)
        .await?;
        let session: Session =
            serde_json::from_str(&value.ok_or_else(|| Error::NotFound(id.into()))?)?;
        if session.version != 1 {
            return Err(Error::Incompatible("planning session version".into()));
        }
        Ok(session)
    }
    async fn replace(&self, old: &Session, new: &Session) -> Result<bool> {
        Ok(sqlx::query("UPDATE planning_sessions SET document=? WHERE session_id=? AND workspace_id=? AND document=?").bind(serde_json::to_string(new)?).bind(&old.session_id).bind(&self.runtime.workspace_id).bind(serde_json::to_string(old)?).execute(&self.pool).await?.rows_affected()==1)
    }
    async fn sessions(&self) -> Result<Vec<Session>> {
        let rows: Vec<String> = sqlx::query_scalar(
            "SELECT document FROM planning_sessions WHERE workspace_id=? ORDER BY rowid",
        )
        .bind(&self.runtime.workspace_id)
        .fetch_all(&self.pool)
        .await?;
        rows.into_iter()
            .map(|s| Ok(serde_json::from_str(&s)?))
            .collect()
    }
    /// Repair saved command intents after a process death; never called from reads.
    pub async fn recover(&self) -> Result<()> {
        for session in self.sessions().await? {
            for revision in session.revisions {
                self.recover_input(revision.input).await?;
                if let Some(h) = revision.handoff {
                    self.recover_input(h.input).await?;
                }
            }
        }
        Ok(())
    }
    async fn recover_input(&self, input: StartRequest) -> Result<()> {
        let id = input.run_id();
        match self.runtime.status(&id).await {
            Ok(run) if run.status.is_terminal() || run.scheduler_id.is_some() => Ok(()),
            Ok(_) | Err(Error::NotFound(_)) => {
                self.runtime.start(input).await?;
                Ok(())
            }
            Err(error) => Err(error),
        }
    }
    async fn config(&self, session_id: &str) -> Result<WorkflowConfig> {
        let saved: Option<(String, String)> =
            sqlx::query_as("SELECT workspace_id, document FROM settings WHERE session_id=?")
                .bind(session_id)
                .fetch_optional(&self.pool)
                .await?;
        if let Some((workspace, document)) = saved {
            return self.decode_settings(&workspace, &document);
        }
        let path = self.runtime.config.workspace.join(".factory/factory.yaml");
        let mut config = WorkflowConfig::from_yaml(
            &String::from_utf8(crate::workflow::artifacts::regular(&path)?)
                .map_err(|_| Error::Config("config is not UTF8".into()))?,
        )?;
        let selection = Selection::default();
        config.resolve(
            &selection,
            self.runtime.services.worker.model_catalog().await?,
        )?;
        let snapshot = SavedSettings {
            version: 1,
            config,
            selection,
        };
        sqlx::query("INSERT INTO settings VALUES (?,?,?) ON CONFLICT DO NOTHING")
            .bind(session_id)
            .bind(&self.runtime.workspace_id)
            .bind(serde_json::to_string(&snapshot)?)
            .execute(&self.pool)
            .await?;
        let (workspace, document): (String, String) =
            sqlx::query_as("SELECT workspace_id,document FROM settings WHERE session_id=?")
                .bind(session_id)
                .fetch_one(&self.pool)
                .await?;
        self.decode_settings(&workspace, &document)
    }
    fn decode_settings(&self, workspace: &str, document: &str) -> Result<WorkflowConfig> {
        if workspace != self.runtime.workspace_id {
            return Err(Error::Conflict(
                "settings belong to a different workspace".into(),
            ));
        }
        let settings: SavedSettings = serde_json::from_str(document)?;
        if settings.version != 1 {
            return Err(Error::Incompatible("settings snapshot version".into()));
        }
        settings.config.validate()?;
        Ok(settings.config)
    }
    pub async fn call(&self, operation: &str, value: Value) -> Result<Value> {
        self.call_as(operation, value, "local").await
    }
    pub async fn call_as(&self, operation: &str, value: Value, actor: &str) -> Result<Value> {
        Box::pin(self.dispatch(operation, value, actor)).await
    }
    async fn dispatch(&self, operation: &str, value: Value, actor: &str) -> Result<Value> {
        if !value.is_object() {
            return Err(Error::Config("a JSON object is required".into()));
        }
        for key in [
            "workspace",
            "data_dir",
            "configuration",
            "config",
            "approved_plan",
            "implementation",
        ] {
            if value.get(key).is_some() {
                return Err(Error::Config(format!("{key} is owned by the service")));
            }
        }
        let id = value
            .get("session_id")
            .and_then(Value::as_str)
            .unwrap_or("default");
        let run_id = value.get("run_id").and_then(Value::as_str).unwrap_or("");
        let after = value
            .get("after_event")
            .and_then(Value::as_u64)
            .unwrap_or(0);
        match operation {
            "start" => {
                let request = text(&value, "request")?;
                let request_id = text(&value, "request_id")?;
                if request_id.len() > 200 {
                    return Err(Error::Config("request ID exceeds 200 characters".into()));
                }
                let mut input = StartRequest::new(
                    &format!("req-{}", digest(request_id.as_bytes())),
                    id,
                    request,
                );
                let saved_id = input.run_id();
                if let Ok(saved) = self.runtime.status(&saved_id).await {
                    if saved.input.request != request || saved.input.session_id != id {
                        return Err(Error::Conflict(
                            "request ID already has different input".into(),
                        ));
                    }
                    if let Some(selected) = value.get("selection").filter(|v| !v.is_null()) {
                        let selection: Selection = serde_json::from_value(selected.clone())?;
                        if saved
                            .input
                            .implementation
                            .as_ref()
                            .and_then(|i| i.config.resolved_settings.as_ref())
                            .map(|s| &s.requested)
                            != Some(&serde_json::to_value(selection)?)
                        {
                            return Err(Error::Conflict(
                                "request ID already has different settings".into(),
                            ));
                        }
                    }
                    return Ok(run_view(self.runtime.start(saved.input).await?, 0));
                }
                let mut config = self.config(id).await?;
                if let Some(selected) = value.get("selection").filter(|v| !v.is_null()) {
                    config.resolved_settings = None;
                    config.resolve(
                        &serde_json::from_value(selected.clone())?,
                        self.runtime.services.worker.model_catalog().await?,
                    )?;
                }
                input.implementation = Some(ImplementationInput {
                    config,
                    required_criteria: vec![],
                    approved_plan: None,
                });
                Ok(run_view(self.runtime.start(input).await?, 0))
            }
            "status" => Ok(run_view(self.runtime.status(run_id).await?, after)),
            "list" => Ok(json!(
                self.runtime
                    .list()
                    .await?
                    .into_iter()
                    .map(|r| run_view(r, 0))
                    .collect::<Vec<_>>()
            )),
            "answer" => {
                let answers = serde_json::from_value(value["answers"].clone())?;
                Ok(run_view(
                    self.runtime
                        .answer_questions(run_id, text(&value, "interaction_id")?, answers)
                        .await?,
                    0,
                ))
            }
            "resume" => {
                // Resume is a saved wake request. A browser request never owns execution.
                let run = self.runtime.status(run_id).await?;
                let run = self.runtime.ensure_scheduled(&run).await?;
                if run.status.is_terminal() {
                    return Ok(run_view(run, 0));
                }
                self.runtime
                    .services
                    .queue
                    .wake(
                        run.scheduler_id
                            .as_deref()
                            .ok_or_else(|| Error::Conflict("not scheduled".into()))?,
                    )
                    .await?;
                if run.status == Status::RecoveryNeeded {
                    self.runtime
                        .change(run_id, |r| {
                            if r.status == Status::RecoveryNeeded {
                                r.status = Status::Pending;
                            }
                            Ok(())
                        })
                        .await?;
                }
                Ok(run_view(self.runtime.status(run_id).await?, 0))
            }
            "cancel" => {
                let run = self
                    .runtime
                    .change(run_id, |r| {
                        if r.delivery_pending() {
                            let delivery = &mut r.workflow.as_mut().unwrap().delivery;
                            delivery["cancel_requested"] = json!(true);
                            delivery["status"] = json!("stopping");
                        }
                        if !r.status.is_terminal() {
                            r.cancel_requested = true;
                            r.status = Status::Stopping;
                            r.event("cancel", "info", "Cancellation requested");
                        }
                        Ok(())
                    })
                    .await?;
                Ok(run_view(run, 0))
            }
            "models" => Ok(json!(self.runtime.services.worker.model_catalog().await?)),
            "settings" => {
                if !run_id.is_empty() {
                    if value.get("selection").is_some_and(|v| !v.is_null()) {
                        return Err(Error::Conflict(
                            "saved run settings cannot be changed".into(),
                        ));
                    }
                    return Ok(json!(
                        self.runtime
                            .status(run_id)
                            .await?
                            .input
                            .implementation
                            .map(|i| i.config)
                    ));
                }
                if let Some(selection) = value.get("selection").filter(|v| !v.is_null()) {
                    let selected: Selection = serde_json::from_value(selection.clone())?;
                    let mut config = self.config(id).await?;
                    config.resolved_settings = None;
                    config.resolve(
                        &selected,
                        self.runtime.services.worker.model_catalog().await?,
                    )?;
                    let changed=sqlx::query("INSERT INTO settings VALUES (?,?,?) ON CONFLICT(session_id) DO UPDATE SET document=excluded.document WHERE workspace_id=excluded.workspace_id").bind(id).bind(&self.runtime.workspace_id).bind(serde_json::to_string(&SavedSettings{version:1,config:config.clone(),selection:selected})?).execute(&self.pool).await?.rows_affected();
                    if changed == 0 {
                        return Err(Error::Conflict("settings ownership conflict".into()));
                    }
                }
                let config = self.config(id).await?;
                Ok(
                    json!({"session_id":id,"configuration":config,"selection":config.resolved_settings.as_ref().map(|s|&s.requested),"resolved":config.resolved_settings}),
                )
            }
            "plan_start" => {
                let request = text(&value, "request")?;
                let request_id = text(&value, "request_id")?;
                if request_id.len() > 200 || request.len() > 65536 {
                    return Err(Error::Config("request too long".into()));
                }
                let session_id = format!(
                    "pln_{}",
                    &digest(format!("{}\0{request_id}", self.runtime.workspace_id).as_bytes())
                        [..12]
                );
                if let Ok(saved) = self.load(&session_id).await {
                    if saved.revisions[0].input.request != request {
                        return Err(Error::Conflict(
                            "request ID already has different input".into(),
                        ));
                    }
                    self.runtime.start(saved.revisions[0].input.clone()).await?;
                    return self.plan_status(&session_id, 0).await;
                }
                let client_session = value
                    .get("client_session_id")
                    .and_then(Value::as_str)
                    .unwrap_or("default");
                let input = Self::plan_input(
                    PlanningInput {
                        session_id: session_id.clone(),
                        revision: 1,
                        feedback: None,
                        previous: None,
                    },
                    request,
                    request_id,
                    self.config(client_session).await?,
                );
                let session = Session {
                    version: 1,
                    session_id: session_id.clone(),
                    workspace_id: self.runtime.workspace_id.clone(),
                    created_at: chrono::Utc::now().to_rfc3339(),
                    revisions: vec![Revision {
                        input,
                        handoff: None,
                    }],
                };
                sqlx::query("INSERT INTO planning_sessions VALUES (?,?,?) ON CONFLICT DO NOTHING")
                    .bind(&session_id)
                    .bind(&session.workspace_id)
                    .bind(serde_json::to_string(&session)?)
                    .execute(&self.pool)
                    .await?;
                let saved = self.load(&session_id).await?;
                if saved.revisions[0].input.request != request {
                    return Err(Error::Conflict(
                        "request ID already has different input".into(),
                    ));
                }
                self.runtime.start(saved.revisions[0].input.clone()).await?;
                self.plan_status(&session_id, 0).await
            }
            "plan_list" => {
                let mut result = vec![];
                for s in self.sessions().await? {
                    let current = s.revisions.last().unwrap();
                    let run = self.runtime.status(&current.run_id()).await?;
                    result.push(json!({"session_id":s.session_id,"revision":s.revisions.len(),"status":run.status,"request":current.input.request,"created_at":s.created_at}));
                }
                Ok(json!(result))
            }
            "plan_status" => self.plan_status(text(&value, "session_id")?, after).await,
            "plan_answer" | "plan_resume" | "plan_cancel" => {
                let id = text(&value, "session_id")?;
                let session = self.load(id).await?;
                let run_id = session.revisions.last().unwrap().run_id();
                let mut payload = value.clone();
                payload["run_id"] = json!(run_id);
                Box::pin(self.call_as(operation.strip_prefix("plan_").unwrap(), payload, actor))
                    .await?;
                self.plan_status(id, 0).await
            }
            "plan_revise" => {
                let id = text(&value, "session_id")?;
                let feedback = text(&value, "feedback")?;
                loop {
                    let old = self.load(id).await?;
                    let current = old.revisions.last().unwrap();
                    // Lost response/concurrent identical revision retries return the saved revision.
                    if current.input.planning.as_ref().unwrap().feedback.as_deref()
                        == Some(feedback)
                    {
                        self.runtime.start(current.input.clone()).await?;
                        return self.plan_status(id, 0).await;
                    }
                    let run = self.runtime.status(&current.run_id()).await?;
                    if !run.status.is_terminal() {
                        return Err(Error::Conflict(
                            "finish or cancel the current revision before revising".into(),
                        ));
                    }
                    let previous = run
                        .workflow
                        .as_ref()
                        .and_then(|s| s.planning.as_ref())
                        .and_then(|s| s.snapshot.clone());
                    let input = Self::plan_input(
                        PlanningInput {
                            session_id: id.into(),
                            revision: old.revisions.len() as u32 + 1,
                            feedback: Some(feedback.into()),
                            previous,
                        },
                        &run.input.request,
                        &format!(
                            "revise-{}",
                            digest(format!("{}\0{feedback}", run.run_id).as_bytes())
                        ),
                        run.input.implementation.as_ref().unwrap().config.clone(),
                    );
                    let mut new = old.clone();
                    new.revisions.push(Revision {
                        input: input.clone(),
                        handoff: None,
                    });
                    if self.replace(&old, &new).await? {
                        self.runtime.start(input).await?;
                        return self.plan_status(id, 0).await;
                    }
                }
            }
            "plan_approve" => {
                let id = text(&value, "session_id")?;
                let hash = text(&value, "plan_hash")?;
                loop {
                    let old = self.load(id).await?;
                    let revision = old.revisions.last().unwrap();
                    if let Some(handoff) = &revision.handoff {
                        if handoff.plan_hash != hash {
                            return Err(Error::Conflict("stale plan hash".into()));
                        }
                        self.runtime.start(handoff.input.clone()).await?;
                        return self.plan_status(id, 0).await;
                    }
                    let run = self.runtime.status(&revision.run_id()).await?;
                    if run
                        .workflow
                        .as_ref()
                        .and_then(|s| s.planning.as_ref())
                        .is_some_and(|p| p.legacy_handoff.is_some())
                    {
                        let legacy = run.workflow.as_ref().unwrap().planning.as_ref().unwrap();
                        if legacy.plan_hash.as_deref() != Some(hash) {
                            return Err(Error::Conflict("stale historical plan hash".into()));
                        }
                        return self.plan_status(id, 0).await;
                    }
                    if run.status != Status::Planned {
                        return Err(Error::Conflict(
                            "only a finished plan may be approved".into(),
                        ));
                    }
                    crate::workflow::artifacts::verify(&self.runtime, &run.run_id).await?;
                    let planning = run.workflow.as_ref().unwrap().planning.as_ref().unwrap();
                    if planning.plan_hash.as_deref() != Some(hash) {
                        return Err(Error::Conflict(
                            "stale plan hash; review current revision".into(),
                        ));
                    }
                    let snapshot = planning
                        .snapshot
                        .as_ref()
                        .ok_or_else(|| Error::Integrity("missing plan snapshot".into()))?;
                    let content = planning
                        .snapshot_content
                        .clone()
                        .unwrap_or(serde_json::to_string(snapshot)?);
                    if digest(content.as_bytes()) != hash {
                        return Err(Error::Integrity("snapshot hash mismatch".into()));
                    }
                    // Approval captures the exact original execution policy and model settings.
                    let config = run.input.implementation.as_ref().unwrap().config.clone();
                    config.validate()?;
                    let mut criteria: Vec<crate::workflow::Criterion> =
                        serde_json::from_value(snapshot["plan"]["completion_checks"].clone())?;
                    if snapshot["glossary"]
                        .as_array()
                        .is_some_and(|a| !a.is_empty())
                    {
                        criteria.push(crate::workflow::Criterion {
                            id: "planning-glossary".into(),
                            description: "CONTEXT.md contains the exact approved glossary wording"
                                .into(),
                        });
                    }
                    for decision in snapshot["decisions"]
                        .as_array()
                        .into_iter()
                        .flatten()
                        .filter(|d| d["adr"] == true)
                    {
                        criteria.push(crate::workflow::Criterion {
                            id: format!("planning-adr-{}", decision["id"].as_str().unwrap_or("")),
                            description: format!(
                                "docs/adr records the approved decision and rationale: {}",
                                decision["title"]
                            ),
                        });
                    }
                    for earlier in old.revisions.iter().take(old.revisions.len() - 1) {
                        if let Some(h) = &earlier.handoff {
                            let run_id = h.input.run_id();
                            match self.runtime.status(&run_id).await {
                                Ok(r) if r.status.is_terminal() => (),
                                _ => return Err(Error::Busy),
                            }
                        }
                    }
                    let mut input = StartRequest::new(
                        &format!(
                            "handoff-{}",
                            digest(format!("{}\0{hash}", run.run_id).as_bytes())
                        ),
                        &format!("impl-{id}"),
                        &format!(
                            "Implement the exact approved_plan for planning session {id} (sha256 {hash}). Its immutable content contains the settled scope, decisions and document versions. Ask only about new blockers. Write the approved glossary to CONTEXT.md and ADR decisions under docs/adr, preserving unrelated content. Approval grants no commit, push, merge or deploy authorization."
                        ),
                    );
                    input.implementation = Some(ImplementationInput {
                        config,
                        required_criteria: vec![],
                        approved_plan: Some(ApprovedPlan {
                            planning_session_id: id.into(),
                            revision_id: run.run_id.clone(),
                            sha256: hash.into(),
                            content,
                            completion_checks: criteria,
                        }),
                    });
                    input.validate()?;
                    let handoff = Handoff {
                        input: input.clone(),
                        plan_hash: hash.into(),
                        actor: actor.into(),
                        approved_at: chrono::Utc::now().to_rfc3339(),
                        decision_id: format!("approve-{}", input.request_id),
                    };
                    let mut new = old.clone();
                    new.revisions.last_mut().unwrap().handoff = Some(handoff);
                    if self.replace(&old, &new).await? {
                        self.runtime
                            .services
                            .faults
                            .hit(crate::CrashPoint::AfterHandoffIntent);
                        self.runtime.start(input).await?;
                        return self.plan_status(id, 0).await;
                    }
                }
            }
            "plan_report" => {
                let s = self.load(text(&value, "session_id")?).await?;
                let run = self
                    .runtime
                    .status(&s.revisions.last().unwrap().run_id())
                    .await?;
                if let Some(report) = run
                    .workflow
                    .as_ref()
                    .and_then(|w| w.planning.as_ref())
                    .and_then(|p| p.legacy_implementation.as_ref())
                    .and_then(|i| i.get("report"))
                    .and_then(Value::as_str)
                {
                    return Ok(json!({"report":report}));
                }
                let h = s
                    .revisions
                    .last()
                    .unwrap()
                    .handoff
                    .as_ref()
                    .ok_or_else(|| Error::NotFound("no implementation".into()))?;
                let id = h.input.run_id();
                self.runtime.status(&id).await?;
                crate::workflow::artifacts::verify(&self.runtime, &id).await?;
                let path = self
                    .runtime
                    .config
                    .data_dir
                    .join("evidence")
                    .join(&id)
                    .join("handoff.json");
                let report = crate::workflow::artifacts::regular(&path)?;
                Ok(json!({"report":String::from_utf8_lossy(&report)}))
            }
            _ => Err(Error::Config(format!(
                "unknown control operation: {operation}"
            ))),
        }
    }
    fn plan_input(
        planning: PlanningInput,
        request: &str,
        key: &str,
        config: WorkflowConfig,
    ) -> StartRequest {
        let mut input = StartRequest::new(
            &format!(
                "plan-{}",
                digest(format!("{}\0{key}", planning.session_id).as_bytes())
            ),
            &planning.session_id,
            request,
        );
        input.implementation = Some(ImplementationInput {
            config,
            required_criteria: vec![],
            approved_plan: None,
        });
        input.planning = Some(planning);
        input
    }
    async fn plan_status(&self, id: &str, after: u64) -> Result<Value> {
        let session = self.load(id).await?;
        let current = session.revisions.last().unwrap();
        let run = self.runtime.status(&current.run_id()).await?;
        let state = run.workflow.as_ref().unwrap();
        let planning = state.planning.as_ref();
        let mut rounds = planning.map(|p| p.rounds.clone()).unwrap_or_default();
        for question in state.interactions.values() {
            if !rounds
                .iter()
                .any(|round| round["interaction_id"] == question.interaction_id)
            {
                let round = question
                    .interaction_id
                    .rsplit(':')
                    .next()
                    .and_then(|s| s.parse::<usize>().ok())
                    .unwrap_or(rounds.len() + 1);
                let envelope = state
                    .operations
                    .get(&format!("{}-grill-{round}", run.run_id))
                    .and_then(|op| op.result.as_ref())
                    .and_then(|s| serde_json::from_str::<Value>(s).ok());
                rounds.push(json!({"interaction_id":question.interaction_id,"questions":question.questions,"answers":question.answers,"state":"answered","envelope":envelope}));
            }
        }
        if let Some(q) = &state.interaction {
            let round = rounds.len() + 1;
            let envelope = state
                .operations
                .get(&format!("{}-grill-{round}", run.run_id))
                .and_then(|op| op.result.as_ref())
                .and_then(|s| serde_json::from_str::<Value>(s).ok());
            rounds.push(json!({"interaction_id":q.interaction_id,"questions":q.questions,"answers":q.answers,"run_id":run.run_id,"source":"planner","state":"waiting","envelope":envelope}));
        }
        for round in &mut rounds {
            if round.get("state").is_none() {
                round["state"] = json!("answered");
            }
        }
        let pending = rounds.iter().find(|r| r["state"] == "waiting").cloned();
        let mut history = vec![];
        for (index, r) in session.revisions.iter().enumerate() {
            let run = self.runtime.status(&r.run_id()).await?;
            history.push(json!({"revision":index+1,"run_id":run.run_id,"status":run.status,"feedback":r.input.planning.as_ref().unwrap().feedback,"plan_hash":run.workflow.as_ref().and_then(|w|w.planning.as_ref()).and_then(|p|p.plan_hash.as_ref()),"handoff":r.handoff.as_ref().map(|h|handoff_view(Some(h))).or_else(||run.workflow.as_ref().and_then(|w|w.planning.as_ref()).and_then(|p|p.legacy_handoff.clone()))}));
        }
        let implementation = if let Some(h) = &current.handoff {
            let run_id = h.input.run_id();
            Some(match self.runtime.status(&run_id).await {
                Ok(run) => run_view(run, 0),
                Err(Error::NotFound(_)) => {
                    json!({"run_id":run_id,"status":"pending","controller_state":"handoff_pending","interactions":[],"final_report_path":null})
                }
                Err(error) => return Err(error),
            })
        } else {
            planning.and_then(|p| p.legacy_implementation.clone())
        };
        let handoff = current
            .handoff
            .as_ref()
            .map(|h| handoff_view(Some(h)))
            .or_else(|| planning.and_then(|p| p.legacy_handoff.clone()));
        Ok(
            json!({"session_id":id,"revision":session.revisions.len(),"run_id":run.run_id,"status":run.status,"controller_state":"saved","error":run.error,"request":run.input.request,"feedback":current.input.planning.as_ref().unwrap().feedback,"plan_hash":planning.and_then(|p|p.plan_hash.as_ref()),"rounds":rounds,"pending":pending,"snapshot":planning.and_then(|p|p.snapshot.as_ref()),"handoff":handoff,"implementation":implementation,"history":history,"events":run.events.iter().filter(|e|e.sequence>after).collect::<Vec<_>>(),"event_position":run.events.len()}),
        )
    }
}
fn handoff_view(handoff: Option<&Handoff>) -> Value {
    handoff.map(|h|json!({"plan_hash":h.plan_hash,"implementation_run_id":h.input.run_id(),"request_id":h.input.request_id,"actor":h.actor,"approved_at":h.approved_at,"decision_id":h.decision_id,"scope":h.input.implementation.as_ref().unwrap().approved_plan})).unwrap_or(Value::Null)
}
fn text<'a>(value: &'a Value, key: &str) -> Result<&'a str> {
    value
        .get(key)
        .and_then(Value::as_str)
        .filter(|s| !s.trim().is_empty())
        .ok_or_else(|| Error::Config(format!("{key} is required")))
}
pub fn run_view(run: Run, after: u64) -> Value {
    let state = run.workflow.as_ref();
    let mut interactions=state.map(|s|s.interactions.values().map(|q|json!({"interaction_id":q.interaction_id,"questions":q.questions,"answers":q.answers,"run_id":run.run_id,"source":"planner","state":"answered"})).collect::<Vec<_>>()).unwrap_or_default();
    if let Some(q) = state.and_then(|s| s.interaction.as_ref()) {
        interactions.push(json!({"interaction_id":q.interaction_id,"questions":q.questions,"answers":q.answers,"run_id":run.run_id,"source":"planner","state":"waiting"}));
    }
    json!({"run_id":run.run_id,"status":run.status,"request":run.input.request,"error":run.error,"result":run.result,"current_step":state.map(|s|&s.next),"interactions":interactions,"delivery":state.map(|s|&s.delivery),"pull_request":state.and_then(|s|s.delivery.get("pr_url")),"final_report_path":state.filter(|s|s.artifacts.contains_key("handoff.json")).map(|_|"handoff.json"),"settings":run.input.implementation.as_ref().map(|i|&i.config.resolved_settings),"events":run.events.iter().filter(|e|e.sequence>after).collect::<Vec<_>>(),"event_position":run.events.len()})
}

mod migration;
