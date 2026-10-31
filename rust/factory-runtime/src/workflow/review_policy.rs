use super::{config::ReviewPolicy, repository::RepositorySnapshot};
use crate::Result;
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};

// These fixed protections cannot be removed by generated/documentation globs.
const INSTRUCTION_PATHS: &[&str] = &[
    "*deploy*",
    ".env*",
    ".github/workflows/",
    ".github/actions/",
    ".gitlab-ci*",
    "Jenkinsfile*",
    "Dockerfile*",
    "*.dockerfile",
    "compose*.y*ml",
    "docker-compose*.y*ml",
    "*.tf",
    "*.tfvars",
    "package.json",
    "package-lock.json",
    "npm-shrinkwrap.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "bun.lock*",
    ".npmrc",
    "pyproject.toml",
    "uv.lock",
    "requirements*.txt",
    "setup.py",
    "setup.cfg",
    "Pipfile*",
    "poetry.lock",
    "Cargo.toml",
    "Cargo.lock",
    "Gemfile*",
    "go.mod",
    "go.sum",
    "pom.xml",
    "*.gradle*",
    "gradle.lockfile",
    "composer.json",
    "composer.lock",
    "Makefile*",
    "justfile",
    "*.sh",
    "*.bash",
    "*.ps1",
    "AGENTS.md",
    "CLAUDE.md",
    ".factory/",
    "tools/",
    "scripts/",
    ".gitmodules",
    "*.csproj",
    "*.fsproj",
    "Directory.Build.*",
    "Directory.Packages.props",
];
const SENSITIVE_PATHS: &[&str] = &[
    "*auth*",
    "*secret*",
    "*credential*",
    "*password*",
    "*crypto*",
    "*encrypt*",
    "*permission*",
    "*session*",
    "*token*",
    "*migration*",
    "*.pem",
    "*.key",
];
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ReviewSelection {
    pub version: u32,
    pub content_version: String,
    pub policy: ReviewPolicy,
    pub level: String,
    pub reasons: Vec<String>,
    pub required_reviewers: Vec<String>,
    pub changed_lines: usize,
    pub changed_file_count: usize,
    pub measured_file_count: usize,
    pub sensitive_files: Vec<String>,
    pub unmeasurable_files: Vec<String>,
    pub generated_files: Vec<String>,
    pub review_files: Vec<String>,
    pub instruction_reasons: Vec<String>,
}
fn matches(path: &str, patterns: impl IntoIterator<Item = impl AsRef<str>>) -> bool {
    let path = path.to_ascii_lowercase();
    let parts: Vec<_> = path.split('/').collect();
    patterns.into_iter().any(|pattern| {
        let p = pattern.as_ref().to_ascii_lowercase();
        let Ok(g) = globset::Glob::new(p.trim_end_matches('/')) else {
            return false;
        };
        let matcher = g.compile_matcher();
        if p.ends_with('/') {
            (1..=parts.len()).any(|n| matcher.is_match(parts[..n].join("/")))
                || (!p.trim_end_matches('/').contains('/')
                    && parts.iter().any(|s| matcher.is_match(s)))
        } else if !p.contains('/') {
            parts.iter().any(|s| matcher.is_match(s))
        } else {
            matcher.is_match(&path)
                || p.strip_prefix("**/").is_some_and(|p| {
                    globset::Glob::new(p)
                        .unwrap()
                        .compile_matcher()
                        .is_match(&path)
                })
        }
    })
}
pub(crate) fn select(
    policy: &ReviewPolicy,
    patches: &BTreeMap<String, String>,
    content: &str,
    baseline: &RepositorySnapshot,
    current: &RepositorySnapshot,
) -> Result<ReviewSelection> {
    let sensitive_content = regex::Regex::new(r"(?i)auth(?:entication|orization|enticate|orize)?|secret|credential|password|cryptograph|encrypt|decrypt|migration|\bdeploy\b|\bsubprocess\b|child_process|\b(?:exec|eval)\s*\(|\bos\.system\b|\b(?:pickle|yaml)\.load\b|\b(?:preinstall|postinstall|install_requires|jwt|oauth|hmac|hashlib|token|session|permission|rbac|acl)\b").unwrap();
    let instruction_content = regex::Regex::new(r"\b(?:getenv|environ|process\.env|subprocess|child_process|argparse|typer|commands?|cli|tools?)\b|\$\{?[A-Z][A-Z0-9_]+|--[a-z][a-z-]+").unwrap();
    let mode = regex::Regex::new(
        r"(?m)^(?:new file mode|deleted file mode|old mode|new mode) (?:120000|160000)$",
    )
    .unwrap();
    let sensitive_mode = regex::Regex::new(
        r"(?m)^(?:new file mode|deleted file mode|old mode|new mode) (?:100755|160000)$",
    )
    .unwrap();
    let mut selected = ReviewSelection {
        version: 1,
        content_version: content.into(),
        policy: policy.clone(),
        level: String::new(),
        reasons: vec![],
        required_reviewers: vec!["request".into()],
        changed_lines: 0,
        changed_file_count: patches.len(),
        measured_file_count: 0,
        sensitive_files: vec![],
        unmeasurable_files: vec![],
        generated_files: vec![],
        review_files: vec![],
        instruction_reasons: vec![],
    };
    let mut docs_changed = false;
    for (path, patch) in patches {
        let mut in_hunk = false;
        let mut lines = vec![];
        let mut context = String::new();
        for line in patch.lines() {
            if line.starts_with("@@ ") {
                in_hunk = true;
                context.push_str(line);
                context.push('\n');
            } else if line.starts_with("diff --git ") {
                in_hunk = false;
            } else if in_hunk && line.starts_with(['+', '-', ' ']) {
                context.push_str(&line[1..]);
                context.push('\n');
                if line.starts_with(['+', '-']) {
                    lines.push(&line[1..]);
                }
            }
        }
        let sensitive = matches(
            path,
            INSTRUCTION_PATHS
                .iter()
                .chain(SENSITIVE_PATHS)
                .copied()
                .chain(policy.sensitive_paths.iter().map(String::as_str)),
        ) || sensitive_content.is_match(&context)
            || sensitive_mode.is_match(patch);
        if sensitive {
            selected.sensitive_files.push(path.clone());
        }
        if !sensitive && matches(path, &policy.generated_paths) {
            selected.generated_files.push(path.clone());
            continue;
        }
        selected.review_files.push(path.clone());
        selected.changed_lines += lines.len();
        if patch.contains("GIT binary patch")
            || patch.contains("Binary files ")
            || mode.is_match(patch)
            || (!patch.trim().is_empty() && lines.is_empty())
        {
            selected.unmeasurable_files.push(path.clone());
        }
        docs_changed |= matches(path, &policy.documentation_paths);
        if matches(path, INSTRUCTION_PATHS) || instruction_content.is_match(&context) {
            selected.instruction_reasons.push(format!(
                "Tools, commands, environment, or project rules: {path}"
            ));
        }
    }
    let directories = |snapshot: &RepositorySnapshot| -> BTreeSet<String> {
        snapshot
            .files
            .iter()
            .filter(|(p, e)| e.kind != "missing" && !selected.generated_files.contains(p))
            .flat_map(|(p, _)| {
                std::path::Path::new(p)
                    .ancestors()
                    .skip(1)
                    .filter(|p| !p.as_os_str().is_empty())
                    .map(|p| p.to_string_lossy().into_owned())
                    .collect::<Vec<_>>()
            })
            .collect()
    };
    if directories(baseline) != directories(current) {
        selected
            .instruction_reasons
            .push("Repository directory structure changed".into());
    }
    selected.measured_file_count = selected.review_files.len();
    if !selected.sensitive_files.is_empty() || !selected.unmeasurable_files.is_empty() {
        selected.level = "full".into();
        if !selected.sensitive_files.is_empty() {
            selected
                .reasons
                .push("Sensitive paths or changed content require security review".into());
        }
        if !selected.unmeasurable_files.is_empty() {
            selected
                .reasons
                .push("Some changes cannot be measured as text lines".into());
        }
    } else if patches.is_empty()
        || (!selected.review_files.is_empty()
            && patches
                .keys()
                .all(|p| matches(p, &policy.documentation_paths))
            && selected.changed_lines <= policy.trivial_max_lines
            && selected.measured_file_count <= policy.normal_max_files)
    {
        selected.level = "trivial".into();
        selected.reasons.push(
            if patches.is_empty() {
                "No changes"
            } else {
                "Documentation within the trivial limits"
            }
            .into(),
        );
    } else if selected.changed_lines <= policy.normal_max_lines
        && selected.measured_file_count <= policy.normal_max_files
    {
        selected.level = "normal".into();
        selected
            .reasons
            .push("Both normal size limits are satisfied".into());
    } else {
        selected.level = "full".into();
        selected
            .reasons
            .push("The normal line or file limit is exceeded".into());
    }
    if selected.level != "trivial" {
        selected.required_reviewers.push("code".into());
    }
    if selected.level == "full" {
        selected.required_reviewers.push("security".into());
    }
    if docs_changed || !selected.instruction_reasons.is_empty() {
        selected.required_reviewers.push("documentation".into());
        selected
            .reasons
            .push("Documentation or project instructions need review".into());
    }
    Ok(selected)
}
