use super::{
    delivery::{Checks, Pull, Remote, ReviewThread},
    verified_git,
};
use crate::{Error, Result};
use async_trait::async_trait;
use serde_json::{Value, json};
use std::{collections::BTreeSet, path::PathBuf, process::Stdio};
use tokio::process::Command;

pub struct GitHub {
    pub workspace: PathBuf,
}
fn encode(value: &str) -> String {
    value
        .bytes()
        .map(|b| {
            if b.is_ascii_alphanumeric() || b"-_.~".contains(&b) {
                (b as char).to_string()
            } else {
                format!("%{b:02X}")
            }
        })
        .collect()
}
impl GitHub {
    async fn api(&self, path: &str, payload: Option<Value>, pages: bool) -> Result<Value> {
        let mut command = Command::new("gh");
        command
            .args(["api", "--hostname", "github.com", path])
            .current_dir(&self.workspace)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .kill_on_drop(true);
        if pages {
            command.args(["--paginate", "--slurp"]);
        }
        if payload.is_some() {
            command.args(["--method", "POST", "--input", "-"]);
        }
        let input = payload.map(|p| serde_json::to_vec(&p)).transpose()?;
        let output = super::process::output(
            command,
            input.as_deref(),
            std::time::Duration::from_secs(60),
        )
        .await?;
        if !output.status.success() {
            return Err(Error::Worker(
                String::from_utf8_lossy(&output.stderr).trim().into(),
            ));
        }
        let value: Value = serde_json::from_slice(&output.stdout)?;
        if value.get("errors").is_some() {
            return Err(Error::Worker(format!(
                "GitHub rejected operation: {}",
                value["errors"]
            )));
        }
        Ok(value)
    }
    fn pull_value(raw: &Value) -> Result<Pull> {
        let text = |path: &[&str]| -> Result<String> {
            let value = path.iter().fold(raw, |v, k| &v[*k]);
            value
                .as_str()
                .map(String::from)
                .ok_or_else(|| Error::Worker("GitHub returned an invalid PR".into()))
        };
        Ok(Pull {
            number: raw["number"]
                .as_u64()
                .ok_or_else(|| Error::Worker("PR number missing".into()))?,
            url: text(&["html_url"])?,
            branch: text(&["head", "ref"])?,
            base: text(&["base", "ref"])?,
            head: text(&["head", "sha"])?,
            draft: raw["draft"]
                .as_bool()
                .ok_or_else(|| Error::Worker("PR draft state missing".into()))?,
            state: text(&["state"])?,
            body: raw["body"].as_str().unwrap_or("").into(),
            title: text(&["title"])?,
        })
    }
}
#[async_trait]
impl Remote for GitHub {
    async fn branch_head(&self, repository: &str, branch: &str) -> Result<Option<String>> {
        let pages = self
            .api(
                &format!(
                    "repos/{repository}/git/matching-refs/heads/{}",
                    encode(branch)
                ),
                None,
                true,
            )
            .await?;
        for page in pages
            .as_array()
            .ok_or_else(|| Error::Worker("branch listing is invalid".into()))?
        {
            for value in page
                .as_array()
                .ok_or_else(|| Error::Worker("branch page is invalid".into()))?
            {
                if value["ref"] == format!("refs/heads/{branch}") {
                    return value["object"]["sha"]
                        .as_str()
                        .map(|s| Some(s.into()))
                        .ok_or_else(|| Error::Worker("branch has no commit".into()));
                }
            }
        }
        Ok(None)
    }
    async fn push(
        &self,
        repository: &str,
        branch: &str,
        commit: &str,
        previous: Option<&str>,
    ) -> Result<()> {
        if let Some(previous) = previous {
            verified_git::command(
                &self.workspace,
                &["merge-base", "--is-ancestor", previous, commit],
                None,
                None,
            )
            .await?;
        }
        verified_git::command(
            &self.workspace,
            &[
                "-c",
                "credential.helper=",
                "-c",
                "credential.helper=!gh auth git-credential",
                "push",
                "--porcelain",
                &format!(
                    "--force-with-lease=refs/heads/{branch}:{}",
                    previous.unwrap_or("")
                ),
                &format!("https://github.com/{repository}.git"),
                &format!("{commit}:refs/heads/{branch}"),
            ],
            None,
            None,
        )
        .await?;
        Ok(())
    }
    async fn find_pull(&self, repository: &str, branch: &str) -> Result<Option<Pull>> {
        let owner = repository.split('/').next().unwrap_or("");
        let pages = self
            .api(
                &format!(
                    "repos/{repository}/pulls?state=all&head={}&per_page=100",
                    encode(&format!("{owner}:{branch}"))
                ),
                None,
                true,
            )
            .await?;
        let mut found = None;
        for page in pages
            .as_array()
            .ok_or_else(|| Error::Worker("PR listing invalid".into()))?
        {
            for pull in page
                .as_array()
                .ok_or_else(|| Error::Worker("PR page invalid".into()))?
            {
                if pull["head"]["repo"]["full_name"]
                    .as_str()
                    .is_some_and(|s| s.eq_ignore_ascii_case(repository))
                {
                    if found.is_some() {
                        return Err(Error::Conflict(
                            "multiple PRs match the saved branch".into(),
                        ));
                    }
                    found = Some(Self::pull_value(pull)?);
                }
            }
        }
        Ok(found)
    }
    async fn create_pull(
        &self,
        repository: &str,
        branch: &str,
        base: &str,
        title: &str,
        body: &str,
    ) -> Result<()> {
        self.api(
            &format!("repos/{repository}/pulls"),
            Some(json!({"head":branch,"base":base,"title":title,"body":body,"draft":false})),
            false,
        )
        .await?;
        Ok(())
    }
    async fn pull(&self, repository: &str, number: u64) -> Result<Pull> {
        Self::pull_value(
            &self
                .api(&format!("repos/{repository}/pulls/{number}"), None, false)
                .await?,
        )
    }
    async fn checks(&self, repository: &str, commit: &str) -> Result<Checks> {
        let pages = self
            .api(
                &format!(
                    "repos/{repository}/commits/{commit}/check-runs?filter=latest&per_page=100"
                ),
                None,
                true,
            )
            .await?;
        let mut checks = vec![];
        for page in pages
            .as_array()
            .ok_or_else(|| Error::Worker("check pages invalid".into()))?
        {
            for check in page["check_runs"]
                .as_array()
                .ok_or_else(|| Error::Worker("check page invalid".into()))?
            {
                if check["head_sha"] != commit {
                    return Err(Error::Conflict("check belongs to another commit".into()));
                }
                let status = if check["status"] != "completed" {
                    "pending"
                } else if ["success", "neutral", "skipped"]
                    .iter()
                    .any(|s| check["conclusion"] == *s)
                {
                    "passed"
                } else {
                    "failed"
                };
                checks.push(json!({"name":check["name"],"status":status,"url":check["html_url"]}));
            }
        }
        let pages = self
            .api(
                &format!("repos/{repository}/commits/{commit}/statuses?per_page=100"),
                None,
                true,
            )
            .await?;
        let mut seen = BTreeSet::new();
        for page in pages
            .as_array()
            .ok_or_else(|| Error::Worker("status pages invalid".into()))?
        {
            for status in page
                .as_array()
                .ok_or_else(|| Error::Worker("status page invalid".into()))?
            {
                let name = status["context"]
                    .as_str()
                    .ok_or_else(|| Error::Worker("status has no context".into()))?;
                if seen.insert(name.to_owned()) {
                    checks.push(json!({"name":name,"status":match status["state"].as_str(){Some("success")=>"passed",Some("pending")=>"pending",_=>"failed"},"url":status["target_url"]}));
                }
            }
        }
        let status = if checks.iter().any(|c| c["status"] == "failed") {
            "failed"
        } else if checks.iter().any(|c| c["status"] == "pending") {
            "pending"
        } else if checks.is_empty() {
            "unavailable"
        } else {
            "passed"
        };
        Ok(Checks {
            commit: commit.into(),
            status: status.into(),
            checks,
        })
    }
    async fn review_threads(&self, repository: &str, number: u64) -> Result<Vec<ReviewThread>> {
        let (owner, name) = repository
            .split_once('/')
            .ok_or_else(|| Error::Config("invalid repository".into()))?;
        let query = "query($owner:String!,$name:String!,$number:Int!,$cursor:String){repository(owner:$owner,name:$name){pullRequest(number:$number){reviewThreads(first:100,after:$cursor){pageInfo{hasNextPage endCursor}nodes{id isResolved isOutdated path line comments(first:100){pageInfo{hasNextPage endCursor}nodes{id databaseId body author{login} originalCommit{oid}}}}}}}}";
        let mut cursor = Value::Null;
        let mut threads = vec![];
        loop {
            let response=self.api("graphql",Some(json!({"query":query,"variables":{"owner":owner,"name":name,"number":number,"cursor":cursor}})),false).await?;
            let connection = &response["data"]["repository"]["pullRequest"]["reviewThreads"];
            for node in connection["nodes"]
                .as_array()
                .ok_or_else(|| Error::Worker("review thread listing invalid".into()))?
            {
                let mut comments = node["comments"]["nodes"]
                    .as_array()
                    .ok_or_else(|| Error::Worker("review comments invalid".into()))?
                    .clone();
                let mut page = node["comments"]["pageInfo"].clone();
                while page["hasNextPage"] == true {
                    let more=self.api("graphql",Some(json!({"query":"query($id:ID!,$cursor:String!){node(id:$id){... on PullRequestReviewThread{comments(first:100,after:$cursor){pageInfo{hasNextPage endCursor}nodes{id databaseId body author{login} originalCommit{oid}}}}}}","variables":{"id":node["id"],"cursor":page["endCursor"]}})),false).await?;
                    let data = &more["data"]["node"]["comments"];
                    comments.extend(
                        data["nodes"]
                            .as_array()
                            .ok_or_else(|| Error::Worker("comment page invalid".into()))?
                            .clone(),
                    );
                    page = data["pageInfo"].clone();
                }
                let first = comments
                    .first()
                    .ok_or_else(|| Error::Worker("review thread is empty".into()))?;
                threads.push(ReviewThread {
                    id: node["id"]
                        .as_str()
                        .ok_or_else(|| Error::Worker("thread ID missing".into()))?
                        .into(),
                    resolved: node["isResolved"] == true,
                    outdated: node["isOutdated"] == true,
                    path: node["path"].as_str().unwrap_or("").into(),
                    line: node["line"].as_u64().and_then(|n| u32::try_from(n).ok()),
                    body: first["body"].as_str().unwrap_or("").into(),
                    comment_id: first["databaseId"]
                        .as_u64()
                        .ok_or_else(|| Error::Worker("thread comment ID missing".into()))?,
                    commit: first["originalCommit"]["oid"].as_str().map(String::from),
                    comments,
                });
            }
            if connection["pageInfo"]["hasNextPage"] != true {
                break;
            }
            cursor = connection["pageInfo"]["endCursor"].clone();
        }
        Ok(threads)
    }
    async fn reply(
        &self,
        repository: &str,
        number: u64,
        thread: &ReviewThread,
        body: &str,
    ) -> Result<()> {
        self.api(
            &format!(
                "repos/{repository}/pulls/{number}/comments/{}/replies",
                thread.comment_id
            ),
            Some(json!({"body":body})),
            false,
        )
        .await?;
        Ok(())
    }
    async fn resolve(&self, id: &str) -> Result<()> {
        self.api("graphql",Some(json!({"query":"mutation($id:ID!){resolveReviewThread(input:{threadId:$id}){thread{id isResolved}}}","variables":{"id":id}})),false).await?;
        Ok(())
    }
    async fn comments(&self, repository: &str, number: u64) -> Result<Vec<Value>> {
        let pages = self
            .api(
                &format!("repos/{repository}/issues/{number}/comments?per_page=100"),
                None,
                true,
            )
            .await?;
        let mut comments = vec![];
        for page in pages
            .as_array()
            .ok_or_else(|| Error::Worker("comment pages invalid".into()))?
        {
            comments.extend(
                page.as_array()
                    .ok_or_else(|| Error::Worker("comment page invalid".into()))?
                    .clone(),
            );
        }
        Ok(comments)
    }
    async fn comment(&self, repository: &str, number: u64, body: &str) -> Result<()> {
        self.api(
            &format!("repos/{repository}/issues/{number}/comments"),
            Some(json!({"body":body})),
            false,
        )
        .await?;
        Ok(())
    }
}
