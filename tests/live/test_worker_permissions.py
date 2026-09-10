"""Opt-in local isolation checks. No login, model call, or external data submission."""

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from software_factory.worker_container import (
    ContainerProvider,
    container_command,
    inspect_container,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("FACTORY_LIVE_CONTAINER") != "1",
    reason="requires the local worker image and Apple Container",
)


@pytest.mark.parametrize("read_only", [False, True])
async def test_real_container_enforces_role_and_evidence_permissions(
    tmp_path: Path, read_only: bool
) -> None:
    workspace = tmp_path / "workspace"
    evidence = tmp_path / "evidence"
    for directory in [workspace / ".factory", workspace / ".git", evidence]:
        directory.mkdir(parents=True)
    (workspace / ".env").write_text("dummy secret")
    (evidence / "result.json").write_text("original evidence")
    operation = "permission-proof-" + uuid4().hex
    provider = ContainerProvider(
        "factory-" + operation,
        operation,
        workspace,
        evidence,
        tmp_path / "provider",
        [workspace / ".factory", workspace / ".git", workspace / ".env", workspace / ".bb"],
        read_only=read_only,
        authenticate=False,
    )
    script = (
        """import json, os
from pathlib import Path
out={}
for target in ['answer.txt','.factory/settings.json','.git/index','.env', """
        + repr(str(evidence / "result.json"))
        + """]:
 try:
  Path(target).write_text('probe')
  out[target]='wrote'
 except OSError:
  out[target]='denied'
for action in ['unlink', 'rename', 'chmod', 'hardlink', 'symlink-write']:
 try:
  if action == 'unlink': Path('.env').unlink()
  elif action == 'rename': Path('.env').rename('moved-secret')
  elif action == 'chmod': Path('.env').chmod(0o777)
  elif action == 'hardlink': os.link('.env','hardlink-secret')
  else:
   os.symlink('.env','symlink-secret')
   Path('symlink-secret').write_text('changed')
  out[action]='wrote'
 except OSError:
  out[action]='denied'
print(json.dumps(out))
"""
    )
    try:
        await provider.start()
        assert not (workspace / ".bb").exists()
        result = await provider.request(
            "command/exec",
            {
                "command": ["python3", "-c", script],
                "cwd": str(workspace),
                "permissionProfile": "factory",
                "timeoutMs": 10000,
            },
        )
        assert result["exitCode"] == 0, result
        outcomes = json.loads(result["stdout"])
        assert outcomes.pop("answer.txt") == ("denied" if read_only else "wrote")
        assert set(outcomes.values()) == {"denied"}
        assert (evidence / "result.json").read_text() == "original evidence"
        assert (workspace / ".env").read_text() == "dummy secret"
        assert not (workspace / ".bb").exists()
    finally:
        await provider.close()
    assert (await inspect_container(provider.name, operation))["status"]["state"] == "stopped"
    await container_command("delete", provider.name)
