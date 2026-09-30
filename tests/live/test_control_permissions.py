"""A real outer-assistant sandbox probe; no model call or credentials required."""

import json
import os
import subprocess
import sys

import pytest


@pytest.mark.skipif(
    os.environ.get("FACTORY_LIVE_SANDBOX") != "1", reason="requires the local macOS Codex sandbox"
)
def test_control_only_assistant_can_read_but_cannot_write_target(tmp_path):
    workspace = tmp_path / "target"
    workspace.mkdir()
    (workspace / "source.txt").write_text("original")
    script = """import json
from pathlib import Path
result={'read':Path('source.txt').read_text()}
try:
 Path('source.txt').write_text('changed')
 result['write']='allowed'
except PermissionError:
 result['write']='denied'
print(json.dumps(result))
"""
    result = subprocess.run(
        [
            "codex",
            "sandbox",
            "-C",
            str(workspace),
            "-P",
            "factory_control",
            "-c",
            'default_permissions="factory_control"',
            "-c",
            'permissions.factory_control.filesystem={":root"="read"}',
            "-c",
            "permissions.factory_control.network.enabled=false",
            "--",
            sys.executable,
            "-c",
            script,
        ],
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"read": "original", "write": "denied"}
    assert (workspace / "source.txt").read_text() == "original"
