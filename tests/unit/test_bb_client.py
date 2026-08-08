from __future__ import annotations

import json
from pathlib import Path

import pytest

from software_factory.bb import BBClient, BBContext
from software_factory.config import AgentConfig


@pytest.mark.asyncio
async def test_spawn_uses_current_json_cli_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = tmp_path / "argv.json"
    executable = tmp_path / "fake-bb"
    executable.write_text(
        """#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
Path(os.environ["FAKE_BB_LOG"]).write_text(json.dumps(sys.argv[1:]))
print(json.dumps({"thread": {"id": "child-123"}}))
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    monkeypatch.setenv("FAKE_BB_LOG", str(log))
    context = BBContext("parent-1", "project-1", "environment-1", tmp_path / "storage")
    client = BBClient(context, tmp_path, (str(executable),))
    agent = AgentConfig(provider="codex", reasoning_level="high", permission_mode="full")
    thread_id = await client.spawn_child("builder", "Implement it", agent, "fct_test:builder")
    argv = json.loads(log.read_text(encoding="utf-8"))
    assert thread_id == "child-123"
    assert argv[:2] == ["thread", "spawn"]
    assert argv[-1] == "--json"
    assert argv[argv.index("--parent-thread") + 1] == "parent-1"
    assert argv[argv.index("--environment") + 1] == "environment-1"
    assert argv[argv.index("--permission-mode") + 1] == "full"


@pytest.mark.asyncio
async def test_find_child_reconciles_exact_spawn_title(tmp_path: Path) -> None:
    executable = tmp_path / "fake-bb"
    executable.write_text(
        """#!/usr/bin/env python3
import json
print(json.dumps({"threads": [
    {"id": "wrong", "title": "Planner · Software Factory · another:planner"},
    {"id": "child-123", "title": "Planner · Software Factory · fct_test:planner"}
]}))
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    context = BBContext("parent-1", "project-1", "environment-1", tmp_path / "storage")
    client = BBClient(context, tmp_path, (str(executable),))

    thread_id = await client.find_child("planner", "fct_test:planner")

    assert thread_id == "child-123"
