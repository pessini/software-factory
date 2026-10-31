use super::config::safe_relative;
use crate::{Error, Result, runtime::digest};
use serde::{Deserialize, Serialize};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    os::unix::fs::PermissionsExt,
    path::{Path, PathBuf},
};
use tokio::process::Command;

pub(crate) async fn git(root: &Path, args: &[&str]) -> Result<Vec<u8>> {
    let output = Command::new("git")
        .args(args)
        .current_dir(root)
        .env("GIT_OPTIONAL_LOCKS", "0")
        .kill_on_drop(true)
        .output()
        .await?;
    if !output.status.success() {
        return Err(Error::Config(
            String::from_utf8_lossy(&output.stderr).trim().into(),
        ));
    }
    Ok(output.stdout)
}
fn paths(raw: Vec<u8>) -> Result<BTreeSet<String>> {
    raw.split(|b| *b == 0)
        .filter(|s| !s.is_empty())
        .map(|s| {
            String::from_utf8(s.to_vec())
                .map_err(|_| Error::Config("non-UTF8 repository paths are unsupported".into()))
        })
        .collect()
}
pub(crate) fn internal(path: &str) -> bool {
    [
        ".factory-rust.lock",
        ".factory-rust.claim",
        ".factory-rust.identity",
    ]
    .contains(&path)
}
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct FileEntry {
    pub kind: String,
    pub sha256: String,
    pub mode: u32,
    pub lines: usize,
}
fn entry(root: &Path, relative: &str) -> Result<FileEntry> {
    safe_relative(relative)?;
    let path = root.join(relative);
    let mut ancestor = path.parent().unwrap();
    while !ancestor.exists() && !ancestor.is_symlink() {
        ancestor = ancestor.parent().unwrap();
    }
    if !ancestor.canonicalize()?.starts_with(root) {
        return Err(Error::Config(
            "repository path escapes through a symbolic link".into(),
        ));
    }
    match fs::symlink_metadata(&path) {
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(FileEntry {
            kind: "missing".into(),
            sha256: String::new(),
            mode: 0,
            lines: 0,
        }),
        Err(e) => Err(e.into()),
        Ok(meta) => {
            let (kind, bytes) = if meta.is_symlink() {
                (
                    "symlink",
                    fs::read_link(&path)?
                        .as_os_str()
                        .as_encoded_bytes()
                        .to_vec(),
                )
            } else if meta.is_file() {
                ("file", fs::read(&path)?)
            } else if meta.is_dir() {
                ("directory", vec![])
            } else {
                return Err(Error::Config("repository contains a special file".into()));
            };
            let lines = if bytes.is_empty() {
                0
            } else {
                bytes.split(|b| *b == b'\n').count() - usize::from(bytes.ends_with(b"\n"))
            };
            Ok(FileEntry {
                kind: kind.into(),
                sha256: digest(&bytes),
                mode: meta.permissions().mode() & 0o7777,
                lines,
            })
        }
    }
}
pub(crate) fn tree(root: &Path) -> Result<BTreeMap<String, FileEntry>> {
    fn walk(root: &Path, path: &Path, result: &mut BTreeMap<String, FileEntry>) -> Result<()> {
        for child in fs::read_dir(path)? {
            let child = child?.path();
            let relative = child
                .strip_prefix(root)
                .unwrap()
                .to_str()
                .ok_or_else(|| Error::Config("non-UTF8 path".into()))?
                .to_owned();
            let e = entry(root, &relative)?;
            let directory = e.kind == "directory";
            result.insert(relative, e);
            if directory {
                walk(root, &child, result)?;
            }
        }
        Ok(())
    }
    let mut result = BTreeMap::new();
    walk(root, root, &mut result)?;
    Ok(result)
}
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct RepositorySnapshot {
    pub files: BTreeMap<String, FileEntry>,
    pub ignored: BTreeMap<String, FileEntry>,
    pub protected: BTreeMap<String, FileEntry>,
    pub git_metadata: BTreeMap<String, BTreeMap<String, FileEntry>>,
    pub head: String,
    pub head_ref: String,
    pub index: String,
    pub refs: String,
}
impl RepositorySnapshot {
    pub fn content_version(&self) -> Result<String> {
        Ok(digest(&serde_json::to_vec(self)?))
    }
    pub fn check_protection(&self, current: &Self, read_only: bool) -> Result<()> {
        if self.head != current.head
            || self.head_ref != current.head_ref
            || self.index != current.index
            || self.refs != current.refs
            || self.git_metadata != current.git_metadata
            || self.protected != current.protected
            || self.ignored != current.ignored
            || (read_only && self.files != current.files)
        {
            return Err(Error::Config(
                "protected repository state changed; workspace preserved for inspection".into(),
            ));
        }
        Ok(())
    }
}
pub(crate) fn protected_paths(
    configured: &[String],
    files: &BTreeMap<String, FileEntry>,
) -> Vec<String> {
    let mut paths: BTreeSet<_> = [
        ".factory",
        ".bb",
        ".github/workflows",
        ".github/actions",
        ".gitmodules",
    ]
    .map(String::from)
    .into();
    paths.extend(
        configured
            .iter()
            .map(|p| p.trim_end_matches('/').to_owned()),
    );
    paths.extend(
        files
            .keys()
            .filter(|p| {
                matches!(
                    Path::new(p).file_name().and_then(|s| s.to_str()),
                    Some("AGENTS.md" | "CLAUDE.md")
                )
            })
            .cloned(),
    );
    paths.into_iter().collect()
}
pub(crate) async fn snapshot(root: &Path, configured: &[String]) -> Result<RepositorySnapshot> {
    let actual = String::from_utf8_lossy(&git(root, &["rev-parse", "--show-toplevel"]).await?)
        .trim()
        .to_owned();
    if Path::new(&actual).canonicalize()? != root {
        return Err(Error::Config(
            "workspace must be the repository root".into(),
        ));
    }
    let mut files = paths(git(root, &["ls-files", "-z"]).await?)?;
    files.extend(paths(
        git(root, &["ls-files", "--others", "--exclude-standard", "-z"]).await?,
    )?);
    let files: BTreeMap<_, _> = files
        .into_iter()
        .filter(|p| !internal(p))
        .map(|p| Ok((p.clone(), entry(root, &p)?)))
        .collect::<Result<_>>()?;
    let ignored: BTreeMap<_, _> = paths(
        git(
            root,
            &[
                "ls-files",
                "--others",
                "--ignored",
                "--exclude-standard",
                "-z",
            ],
        )
        .await?,
    )?
    .into_iter()
    .filter(|p| !internal(p))
    .map(|p| Ok((p.clone(), entry(root, &p)?)))
    .collect::<Result<_>>()?;
    let all = tree_without_git(root)?;
    let rules = protected_paths(configured, &files);
    let mut protected: BTreeMap<_, _> = all
        .into_iter()
        .filter(|(p, _)| {
            rules
                .iter()
                .any(|r| p == r || p.starts_with(&format!("{r}/")))
        })
        .collect();
    for rule in rules {
        if !protected.contains_key(&rule) {
            protected.insert(rule.clone(), entry(root, &rule)?);
        }
    }
    let mut git_metadata = BTreeMap::new();
    for flag in ["--git-dir", "--git-common-dir"] {
        let raw = git(root, &["rev-parse", "--path-format=absolute", flag]).await?;
        let directory = PathBuf::from(String::from_utf8_lossy(&raw).trim());
        git_metadata.insert(directory.to_string_lossy().into_owned(), tree(&directory)?);
    }
    let head = String::from_utf8_lossy(&git(root, &["rev-parse", "HEAD"]).await?)
        .trim()
        .into();
    let head_ref =
        String::from_utf8_lossy(&git(root, &["rev-parse", "--symbolic-full-name", "HEAD"]).await?)
            .trim()
            .into();
    let index = digest(&git(root, &["ls-files", "--stage", "-v", "-z"]).await?);
    let refs = String::from_utf8_lossy(
        &git(
            root,
            &["for-each-ref", "--format=%(refname)%00%(objectname)"],
        )
        .await?,
    )
    .into_owned();
    Ok(RepositorySnapshot {
        files,
        ignored,
        protected,
        git_metadata,
        head,
        head_ref,
        index,
        refs,
    })
}
fn tree_without_git(root: &Path) -> Result<BTreeMap<String, FileEntry>> {
    fn walk(root: &Path, dir: &Path, result: &mut BTreeMap<String, FileEntry>) -> Result<()> {
        for child in fs::read_dir(dir)? {
            let path = child?.path();
            let name = path
                .strip_prefix(root)
                .unwrap()
                .to_str()
                .ok_or_else(|| Error::Config("non-UTF8 path".into()))?
                .to_owned();
            if name == ".git" || internal(&name) {
                continue;
            }
            let e = entry(root, &name)?;
            let directory = e.kind == "directory";
            result.insert(name, e);
            if directory {
                walk(root, &path, result)?;
            }
        }
        Ok(())
    }
    let mut result = BTreeMap::new();
    walk(root, root, &mut result)?;
    Ok(result)
}
pub(crate) async fn changed_files(root: &Path) -> Result<Vec<String>> {
    let mut changed =
        paths(git(root, &["diff", "--no-renames", "--name-only", "-z", "HEAD"]).await?)?;
    changed.extend(paths(
        git(root, &["ls-files", "--others", "--exclude-standard", "-z"]).await?,
    )?);
    Ok(changed.into_iter().filter(|p| !internal(p)).collect())
}
pub(crate) async fn patches(root: &Path, files: &[String]) -> Result<BTreeMap<String, String>> {
    let mut result = BTreeMap::new();
    for file in files {
        let mut raw = git(
            root,
            &[
                "diff",
                "--binary",
                "--no-ext-diff",
                "--no-textconv",
                "--no-renames",
                "HEAD",
                "--",
                file,
            ],
        )
        .await?;
        if raw.is_empty() {
            let output = Command::new("git")
                .args([
                    "diff",
                    "--no-index",
                    "--binary",
                    "--no-ext-diff",
                    "--no-textconv",
                    "--",
                    "/dev/null",
                    file,
                ])
                .current_dir(root)
                .env("GIT_OPTIONAL_LOCKS", "0")
                .output()
                .await?;
            if output.status.code().is_none_or(|n| n > 1) {
                return Err(Error::Config("cannot capture patch".into()));
            }
            raw = output.stdout;
        }
        result.insert(file.clone(), String::from_utf8_lossy(&raw).into_owned());
    }
    Ok(result)
}
