import asyncio
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from software_factory.cli import app
from software_factory.persistence import RunStore


def test_graph_command_prints_topology() -> None:
    result = CliRunner().invoke(app, ["graph", "--format", "mermaid"])
    assert result.exit_code == 0
    assert "bootstrap --> baseline --> plan --> build --> test" in result.stdout


def test_help_exposes_operational_commands() -> None:
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("acp", "doctor", "run", "inspect", "resume", "cancel", "repo-check"):
        assert command in result.stdout


@pytest.mark.parametrize("status", ["ready_for_review", "failed", "blocked", "cancelled"])
def test_resume_exit_code_and_printed_result_match_the_stored_outcome(
    tmp_path: Path, status: str
) -> None:
    storage = tmp_path / "parent-storage"
    store = RunStore(storage / "factory" / "checkpoints.sqlite")

    async def save_outcome() -> None:
        await store.initialize()
        await store.create_run("fct_cli_outcome", tmp_path, "parent-1")
        await store.set_status("fct_cli_outcome", "running")
        if status == "cancelled":
            await store.request_stop("fct_cli_outcome", status, "cancelled by user")
        await store.set_status("fct_cli_outcome", status, result={"changed_files": ["answer.txt"]})

    asyncio.run(save_outcome())
    result = CliRunner().invoke(
        app,
        ["resume", "fct_cli_outcome", "--workspace", str(tmp_path)],
        env={
            "BB_THREAD_ID": "parent-1",
            "BB_PROJECT_ID": "project-1",
            "BB_ENVIRONMENT_ID": "environment-1",
            "BB_THREAD_STORAGE": str(storage),
        },
    )
    assert result.exit_code == (0 if status == "ready_for_review" else 1), result.output
    printed = json.loads(result.stdout)
    assert printed["status"] == status
    assert printed["changed_files"] == ["answer.txt"]
