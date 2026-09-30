from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from software_factory.bb import BBClient, BBContext
from software_factory.bb.client import BBError, RecoveryNeeded
from software_factory.config import AgentConfig
from software_factory.persistence import RunStore


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
    thread_id = await client.start_worker("builder", "Implement it", agent, "fct_test:builder")
    argv = json.loads(log.read_text(encoding="utf-8"))
    assert thread_id == "child-123"
    assert argv[:2] == ["thread", "spawn"]
    assert argv[-1] == "--json"
    assert argv[argv.index("--parent-thread") + 1] == "parent-1"
    assert argv[argv.index("--environment") + 1] == "environment-1"
    assert argv[argv.index("--permission-mode") + 1] == "full"


async def test_bb_command_timeout_stops_a_group_that_ignores_term(tmp_path: Path) -> None:
    marker = tmp_path / "group.json"
    executable = tmp_path / "slow-bb"
    executable.write_text(f"""#!/usr/bin/env python3
import json, os, signal, subprocess, sys, time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
child = subprocess.Popen([sys.executable, '-c', 'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)'])
Path({str(marker)!r}).write_text(json.dumps({{'group': os.getpgrp(), 'child': child.pid}}))
time.sleep(30)
""")
    executable.chmod(0o755)
    store = RunStore(tmp_path / "factory.sqlite")
    await store.initialize()
    await store.create_run("slow-command", tmp_path, "parent")
    client = BBClient(
        BBContext("parent", "project", "environment", tmp_path / "storage"),
        tmp_path,
        (sys.executable, str(executable)),
        command_timeout_seconds=1,
        store=store,
        run_id="slow-command",
    )
    with pytest.raises(BBError) as caught:
        await client.output("child")
    assert caught.value.kind == "time_limit"
    group = json.loads(marker.read_text())["group"]
    with pytest.raises(ProcessLookupError):
        os.killpg(group, 0)
    assert await store.processes("slow-command") == []


async def test_live_adapter_rejects_output_without_an_execution_identity(tmp_path: Path) -> None:
    client = BBClient(BBContext("parent", "project", "environment", tmp_path / "storage"), tmp_path)
    assert await client.reconcile_operation("child", "saved-operation") is None
    with pytest.raises(RecoveryNeeded, match="no execution identity"):
        await client.execution("child", "expected-execution")


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

    thread_id = await client.find_worker("planner", "fct_test:planner")

    assert thread_id == "child-123"


async def test_model_discovery_uses_environment_and_model_specific_efforts(tmp_path: Path) -> None:
    executable = tmp_path / "catalog-bb"
    log = tmp_path / "catalog-args.jsonl"
    executable.write_text(f"""#!/usr/bin/env python3
import json, sys
from pathlib import Path
with Path({str(log)!r}).open('a') as out: out.write(json.dumps(sys.argv[1:]) + '\\n')
if sys.argv[1:3] == ['provider', 'list']:
    print(json.dumps([{{'id':'provider-a','available':True,'capabilities':{{'permissionModes':['auto','accept-edits']}}}}, {{'id':'acp-software-factory','available':True}}]))
else:
    print(json.dumps([{{'id':'catalog-id','model':'dispatch-id','displayName':'Model','supportedReasoningEfforts':[{{'reasoningEffort':'minimal'}}],'defaultReasoningEffort':'minimal','isDefault':True}}]))
""")
    executable.chmod(0o755)
    client = BBClient(
        BBContext("parent", "project", "target-env", tmp_path / "storage"),
        tmp_path,
        (str(executable),),
    )
    catalog = await client.model_catalog()
    assert catalog[0].model == "dispatch-id"
    assert catalog[0].efforts == ["minimal"]
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert calls == [
        ["provider", "list", "--environment", "target-env", "--json"],
        ["provider", "models", "provider-a", "--environment", "target-env", "--json"],
    ]
