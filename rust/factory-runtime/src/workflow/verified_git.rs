use super::{
    artifacts,
    delivery::check_cancel,
    repository::{self, FileEntry, RepositorySnapshot},
};
use crate::{Error, Result, Runtime, runtime::digest};
use serde::{Deserialize, Serialize};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    path::Path,
    process::Stdio,
};
use tokio::process::Command;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct CommitRecord {
    pub commit: String,
    pub tree: String,
    pub branch: String,
    pub snapshot: RepositorySnapshot,
    pub prepared: RepositorySnapshot,
    pub committed_index: String,
}
fn require(condition: bool, message: &str) -> Result<()> {
    if condition {
        Ok(())
    } else {
        Err(Error::Integrity(message.into()))
    }
}
pub(crate) struct VerifiedCommit;
pub(crate) async fn command(
    root: &Path,
    args: &[&str],
    input: Option<&[u8]>,
    index: Option<&Path>,
) -> Result<Vec<u8>> {
    let mut cmd = Command::new("git");
    cmd.args([
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.untrackedCache=false",
    ])
    .args(args)
    .current_dir(root)
    .env("GIT_OPTIONAL_LOCKS", "0")
    .stdin(Stdio::piped())
    .stdout(Stdio::piped())
    .stderr(Stdio::piped())
    .kill_on_drop(true);
    if let Some(index) = index {
        cmd.env("GIT_INDEX_FILE", index);
    }
    let output = super::process::output(cmd, input, std::time::Duration::from_secs(60)).await?;
    if !output.status.success() {
        return Err(Error::Worker(
            String::from_utf8_lossy(&output.stderr).trim().into(),
        ));
    }
    Ok(output.stdout)
}
fn string(raw: Vec<u8>) -> String {
    String::from_utf8_lossy(&raw).trim().into()
}
fn refs(snapshot: &RepositorySnapshot) -> BTreeMap<String, String> {
    snapshot
        .refs
        .lines()
        .filter_map(|line| line.split_once('\0'))
        .map(|(k, v)| (k.into(), v.into()))
        .collect()
}
fn present(files: &BTreeMap<String, FileEntry>) -> BTreeMap<String, FileEntry> {
    files
        .iter()
        .filter(|(_, e)| e.kind != "missing")
        .map(|(p, e)| (p.clone(), e.clone()))
        .collect()
}
fn metadata_compatible(
    before: &RepositorySnapshot,
    after: &RepositorySnapshot,
    allowed: &BTreeSet<String>,
) -> bool {
    if before.git_metadata.keys().collect::<Vec<_>>()
        != after.git_metadata.keys().collect::<Vec<_>>()
    {
        return false;
    }
    for (directory, old) in &before.git_metadata {
        let new = &after.git_metadata[directory];
        for path in old.keys().chain(new.keys()) {
            if old.get(path) == new.get(path) {
                continue;
            }
            if !allowed.contains(path) {
                return false;
            }
        }
    }
    true
}
fn reference_paths(branch: &str) -> BTreeSet<String> {
    let mut allowed: BTreeSet<String> = ["HEAD", "index", "logs/HEAD"].map(String::from).into();
    for prefix in ["refs/heads", "logs/refs/heads"] {
        let path = format!("{prefix}/{branch}");
        allowed.insert(path.clone());
        for ancestor in Path::new(&path).ancestors().skip(1) {
            if !ancestor.as_os_str().is_empty() {
                allowed.insert(ancestor.to_string_lossy().into());
            }
        }
    }
    allowed
}
pub(crate) fn same_tested_state(
    expected: &RepositorySnapshot,
    current: &RepositorySnapshot,
) -> bool {
    // Preparing Git objects is content-addressed and leaves refs/index/files
    // untouched. Unreferenced additions can survive a crash before the commit
    // record is saved; existing objects and all other metadata must still match.
    let mut allowed = BTreeSet::new();
    for (directory, entries) in &current.git_metadata {
        let Some(before) = expected.git_metadata.get(directory) else {
            return false;
        };
        allowed.extend(
            entries
                .keys()
                .filter(|path| path.starts_with("objects/") && !before.contains_key(*path))
                .cloned(),
        );
    }
    let mut comparable = expected.clone();
    comparable.git_metadata = current.git_metadata.clone();
    comparable == *current && metadata_compatible(expected, current, &allowed)
}
impl VerifiedCommit {
    pub async fn prepare(
        runtime: &Runtime,
        id: &str,
        snapshot: &RepositorySnapshot,
        title: &str,
        branch: &str,
    ) -> Result<CommitRecord> {
        check_cancel(runtime, id).await?;
        artifacts::verify(runtime, id).await?;
        let run = runtime.status(id).await?;
        let config = &run.input.implementation.as_ref().unwrap().config;
        let root = &runtime.config.workspace;
        let current = repository::snapshot(root, &config.safety.protected_paths).await?;
        require(
            same_tested_state(snapshot, &current),
            "stale tested bytes before commit preparation",
        )?;
        let directory = runtime.config.data_dir.join("delivery").join(id);
        fs::create_dir_all(&directory)?;
        let index = directory.join("index");
        if index.exists() {
            fs::remove_file(&index)?;
        }
        command(root, &["read-tree", &snapshot.head], None, Some(&index)).await?;
        let result=async {
            let mut objects=BTreeSet::new();
            for path in repository::changed_files(root).await? {
                let entry=snapshot.files.get(&path).ok_or_else(||Error::Config("changed file absent from tested snapshot".into()))?;
                if entry.kind=="missing"{command(root,&["update-index","--force-remove","--",&path],None,Some(&index)).await?;continue;}
                let (bytes,mode)=match entry.kind.as_str(){"file"=>(fs::read(root.join(&path))?,if entry.mode&0o111!=0{"100755"}else{"100644"}),"symlink"=>(fs::read_link(root.join(&path))?.as_os_str().as_encoded_bytes().to_vec(),"120000"),_=>return Err(Error::Config("delivery does not support directories/submodules".into()))};
                require(digest(&bytes)==entry.sha256,"source bytes changed while preparing commit")?;
                let blob=string(command(root,&["hash-object","-w","--no-filters","--stdin"],Some(&bytes),None).await?);objects.insert(blob.clone());
                command(root,&["update-index","--add","--cacheinfo",mode,&blob,&path],None,Some(&index)).await?;
            }
            let tree=string(command(root,&["write-tree"],None,Some(&index)).await?);objects.insert(tree.clone());
            let entries=command(root,&["ls-tree","-r","-t","-z",&tree],None,None).await?;
            for raw in entries.split(|b|*b==0).filter(|s|!s.is_empty()) {
                let entry=String::from_utf8_lossy(raw);let metadata=entry.split('\t').next().unwrap();if let Some(hash)=metadata.split_whitespace().nth(2){objects.insert(hash.into());}
            }
            verify_tree(root,&tree,snapshot).await?;
            let index_bytes=command(root,&["ls-files","--stage","-v","-z"],None,Some(&index)).await?;
            let author=string(command(root,&["var","GIT_AUTHOR_IDENT"],None,None).await?);let committer=string(command(root,&["var","GIT_COMMITTER_IDENT"],None,None).await?);
            let content=format!("tree {tree}\nparent {}\nauthor {author}\ncommitter {committer}\n\n{title}\n\nFactory delivery: {id}\n",snapshot.head);
            let commit=string(command(root,&["hash-object","-t","commit","-w","--stdin"],Some(content.as_bytes()),None).await?);objects.insert(commit.clone());
            let after=repository::snapshot(root,&config.safety.protected_paths).await?;
            let allowed=objects.iter().flat_map(|hash|[format!("objects/{}",&hash[..2]),format!("objects/{}/{}",&hash[..2],&hash[2..])]).collect();
            require(snapshot.files==after.files && snapshot.ignored==after.ignored && snapshot.protected==after.protected && snapshot.head==after.head && snapshot.head_ref==after.head_ref && snapshot.refs==after.refs && snapshot.index==after.index && metadata_compatible(&current,&after,&allowed),"unexpected repository change while preparing commit; user work preserved")?;
            Ok(CommitRecord {commit,tree,branch:branch.into(),snapshot:snapshot.clone(),prepared:after,committed_index:digest(&index_bytes)})
        }.await;
        fs::remove_file(index)?;
        result
    }
    pub async fn install(runtime: &Runtime, id: &str, record: &CommitRecord) -> Result<()> {
        artifacts::verify(runtime, id).await?;
        let run = runtime.status(id).await?;
        let config = &run.input.implementation.as_ref().unwrap().config;
        let current =
            repository::snapshot(&runtime.config.workspace, &config.safety.protected_paths).await?;
        require(
            present(&current.files) == present(&record.snapshot.files)
                && current.ignored == record.snapshot.ignored
                && current.protected == record.snapshot.protected,
            "files changed before verified commit installation",
        )?;
        let own = format!("refs/heads/{}", record.branch);
        let mut actual = refs(&current);
        let own_current = actual.remove(&own);
        let mut expected = refs(&record.snapshot);
        let own_before = expected.remove(&own);
        require(
            actual == expected
                && (own_current == own_before || own_current.as_deref() == Some(&record.commit)),
            "unexpected refs; refusing to overwrite user work",
        )?;
        require(
            (current.head == record.snapshot.head && current.head_ref == record.snapshot.head_ref)
                || (current.head == record.commit && current.head_ref == own),
            "checked out branch changed",
        )?;
        require(
            current.index == record.snapshot.index || current.index == record.committed_index,
            "index changed; staged user work preserved",
        )?;
        require(
            metadata_compatible(&record.prepared, &current, &reference_paths(&record.branch)),
            "protected repository metadata changed before installation",
        )?;
        verify_tree(&runtime.config.workspace, &record.tree, &record.snapshot).await?;
        if own_current.as_deref() != Some(&record.commit)
            || current.head_ref != own
            || current.index != record.committed_index
        {
            check_cancel(runtime, id).await?;
        }
        if own_current.as_deref() != Some(&record.commit) {
            command(
                &runtime.config.workspace,
                &[
                    "-c",
                    "core.logAllRefUpdates=false",
                    "update-ref",
                    &own,
                    &record.commit,
                    own_before.as_deref().unwrap_or(""),
                ],
                None,
                None,
            )
            .await?;
        }
        if current.head_ref != own {
            command(
                &runtime.config.workspace,
                &["symbolic-ref", "HEAD", &own],
                None,
                None,
            )
            .await?;
        }
        if current.index != record.committed_index {
            command(
                &runtime.config.workspace,
                &["read-tree", &record.commit],
                None,
                None,
            )
            .await?;
        }
        Self::require_installed(runtime, id, record).await
    }
    pub async fn require_installed(
        runtime: &Runtime,
        id: &str,
        record: &CommitRecord,
    ) -> Result<()> {
        artifacts::verify(runtime, id).await?;
        let run = runtime.status(id).await?;
        let current = repository::snapshot(
            &runtime.config.workspace,
            &run.input
                .implementation
                .as_ref()
                .unwrap()
                .config
                .safety
                .protected_paths,
        )
        .await?;
        let own = format!("refs/heads/{}", record.branch);
        let mut expected = refs(&record.snapshot);
        expected.insert(own.clone(), record.commit.clone());
        require(
            current.head == record.commit
                && current.head_ref == own
                && refs(&current) == expected
                && current.index == record.committed_index
                && present(&current.files) == present(&record.snapshot.files)
                && current.ignored == record.snapshot.ignored
                && current.protected == record.snapshot.protected
                && metadata_compatible(
                    &record.prepared,
                    &current,
                    &reference_paths(&record.branch),
                ),
            "repository no longer matches the installed verified commit",
        )
    }
}
async fn verify_tree(root: &Path, tree: &str, snapshot: &RepositorySnapshot) -> Result<()> {
    let entries = command(root, &["ls-tree", "-r", "-z", tree], None, None).await?;
    let mut actual = BTreeMap::new();
    for raw in entries.split(|b| *b == 0).filter(|s| !s.is_empty()) {
        let text = String::from_utf8(raw.to_vec())
            .map_err(|_| Error::Config("non-UTF8 tree path".into()))?;
        let (metadata, name) = text
            .split_once('\t')
            .ok_or_else(|| Error::Config("invalid Git tree".into()))?;
        let parts: Vec<_> = metadata.split_whitespace().collect();
        require(
            parts.len() == 3 && parts[1] == "blob",
            "delivery does not support submodules",
        )?;
        let bytes = command(root, &["cat-file", "blob", parts[2]], None, None).await?;
        actual.insert(name.to_owned(), (parts[0].to_owned(), digest(&bytes)));
    }
    let mut expected = BTreeMap::new();
    for (path, entry) in &snapshot.files {
        let mode = match entry.kind.as_str() {
            "missing" => continue,
            "file" => {
                if entry.mode & 0o111 != 0 {
                    "100755"
                } else {
                    "100644"
                }
            }
            "symlink" => "120000",
            _ => return Err(Error::Config("unsupported tested tree entry".into())),
        };
        expected.insert(path.clone(), (mode.into(), entry.sha256.clone()));
    }
    require(
        actual == expected,
        "commit tree differs from tested bytes; publication stopped",
    )
}
