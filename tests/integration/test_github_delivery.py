from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from software_factory.delivery_commands import DeliveryCommands
from software_factory.github_delivery import GitHubDelivery
from software_factory.persistence import RunStore


async def github_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, responses: list
) -> GitHubDelivery:
    """Control the external gh process while exercising the real adapter and runner."""
    (tmp_path / "responses.json").write_text(json.dumps(responses))
    executable = tmp_path / "gh"
    executable.write_text(
        f"#!{sys.executable}\n"
        + """import json,sys
from pathlib import Path
path=Path('responses.json')
responses=json.loads(path.read_text())
response=responses.pop(0)
path.write_text(json.dumps(responses))
with Path('requests.jsonl').open('a') as log:
    log.write(json.dumps({'argv':sys.argv[1:],'body':sys.stdin.read() if '--input' in sys.argv else None})+'\\n')
print(json.dumps(response))
"""
    )
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    store = RunStore(tmp_path / "runs.sqlite")
    await store.initialize()
    await store.create_run("fct_adapter", tmp_path, "parent")
    return GitHubDelivery(DeliveryCommands(tmp_path, store, "fct_adapter"))


@pytest.mark.asyncio
async def test_github_ci_includes_later_pages_and_latest_legacy_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = await github_cli(
        tmp_path,
        monkeypatch,
        [
            [
                {
                    "check_runs": [
                        {
                            "name": "lint",
                            "head_sha": "a" * 40,
                            "status": "completed",
                            "conclusion": "success",
                            "html_url": "https://example.invalid/lint",
                        }
                    ]
                },
                {
                    "check_runs": [
                        {
                            "name": "tests",
                            "head_sha": "a" * 40,
                            "status": "completed",
                            "conclusion": "failure",
                            "html_url": "https://example.invalid/tests",
                        }
                    ]
                },
            ],
            [
                [
                    {"context": "deploy-preview", "state": "success", "target_url": None},
                    {"context": "deploy-preview", "state": "failure", "target_url": None},
                ]
            ],
        ],
    )
    result = await adapter.checks("example/project", "a" * 40)
    assert result["status"] == "failed"
    assert result["checks"][-1]["status"] == "passed"
    assert len(result["checks"]) == 3


@pytest.mark.asyncio
async def test_github_creates_regular_pr_with_literal_multiline_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = await github_cli(tmp_path, monkeypatch, [{"number": 42}])
    body = "Verified changes.\n\nCloses issue #7\n\n`$(do not execute)`"
    await adapter.create_pull("example/project", "feat/7-answer", "main", "Add answer", body)
    request = json.loads((tmp_path / "requests.jsonl").read_text())
    sent = json.loads(request["body"])
    assert sent == {
        "head": "feat/7-answer",
        "base": "main",
        "title": "Add answer",
        "body": body,
        "draft": False,
    }


@pytest.mark.asyncio
async def test_github_review_threads_include_paginated_replies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    finding = {
        "id": "comment-1",
        "databaseId": 1,
        "body": "Handle empty input",
        "author": {"login": "reviewer"},
        "originalCommit": {"oid": "a" * 40},
    }
    reply = {
        "id": "comment-2",
        "databaseId": 2,
        "body": "Fixed with evidence",
        "author": {"login": "author"},
        "originalCommit": {"oid": "b" * 40},
    }
    adapter = await github_cli(
        tmp_path,
        monkeypatch,
        [
            {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviewThreads": {
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                                "nodes": [
                                    {
                                        "id": "thread-1",
                                        "isResolved": False,
                                        "isOutdated": False,
                                        "path": "answer.txt",
                                        "line": 1,
                                        "comments": {
                                            "pageInfo": {"hasNextPage": True, "endCursor": "next"},
                                            "nodes": [finding],
                                        },
                                    }
                                ],
                            }
                        }
                    }
                }
            },
            {
                "data": {
                    "node": {
                        "comments": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [reply],
                        }
                    }
                }
            },
        ],
    )
    threads = await adapter.review_threads("example/project", 42)
    assert threads[0]["comments"] == [finding, reply]
    assert threads[0]["commit"] == "a" * 40
