//! Service-side client operations share the existing verified runtime boundaries.
use super::*;
impl Control {
    pub(super) async fn client_operation(&self, operation: &str, value: &Value) -> Result<Value> {
        let id = text(value, "run_id")?;
        let run = self.runtime.status(id).await?; // resolves workspace ownership before any action
        match operation {
            "deliver" | "fix_reviews" => {
                let runtime = self.runtime.clone();
                let id = id.to_string();
                let policy = value
                    .get("policy")
                    .filter(|v| !v.is_null())
                    .map(|v| serde_json::from_value(v.clone()))
                    .transpose()?;
                let threads: Vec<String> =
                    serde_json::from_value(value.get("threads").cloned().unwrap_or(json!([])))?;
                let fixes = operation == "fix_reviews";
                // Dropping an HTTP response/adapter must not drop execution futures.
                let task = tokio::spawn(async move {
                    if fixes {
                        runtime.fix_reviews(&id, &threads).await
                    } else {
                        runtime.deliver(&id, policy).await
                    }
                });
                Ok(run_view(
                    task.await.map_err(|e| Error::Worker(e.to_string()))??,
                    0,
                ))
            }
            "review_threads" => {
                use crate::workflow::delivery::Remote;
                let state = run
                    .workflow
                    .as_ref()
                    .ok_or_else(|| Error::Config("not an implementation run".into()))?;
                let repository =
                    state.delivery["policy"]["repository"]
                        .as_str()
                        .ok_or_else(|| {
                            Error::Config(
                                "deliver the verified PR before reading review threads".into(),
                            )
                        })?;
                let number = state.delivery["pr_number"]
                    .as_u64()
                    .ok_or_else(|| Error::Config("no saved PR number".into()))?;
                let remote = crate::workflow::github::GitHub {
                    workspace: self.workspace().to_path_buf(),
                };
                Ok(json!({"run_id":id,"threads":remote.review_threads(repository,number).await?}))
            }
            "review_quality" | "review_feedback" => {
                let state = run
                    .workflow
                    .as_ref()
                    .ok_or_else(|| Error::Config("not an implementation run".into()))?;
                crate::workflow::artifacts::verify(&self.runtime, id).await?;
                let mut evidence = serde_json::Map::new();
                for path in state
                    .artifacts
                    .keys()
                    .filter(|p| p.starts_with("review/") || p.as_str() == "review.json")
                {
                    let raw = crate::workflow::artifacts::regular(
                        &self
                            .runtime
                            .config
                            .data_dir
                            .join("evidence")
                            .join(id)
                            .join(path),
                    )?;
                    evidence.insert(path.clone(), serde_json::from_slice::<Value>(&raw)?);
                }
                if operation == "review_feedback" {
                    let attempt = value["attempt"]
                        .as_u64()
                        .filter(|a| *a > 0 && *a < 10000)
                        .ok_or_else(|| Error::Config("attempt must be positive".into()))?;
                    let reviewer = text(value, "reviewer")?;
                    if ![
                        "request",
                        "code",
                        "security",
                        "documentation",
                        "review",
                        "judge",
                    ]
                    .contains(&reviewer)
                    {
                        return Err(Error::Config("unknown reviewer".into()));
                    }
                    let finding = text(value, "finding_id")?;
                    let verdict = text(value, "verdict")?;
                    if !["useful", "false_positive"].contains(&verdict) {
                        return Err(Error::Config(
                            "verdict must be useful or false_positive".into(),
                        ));
                    }
                    let reason = text(value, "reason")?;
                    let path = if matches!(reviewer, "review" | "judge") {
                        format!("review/decision-{attempt:02}.json")
                    } else {
                        format!("review/{reviewer}-{attempt:02}.json")
                    };
                    if !evidence
                        .get(&path)
                        .and_then(|r| r["findings"].as_array())
                        .is_some_and(|a| a.iter().any(|f| f["id"] == finding))
                    {
                        return Err(Error::Config(
                            "feedback must reference an existing saved reviewer finding".into(),
                        ));
                    }
                    let feedback = json!({"attempt":attempt,"reviewer":reviewer,"finding_id":finding,"verdict":verdict,"reason":reason,"recorded_at":chrono::Utc::now().to_rfc3339()});
                    sqlx::query(
                        "INSERT INTO review_feedback (workspace_id,run_id,document) VALUES (?,?,?)",
                    )
                    .bind(&self.runtime.workspace_id)
                    .bind(id)
                    .bind(serde_json::to_string(&feedback)?)
                    .execute(&self.pool)
                    .await?;
                }
                let feedback:Vec<String>=sqlx::query_scalar("SELECT document FROM review_feedback WHERE workspace_id=? AND run_id=? ORDER BY sequence").bind(&self.runtime.workspace_id).bind(id).fetch_all(&self.pool).await?;
                let feedback = feedback
                    .iter()
                    .map(|s| serde_json::from_str::<Value>(s))
                    .collect::<std::result::Result<Vec<_>, _>>()?;
                Ok(
                    json!({"version":1,"run_id":id,"evidence":evidence,"feedback":feedback,"model_fallback":"disabled"}),
                )
            }
            _ => unreachable!(),
        }
    }
}
