//! Offline, explicit import. Original records and provider receipts remain untouched.
use super::*;
use crate::{
    Operation, OperationState,
    planning::{GrillEnvelope, PlanningState},
    workflow::SavedInteraction,
};
impl Control {
    pub(crate) fn maintenance_lock(&self) -> Result<std::fs::File> {
        let path = self.runtime.config.data_dir.join("control-service.lock");
        let lock = std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(path)?;
        lock.try_lock().map_err(|_| Error::Busy)?;
        Ok(lock)
    }

    pub async fn import_planning(&self, archive: Value) -> Result<Value> {
        let _maintenance = self.maintenance_lock()?;
        if archive["version"] != 1 || archive["workspace"].as_str() != self.workspace().to_str() {
            return Err(Error::Conflict("archive version/workspace mismatch".into()));
        }
        let sessions = archive["sessions"]
            .as_array()
            .ok_or_else(|| Error::Config("archive sessions required".into()))?;
        let hash = digest(serde_json::to_string(&archive)?.as_bytes());
        // Retain the complete source records as provenance. No receipt is relabelled as Rust evidence.
        let destination = self
            .runtime
            .config
            .data_dir
            .join("imports")
            .join(format!("{hash}.json"));
        crate::workflow::artifacts::atomic(&destination, &serde_json::to_vec_pretty(&archive)?)?;
        let mut imported = vec![];
        for source in sessions {
            let id = text(source, "session_id")?;
            let source_revisions = source["revisions"]
                .as_array()
                .filter(|r| !r.is_empty())
                .ok_or_else(|| Error::Config("empty archive session".into()))?;
            let mut revisions = vec![];
            for (index, source) in source_revisions.iter().enumerate() {
                let source_run = text(source, "source_run_id")?;
                let mut input = StartRequest::new(
                    &format!(
                        "import-{}",
                        digest(format!("{id}\0{source_run}").as_bytes())
                    ),
                    id,
                    text(source, "request")?,
                );
                let config: WorkflowConfig =
                    serde_json::from_value(source["configuration"].clone())?;
                config.validate()?;
                input.implementation = Some(ImplementationInput {
                    config,
                    required_criteria: vec![],
                    approved_plan: None,
                });
                input.planning = Some(PlanningInput {
                    session_id: id.into(),
                    revision: index as u32 + 1,
                    feedback: source["feedback"].as_str().map(String::from),
                    previous: source["source_record"]["planning"]
                        .get("previous")
                        .filter(|v| !v.is_null())
                        .cloned(),
                });
                input.validate()?;
                let run_id = input.run_id();
                let status = match source["status"].as_str() {
                    Some("planned") => Status::Planned,
                    Some("waiting_for_input") => Status::WaitingForInput,
                    Some("cancelled") => Status::Cancelled,
                    Some("blocked") => Status::Blocked,
                    Some("failed") => Status::Failed,
                    _ => {
                        return Err(Error::Conflict(
                            "active Python runs must be reconciled before import".into(),
                        ));
                    }
                };
                let snapshot = source.get("snapshot").filter(|s| !s.is_null()).cloned();
                let content = source["snapshot_content"].as_str().map(String::from);
                if status == Status::Planned {
                    let content = content.as_ref().ok_or_else(|| {
                        Error::Integrity("missing original snapshot bytes".into())
                    })?;
                    if source["plan_hash"].as_str() != Some(&digest(content.as_bytes()))
                        || serde_json::from_str::<Value>(content)?
                            != snapshot.clone().unwrap_or(Value::Null)
                    {
                        return Err(Error::Integrity(
                            "original approved snapshot hash mismatch".into(),
                        ));
                    }
                }
                let mut planning = PlanningState {
                    source_archive_sha256: Some(hash.clone()),
                    documents: source["documents"].clone(),
                    snapshot,
                    snapshot_content: content,
                    plan_hash: source["plan_hash"].as_str().map(String::from),
                    legacy_handoff: source
                        .get("legacy_handoff")
                        .filter(|v| !v.is_null())
                        .cloned(),
                    legacy_implementation: source
                        .get("legacy_implementation")
                        .filter(|v| !v.is_null())
                        .cloned(),
                    ..Default::default()
                };
                let rounds = source["rounds"]
                    .as_array()
                    .ok_or_else(|| Error::Config("archive rounds required".into()))?;
                let mut answered = std::collections::BTreeMap::new();
                let mut pending = None;
                for round in rounds {
                    let mut question: SavedInteraction = serde_json::from_value(
                        json!({"interaction_id":round["interaction_id"],"questions":round["questions"],"answers":round["answers"]}),
                    )?;
                    let envelope: GrillEnvelope =
                        serde_json::from_value(round["envelope"].clone())?;
                    envelope.validate()?;
                    question.interaction_id =
                        format!("{}:grill:{}", run_id, planning.rounds.len() + 1);
                    if question.answers.is_some() {
                        answered.insert(question.interaction_id.clone(), question.clone());
                        let mut saved = round.clone();
                        saved["interaction_id"] = json!(question.interaction_id);
                        planning.rounds.push(saved);
                    } else {
                        if pending.is_some() {
                            return Err(Error::Config("multiple pending rounds".into()));
                        }
                        pending = Some((question, envelope));
                    }
                }
                if status == Status::WaitingForInput && pending.is_none() {
                    return Err(Error::Integrity(
                        "waiting revision has no saved questions".into(),
                    ));
                }
                if let Ok(existing) = self.runtime.status(&run_id).await {
                    if existing
                        .workflow
                        .as_ref()
                        .and_then(|w| w.planning.as_ref())
                        .and_then(|p| p.source_archive_sha256.as_deref())
                        != Some(&hash)
                    {
                        return Err(Error::Conflict("original import archive differs".into()));
                    }
                    if let Some(snapshot) = &planning.snapshot {
                        crate::workflow::artifacts::write(
                            &self.runtime,
                            &run_id,
                            "plan-snapshot.json",
                            snapshot,
                        )
                        .await?;
                    }
                    if existing.input != input {
                        return Err(Error::Conflict("imported revision input changed".into()));
                    }
                    revisions.push(Revision {
                        input,
                        handoff: None,
                    });
                    continue;
                }
                let now = chrono::Utc::now();
                let mut workflow = crate::workflow::WorkflowState {
                    planning: Some(planning.clone()),
                    interactions: answered,
                    overall_deadline_at: Some(now + chrono::Duration::days(7)),
                    outcome: status.is_terminal().then_some(status),
                    ..Default::default()
                };
                if let Some((question, envelope)) = pending {
                    workflow.interaction = Some(question);
                    workflow.wait_started_at = Some(now);
                    let key = format!("{run_id}-grill-{}", planning.rounds.len() + 1);
                    workflow.operations.insert(
                        key.clone(),
                        Operation {
                            operation_id: key,
                            state: OperationState::Completed,
                            dispatch_count: 0,
                            result: Some(serde_json::to_string(&envelope)?),
                        },
                    );
                }
                let mut events: Vec<crate::contracts::Event> = serde_json::from_value(
                    source["source_record"]
                        .get("events")
                        .cloned()
                        .unwrap_or(json!([])),
                )?;
                for (index, event) in events.iter_mut().enumerate() {
                    event
                        .metadata
                        .insert("source_run_id".into(), json!(event.run_id));
                    event
                        .metadata
                        .insert("source_sequence".into(), json!(event.sequence));
                    event.run_id = run_id.clone();
                    event.sequence = index as u64 + 1;
                }
                let mut run = Run {
                    schema_version: crate::contracts::SCHEMA_VERSION,
                    graph_version: crate::planning::GRAPH_VERSION.into(),
                    run_id: run_id.clone(),
                    workspace_id: self.runtime.workspace_id.clone(),
                    checkpoint_thread_id: format!("factory:{run_id}"),
                    scheduler_id: None,
                    input: input.clone(),
                    revision: 0,
                    status,
                    interaction: None,
                    operation: None,
                    attempts: 0,
                    deadline_at: now + chrono::Duration::hours(1),
                    cancel_requested: false,
                    result: None,
                    error: source["source_record"]["error"].as_str().map(String::from),
                    events,
                    workflow: Some(Box::new(workflow)),
                };
                run.event(
                    "migration",
                    "info",
                    &format!(
                        "Imported quiescent Python revision {source_run}; original archive {hash}"
                    ),
                );
                self.runtime.services.store.create(&run).await?;
                if let Some(snapshot) = &planning.snapshot {
                    crate::workflow::artifacts::write(
                        &self.runtime,
                        &run_id,
                        "plan-snapshot.json",
                        snapshot,
                    )
                    .await?;
                }
                revisions.push(Revision {
                    input,
                    handoff: None,
                });
            }
            let session = Session {
                version: 1,
                session_id: id.into(),
                workspace_id: self.runtime.workspace_id.clone(),
                created_at: text(source, "created_at")?.into(),
                revisions,
            };
            let exists: Option<String> =
                sqlx::query_scalar("SELECT document FROM planning_sessions WHERE session_id=?")
                    .bind(id)
                    .fetch_optional(&self.pool)
                    .await?;
            if let Some(existing) = exists {
                let existing: Session = serde_json::from_str(&existing)?;
                if existing.workspace_id != session.workspace_id
                    || existing.revisions.len() < session.revisions.len()
                    || !session
                        .revisions
                        .iter()
                        .zip(&existing.revisions)
                        .all(|(a, b)| a.input == b.input)
                {
                    return Err(Error::Conflict(
                        "existing planning history differs from archive".into(),
                    ));
                }
            } else {
                sqlx::query("INSERT INTO planning_sessions VALUES (?,?,?)")
                    .bind(id)
                    .bind(&session.workspace_id)
                    .bind(serde_json::to_string(&session)?)
                    .execute(&self.pool)
                    .await?;
            }
            imported.push(id);
        }
        Ok(json!({"imported":imported,"archive_sha256":hash}))
    }
}
