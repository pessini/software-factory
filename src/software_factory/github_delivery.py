"""The external interface used by delivery and controlled integration tests."""

from __future__ import annotations

import json
from typing import Any, Protocol
from urllib.parse import quote, urlencode

from software_factory.delivery_commands import DeliveryCommands


class GitHubProtocol(Protocol):
    async def branch_head(self, repository: str, branch: str) -> str | None: ...
    async def push(
        self, repository: str, branch: str, commit: str, previous: str | None
    ) -> None: ...
    async def find_pull(self, repository: str, branch: str) -> dict[str, Any] | None: ...
    async def create_pull(
        self, repository: str, branch: str, base: str, title: str, body: str
    ) -> None: ...
    async def pull(self, repository: str, number: int) -> dict[str, Any]: ...
    async def checks(self, repository: str, commit: str) -> dict[str, Any]: ...
    async def review_threads(self, repository: str, number: int) -> list[dict[str, Any]]: ...
    async def reply(
        self, repository: str, number: int, thread: dict[str, Any], body: str
    ) -> None: ...
    async def resolve(self, thread_id: str) -> None: ...
    async def comments(self, repository: str, number: int) -> list[dict[str, Any]]: ...
    async def comment(self, repository: str, number: int, body: str) -> None: ...


class GitHubDelivery:
    def __init__(self, commands: DeliveryCommands) -> None:
        self.commands = commands

    async def api(
        self, path: str, payload: dict[str, Any] | None = None, *, pages: bool = False
    ) -> Any:
        argv = ["gh", "api", "--hostname", "github.com", path]
        if pages:
            argv.extend(["--paginate", "--slurp"])
        data = None
        if payload is not None:
            argv.extend(["--method", "POST", "--input", "-"])
            data = json.dumps(payload).encode()
        result = json.loads(await self.commands.run(argv, data))
        if isinstance(result, dict) and result.get("errors"):
            raise ValueError(f"GitHub rejected the operation: {result['errors']}")
        return result

    async def branch_head(self, repository: str, branch: str) -> str | None:
        # A successful full listing distinguishes an absent branch from an API failure.
        pages = await self.api(
            f"repos/{repository}/git/matching-refs/heads/{quote(branch, safe='')}", pages=True
        )
        return next(
            (
                item["object"]["sha"]
                for page in pages
                for item in page
                if item["ref"] == f"refs/heads/{branch}"
            ),
            None,
        )

    async def push(self, repository: str, branch: str, commit: str, previous: str | None) -> None:
        # An explicit lease prevents overwriting a branch changed since reconciliation.
        # Git's ancestry check also rules out a non-fast-forward update.
        if previous:
            await self.commands.git("merge-base", "--is-ancestor", previous, commit)
        await self.commands.git(
            "-c",
            "credential.helper=",
            "-c",
            "credential.helper=!gh auth git-credential",
            "push",
            "--porcelain",
            f"--force-with-lease=refs/heads/{branch}:{previous or ''}",
            f"https://github.com/{repository}.git",
            f"{commit}:refs/heads/{branch}",
        )

    @staticmethod
    def normalize_pull(raw: dict[str, Any]) -> dict[str, Any]:
        return {
            "number": raw["number"],
            "url": raw["html_url"],
            "branch": raw["head"]["ref"],
            "base": raw["base"]["ref"],
            "head": raw["head"]["sha"],
            "draft": raw["draft"],
            "state": raw["state"],
            "body": raw["body"] or "",
            "title": raw["title"],
        }

    async def find_pull(self, repository: str, branch: str) -> dict[str, Any] | None:
        query = urlencode(
            {"state": "all", "head": f"{repository.split('/')[0]}:{branch}", "per_page": 100}
        )
        pages = await self.api(f"repos/{repository}/pulls?{query}", pages=True)
        pulls = [
            p
            for page in pages
            for p in page
            if p["head"]["repo"]
            and p["head"]["repo"]["full_name"].casefold() == repository.casefold()
        ]
        if len(pulls) > 1:
            raise ValueError("multiple PRs match the saved delivery branch")
        return self.normalize_pull(pulls[0]) if pulls else None

    async def create_pull(
        self, repository: str, branch: str, base: str, title: str, body: str
    ) -> None:
        await self.api(
            f"repos/{repository}/pulls",
            {"head": branch, "base": base, "title": title, "body": body, "draft": False},
        )

    async def pull(self, repository: str, number: int) -> dict[str, Any]:
        return self.normalize_pull(await self.api(f"repos/{repository}/pulls/{number}"))

    async def checks(self, repository: str, commit: str) -> dict[str, Any]:
        check_pages = await self.api(
            f"repos/{repository}/commits/{commit}/check-runs?filter=latest&per_page=100", pages=True
        )
        status_pages = await self.api(
            f"repos/{repository}/commits/{commit}/statuses?per_page=100", pages=True
        )
        checks = []
        for page in check_pages:
            for item in page["check_runs"]:
                if item["head_sha"] != commit:
                    raise ValueError("GitHub returned checks for another commit")
                result = (
                    "pending"
                    if item["status"] != "completed"
                    else "passed"
                    if item["conclusion"] in {"success", "neutral", "skipped"}
                    else "failed"
                )
                checks.append({"name": item["name"], "status": result, "url": item["html_url"]})
        seen = set()
        for page in status_pages:
            for item in page:
                if item["context"] not in seen:
                    seen.add(item["context"])
                    checks.append(
                        {
                            "name": item["context"],
                            "status": {"success": "passed", "pending": "pending"}.get(
                                item["state"], "failed"
                            ),
                            "url": item["target_url"],
                        }
                    )
        states = {item["status"] for item in checks}
        result = (
            "failed"
            if "failed" in states
            else "pending"
            if "pending" in states
            else "passed"
            if checks
            else "unavailable"
        )
        return {"commit": commit, "status": result, "checks": checks}

    async def review_threads(self, repository: str, number: int) -> list[dict[str, Any]]:
        owner, name = repository.split("/")
        query = """query($owner:String!,$name:String!,$number:Int!,$cursor:String) {
          repository(owner:$owner,name:$name) { pullRequest(number:$number) {
            reviewThreads(first:100,after:$cursor) { pageInfo {hasNextPage endCursor}
              nodes {id isResolved isOutdated path line comments(first:100) {
                pageInfo {hasNextPage endCursor}
                nodes {id databaseId body author {login} originalCommit {oid}}
              }}
            }
          }}
        }"""
        cursor = None
        threads = []
        while True:
            raw = await self.api(
                "graphql",
                {
                    "query": query,
                    "variables": {"owner": owner, "name": name, "number": number, "cursor": cursor},
                },
            )
            connection = raw["data"]["repository"]["pullRequest"]["reviewThreads"]
            for node in connection["nodes"]:
                comments = node["comments"]["nodes"]
                page = node["comments"]["pageInfo"]
                while page["hasNextPage"]:
                    more = await self.api(
                        "graphql",
                        {
                            "query": """query($id:ID!,$cursor:String!) {
                      node(id:$id) {... on PullRequestReviewThread {comments(first:100,after:$cursor) {
                        pageInfo {hasNextPage endCursor} nodes {id databaseId body author {login} originalCommit {oid}}
                      }}}
                    }""",
                            "variables": {"id": node["id"], "cursor": page["endCursor"]},
                        },
                    )
                    data = more["data"]["node"]["comments"]
                    comments.extend(data["nodes"])
                    page = data["pageInfo"]
                if not comments:
                    raise ValueError("review thread has no finding")
                first = comments[0]
                threads.append(
                    {
                        "id": node["id"],
                        "resolved": node["isResolved"],
                        "outdated": node["isOutdated"],
                        "path": node["path"],
                        "line": node["line"],
                        "body": first["body"],
                        "comment_id": first["databaseId"],
                        "commit": (first["originalCommit"] or {}).get("oid"),
                        "comments": comments,
                    }
                )
            if not connection["pageInfo"]["hasNextPage"]:
                return threads
            cursor = connection["pageInfo"]["endCursor"]

    async def reply(self, repository: str, number: int, thread: dict[str, Any], body: str) -> None:
        await self.api(
            f"repos/{repository}/pulls/{number}/comments/{thread['comment_id']}/replies",
            {"body": body},
        )

    async def resolve(self, thread_id: str) -> None:
        await self.api(
            "graphql",
            {
                "query": "mutation($id:ID!) {resolveReviewThread(input:{threadId:$id}) {thread {id isResolved}}}",
                "variables": {"id": thread_id},
            },
        )

    async def comments(self, repository: str, number: int) -> list[dict[str, Any]]:
        pages = await self.api(
            f"repos/{repository}/issues/{number}/comments?per_page=100", pages=True
        )
        return [{"id": item["id"], "body": item["body"]} for page in pages for item in page]

    async def comment(self, repository: str, number: int, body: str) -> None:
        await self.api(f"repos/{repository}/issues/{number}/comments", {"body": body})
