use crate::{Error, Result};
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Criterion {
    pub id: String,
    pub description: String,
}
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Question {
    pub id: String,
    pub question: String,
    #[serde(default)]
    pub options: Vec<String>,
    pub recommendation: Option<String>,
    pub context: Option<String>,
}
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Plan {
    pub version: u32,
    pub status: String,
    pub summary: String,
    pub acceptance_criteria: Vec<Criterion>,
    pub implementation_steps: Vec<String>,
    #[serde(default)]
    pub scope_exclusions: Vec<String>,
    pub test_strategy: String,
    #[serde(default)]
    pub risks: Vec<String>,
    #[serde(default)]
    pub advisory_test_commands: Vec<String>,
    #[serde(default)]
    pub questions: Vec<Question>,
}
impl Plan {
    pub fn validate(&self) -> Result<()> {
        require(
            self.version == 2
                && ["ready", "blocked", "needs_input"].contains(&self.status.as_str()),
            "invalid plan version/status",
        )?;
        text(&self.summary)?;
        text(&self.test_strategy)?;
        require(
            self.implementation_steps.len() <= 12 && self.questions.len() <= 3,
            "plan exceeds work/question bounds",
        )?;
        unique(self.acceptance_criteria.iter().map(|c| c.id.as_str()))?;
        unique(self.questions.iter().map(|q| q.id.as_str()))?;
        for c in &self.acceptance_criteria {
            identifier(&c.id)?;
            text(&c.description)?;
        }
        for q in &self.questions {
            text(&q.id)?;
            text(&q.question)?;
        }
        for s in self
            .implementation_steps
            .iter()
            .chain(&self.scope_exclusions)
        {
            text(s)?;
        }
        require(
            (self.status != "needs_input" || !self.questions.is_empty())
                && (self.status != "ready"
                    || (self.questions.is_empty() && !self.acceptance_criteria.is_empty())),
            "plan questions/criteria contradict status",
        )
    }
}
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Build {
    pub status: String,
    pub summary: String,
    pub changed_files: Vec<String>,
    #[serde(default)]
    pub blockers: Vec<String>,
    #[serde(default)]
    pub manual_work: Vec<String>,
}
impl Build {
    pub fn validate(&self) -> Result<()> {
        text(&self.summary)?;
        require(
            ["completed", "blocked"].contains(&self.status.as_str()),
            "invalid build status",
        )?;
        unique(self.changed_files.iter().map(String::as_str))?;
        for file in &self.changed_files {
            super::config::safe_relative(file)?;
        }
        Ok(())
    }
}
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Finding {
    pub id: String,
    pub criterion_id: Option<String>,
    pub category: String,
    pub severity: String,
    pub title: String,
    pub evidence: String,
    pub evidence_refs: Vec<String>,
    pub required_change: String,
    pub confidence: String,
    pub file: Option<String>,
    pub line: Option<u32>,
}
impl Finding {
    pub fn blocking(&self) -> bool {
        self.severity != "suggestion"
    }
}
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct CriterionResult {
    pub criterion_id: String,
    pub status: String,
    pub evidence: String,
    pub evidence_refs: Vec<String>,
}
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Resolution {
    pub finding_id: String,
    pub status: String,
    pub evidence: String,
    pub evidence_refs: Vec<String>,
}
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Assessment {
    pub reviewer: String,
    pub finding_id: String,
    pub disposition: String,
    pub reason: String,
    pub evidence_refs: Vec<String>,
    pub retained_finding_id: Option<String>,
}
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Review {
    pub version: u32,
    pub content_version: String,
    pub approved: bool,
    pub summary: String,
    pub findings: Vec<Finding>,
    pub criteria: Vec<CriterionResult>,
    #[serde(default)]
    pub resolutions: Vec<Resolution>,
    #[serde(default)]
    pub manual_work: Vec<String>,
    // Absent in the single-review envelope. Required for judge decisions.
    pub assessments: Option<Vec<Assessment>>,
}
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct FocusedReview {
    pub version: u32,
    pub reviewer: String,
    pub content_version: String,
    pub summary: String,
    pub findings: Vec<Finding>,
    #[serde(default)]
    pub criteria: Vec<CriterionResult>,
    #[serde(default)]
    pub manual_work: Vec<String>,
}
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct FindingHistory {
    pub finding: Finding,
    pub status: String,
    pub history: Vec<Resolution>,
}

pub(crate) struct ReviewContext<'a> {
    pub plan: &'a Plan,
    pub content_version: &'a str,
    pub evidence_refs: &'a BTreeSet<String>,
    pub source_lines: &'a BTreeMap<String, usize>,
}
impl ReviewContext<'_> {
    fn validate_parts(
        &self,
        findings: &[Finding],
        criteria: &[CriterionResult],
        requires_criteria: bool,
    ) -> Result<()> {
        unique(findings.iter().map(|f| f.id.as_str()))?;
        unique(criteria.iter().map(|c| c.criterion_id.as_str()))?;
        let ids: BTreeSet<_> = self
            .plan
            .acceptance_criteria
            .iter()
            .map(|c| c.id.as_str())
            .collect();
        let actual: BTreeSet<_> = criteria.iter().map(|c| c.criterion_id.as_str()).collect();
        require(
            if requires_criteria {
                actual == ids
            } else {
                actual.is_empty()
            },
            "review must cover exactly the required criteria",
        )?;
        for c in criteria {
            text(&c.evidence)?;
            require(
                ["satisfied", "unmet", "not_verified"].contains(&c.status.as_str()),
                "invalid criterion result",
            )?;
            self.refs(&c.evidence_refs, c.status != "not_verified")?;
        }
        for f in findings {
            identifier(&f.id)?;
            for s in [&f.title, &f.evidence, &f.required_change] {
                text(s)?;
            }
            require(
                ["critical", "warning", "suggestion"].contains(&f.severity.as_str())
                    && ["high", "medium", "low"].contains(&f.confidence.as_str())
                    && [
                        "correctness",
                        "security",
                        "reliability",
                        "performance",
                        "testing",
                        "scope",
                        "evidence",
                        "documentation",
                    ]
                    .contains(&f.category.as_str()),
                "invalid finding classification",
            )?;
            require(
                f.criterion_id
                    .as_ref()
                    .is_none_or(|id| ids.contains(id.as_str())),
                "finding refers to unknown criterion",
            )?;
            self.refs(&f.evidence_refs, true)?;
            if let Some(file) = &f.file {
                let lines = self
                    .source_lines
                    .get(file)
                    .ok_or_else(|| Error::Config("finding refers to unknown source".into()))?;
                require(
                    f.line.is_none_or(|n| n >= 1 && n as usize <= *lines),
                    "invalid finding source line",
                )?;
            } else {
                require(f.line.is_none(), "finding line requires source file")?;
            }
        }
        Ok(())
    }
    fn refs(&self, refs: &[String], nonempty: bool) -> Result<()> {
        require(
            (!nonempty || !refs.is_empty()) && refs.iter().all(|s| self.evidence_refs.contains(s)),
            "missing or invalid evidence references",
        )
    }
    pub fn focused(&self, role: &str, review: &FocusedReview) -> Result<()> {
        text(&review.summary)?;
        require(
            review.version == 1
                && review.reviewer == role
                && review.content_version == self.content_version,
            "focused review role/version/content mismatch",
        )?;
        self.validate_parts(&review.findings, &review.criteria, role == "request")
    }
    pub fn decision(
        &self,
        review: &Review,
        history: &BTreeMap<String, FindingHistory>,
        focused: Option<&BTreeMap<String, FocusedReview>>,
    ) -> Result<BTreeMap<String, FindingHistory>> {
        text(&review.summary)?;
        require(
            review.version == 2 && review.content_version == self.content_version,
            "stale review content/version",
        )?;
        self.validate_parts(&review.findings, &review.criteria, true)?;
        let blocked = review.findings.iter().any(Finding::blocking)
            || review.criteria.iter().any(|c| c.status != "satisfied");
        require(
            review.approved != blocked,
            "review decision contradicts findings or criteria",
        )?;
        unique(review.resolutions.iter().map(|r| r.finding_id.as_str()))?;
        let resolutions: BTreeMap<_, _> = review
            .resolutions
            .iter()
            .map(|r| (&r.finding_id, r))
            .collect();
        require(
            resolutions.keys().copied().collect::<BTreeSet<_>>() == history.keys().collect(),
            "decision must resolve every previous finding",
        )?;
        let current: BTreeMap<_, _> = review.findings.iter().map(|f| (&f.id, f)).collect();
        let mut next = history.clone();
        for (id, old) in history {
            let resolution = resolutions[id];
            text(&resolution.evidence)?;
            self.refs(&resolution.evidence_refs, true)?;
            let active = current.get(id);
            let expected = if active.is_some() {
                if old.status == "fixed" {
                    "reopened"
                } else {
                    "unresolved"
                }
            } else {
                "fixed"
            };
            require(
                resolution.status == expected,
                "finding resolution contradicts history/current finding",
            )?;
            require(
                active.is_none_or(|f| !old.finding.blocking() || f.blocking()),
                "blocking finding cannot be downgraded",
            )?;
            let entry = next.get_mut(id).unwrap();
            entry.status = expected.into();
            entry.history.push(resolution.clone());
        }
        for f in &review.findings {
            next.entry(f.id.clone())
                .and_modify(|h| h.finding = f.clone())
                .or_insert(FindingHistory {
                    finding: f.clone(),
                    status: "unresolved".into(),
                    history: vec![],
                });
        }
        if let Some(reviews) = focused {
            let assessments = review
                .assessments
                .as_ref()
                .ok_or_else(|| Error::Config("judge must assess every focused finding".into()))?;
            let candidates: BTreeMap<_, _> = reviews
                .iter()
                .flat_map(|(role, r)| {
                    r.findings
                        .iter()
                        .map(move |f| ((role.as_str(), f.id.as_str()), f))
                })
                .collect();
            let mut seen = BTreeSet::new();
            let mut retained = BTreeSet::new();
            for a in assessments {
                require(
                    seen.insert((a.reviewer.as_str(), a.finding_id.as_str())),
                    "duplicate judge assessment",
                )?;
                let original = candidates
                    .get(&(a.reviewer.as_str(), a.finding_id.as_str()))
                    .ok_or_else(|| Error::Config("unknown judge candidate".into()))?;
                text(&a.reason)?;
                self.refs(&a.evidence_refs, true)?;
                match a.disposition.as_str() {
                    "unsupported" => require(
                        a.retained_finding_id.is_none(),
                        "unsupported finding cannot have a target",
                    )?,
                    "retained" | "duplicate" => {
                        let target = a
                            .retained_finding_id
                            .as_ref()
                            .and_then(|id| current.get(id))
                            .ok_or_else(|| {
                                Error::Config("assessment target is not a final finding".into())
                            })?;
                        require(
                            !original.blocking() || target.blocking(),
                            "judge cannot downgrade retained blockers",
                        )?;
                        if a.disposition == "retained" {
                            require(
                                retained.insert(target.id.as_str()),
                                "duplicate retained target",
                            )?;
                        }
                    }
                    _ => return Err(Error::Config("invalid assessment disposition".into())),
                }
            }
            require(
                seen == candidates.keys().copied().collect()
                    && retained == current.keys().map(|s| s.as_str()).collect(),
                "judge must assess every candidate and support every final finding",
            )?;
        } else {
            require(
                review.assessments.is_none(),
                "single reviewer cannot return judge assessments",
            )?;
        }
        Ok(next)
    }
}
pub(crate) fn require(condition: bool, message: &str) -> Result<()> {
    if condition {
        Ok(())
    } else {
        Err(Error::Config(message.into()))
    }
}
pub(crate) fn text(value: &str) -> Result<()> {
    require(!value.trim().is_empty(), "required text is empty")
}
pub(crate) fn identifier(value: &str) -> Result<()> {
    require(
        value.as_bytes().first().is_some_and(u8::is_ascii_lowercase)
            && value
                .bytes()
                .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b"_-".contains(&b)),
        "invalid stable identifier",
    )
}
pub(crate) fn unique<'a>(values: impl Iterator<Item = &'a str>) -> Result<()> {
    let mut seen = BTreeSet::new();
    for value in values {
        require(seen.insert(value), "duplicate identifier")?;
    }
    Ok(())
}
