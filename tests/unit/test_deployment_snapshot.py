"""The offline snapshot command preserves a whole WAL-backed deployment."""

import json
import sqlite3
import subprocess
import sys
from pathlib import Path


def test_snapshot_restores_wal_and_receipts_and_refuses_overwrite(tmp_path: Path) -> None:
    data = tmp_path / "data"
    workspace = tmp_path / "workspace"
    snapshot = tmp_path / "snapshot"
    data.mkdir()
    workspace.mkdir()
    (workspace / "greeting.txt").write_text("verified greeting\n")
    (data / "receipt.json").write_text('{"operation":"exact-saved-operation"}')
    docker = tmp_path / "docker"
    docker.write_text("#!/bin/sh\nexit 0\n")  # External daemon: no surviving workers.
    docker.chmod(0o755)
    connection = sqlite3.connect(data / "state.sqlite")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute("CREATE TABLE saved (value TEXT)")
    connection.execute("INSERT INTO saved VALUES ('pending-exact-revision')")
    connection.commit()
    script = Path(__file__).resolve().parents[2] / "scripts/factory-snapshot.py"

    def command(action: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(script),
                action,
                "--data",
                str(data),
                "--workspace",
                str(workspace),
                "--snapshot",
                str(snapshot),
                "--docker",
                str(docker),
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    backed_up = command("backup")
    assert backed_up.returncode == 0, backed_up.stderr
    connection.close()
    assert command("restore").returncode != 0  # Never overwrite existing state.
    data.rename(tmp_path / "original-data")
    workspace.rename(tmp_path / "original-workspace")
    restored = command("restore")
    assert restored.returncode == 0, restored.stderr
    with sqlite3.connect(data / "state.sqlite") as saved:
        assert saved.execute("SELECT value FROM saved").fetchone() == ("pending-exact-revision",)
    assert json.loads((data / "receipt.json").read_text())["operation"] == "exact-saved-operation"
    assert (workspace / "greeting.txt").read_text() == "verified greeting\n"
