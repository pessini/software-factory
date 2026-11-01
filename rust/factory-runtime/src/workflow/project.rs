use super::{QualityCommand, repository::RepositorySnapshot};
use crate::{Error, Result, runtime::digest};
use serde::{Deserialize, Serialize};
use std::{
    collections::BTreeSet,
    fs,
    path::{Path, PathBuf},
};
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct Instruction {
    pub source: PathBuf,
    pub scope: PathBuf,
    pub content: String,
    pub sha256: String,
}
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct Project {
    pub starting_commit: String,
    pub instructions: Vec<Instruction>,
    pub reviewed_commands: Vec<QualityCommand>,
}
impl Project {
    pub fn capture(
        root: &Path,
        snapshot: &RepositorySnapshot,
        commands: &[QualityCommand],
    ) -> Result<Self> {
        let mut sources = BTreeSet::new();
        for parent in root.ancestors() {
            for name in ["AGENTS.md", "CLAUDE.md"] {
                let path = parent.join(name);
                if path.exists() || path.is_symlink() {
                    sources.insert(path);
                }
            }
        }
        for path in snapshot.files.keys() {
            if matches!(
                Path::new(path).file_name().and_then(|s| s.to_str()),
                Some("AGENTS.md" | "CLAUDE.md")
            ) {
                sources.insert(root.join(path));
            }
        }
        let instructions = sources
            .into_iter()
            .map(|source| {
                if source.is_symlink() || !source.is_file() {
                    return Err(Error::Config(
                        "project instructions must be regular files".into(),
                    ));
                }
                let raw = fs::read(&source)?;
                let sha256 = digest(&raw);
                let content = String::from_utf8(raw)
                    .map_err(|_| Error::Config("project instructions must be UTF8".into()))?;
                Ok(Instruction {
                    scope: source.parent().unwrap().to_owned(),
                    source,
                    content,
                    sha256,
                })
            })
            .collect::<Result<_>>()?;
        Ok(Self {
            starting_commit: snapshot.head.clone(),
            instructions,
            reviewed_commands: commands.to_vec(),
        })
    }
    pub fn verify(&self) -> Result<()> {
        for instruction in &self.instructions {
            if instruction.source.is_symlink()
                || digest(&fs::read(&instruction.source)?) != instruction.sha256
            {
                return Err(Error::Integrity(
                    "project instruction source changed; workspace preserved".into(),
                ));
            }
        }
        Ok(())
    }
}
