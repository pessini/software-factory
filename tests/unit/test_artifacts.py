import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from software_factory.artifacts import ArtifactIntegrityError, ArtifactStore


def artifact_store(tmp_path: Path, *, create: bool = True) -> ArtifactStore:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return ArtifactStore(
        workspace, "fct_evidence", storage_root=tmp_path / "controller", create=create
    )


def test_evidence_versions_and_hashes_survive_reopen(tmp_path: Path) -> None:
    store = artifact_store(tmp_path)
    store.write_text("plan.md", "hello\n")
    store.write_text("plan.md", "hello\n")
    reopened = artifact_store(tmp_path, create=False)
    assert reopened.read_text("plan.md") == "hello\n"
    manifest = json.loads((store.root / "manifest.json").read_text())
    assert manifest["version"] == 2
    assert manifest["files"]["plan.md"] == {
        "revision": 2,
        "sha256": "5891b5b522d5df086d0ff0b110fbd9d21bb4fc7163af34d08286a2e846f6be03",
    }


@pytest.mark.parametrize("change", ["replace", "delete", "corrupt", "symlink", "manifest"])
def test_evidence_damage_is_rejected_before_read_or_write(tmp_path: Path, change: str) -> None:
    store = artifact_store(tmp_path)
    plan = store.write_text("plan.md", "original")
    if change == "replace":
        replacement = tmp_path / "replacement"
        replacement.write_text("forged")
        replacement.replace(plan)
    elif change == "delete":
        plan.unlink()
    elif change == "corrupt":
        plan.write_bytes(b"\xff")
    elif change == "symlink":
        plan.unlink()
        outside = tmp_path / "outside"
        outside.write_text("original")
        plan.symlink_to(outside)
    else:
        (store.root / "manifest.json").write_text("{}")
    with pytest.raises(ArtifactIntegrityError):
        store.read_text("plan.md")
    with pytest.raises(ArtifactIntegrityError):
        store.write_text("final-report.md", "ACCEPTED")
    with pytest.raises(ArtifactIntegrityError):
        artifact_store(tmp_path, create=False)


def test_evidence_in_workspace_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ArtifactIntegrityError, match="outside the workspace"):
        ArtifactStore(tmp_path, "fct_inside", storage_root=tmp_path / "evidence")


def test_old_runs_are_rejected_without_modification(tmp_path: Path) -> None:
    legacy = tmp_path / "workspace" / ".factory" / "runs" / "fct_evidence"
    legacy.mkdir(parents=True)
    (legacy / "plan.md").write_text("legacy plan")
    with pytest.raises(ArtifactIntegrityError, match="old runs cannot resume"):
        artifact_store(tmp_path, create=False)
    assert (legacy / "plan.md").read_text() == "legacy plan"
    assert not (tmp_path / "controller").exists()


def test_removed_manifest_does_not_reinitialize_evidence(tmp_path: Path) -> None:
    store = artifact_store(tmp_path)
    plan = store.write_text("plan.md", "original")
    (store.root / "manifest.json").unlink()
    with pytest.raises(ArtifactIntegrityError, match="missing version-2"):
        artifact_store(tmp_path)
    assert plan.read_text() == "original"


@pytest.mark.parametrize("relative", ["plan.md", "manifest.json"])
def test_named_pipe_evidence_is_rejected_without_blocking(tmp_path: Path, relative: str) -> None:
    store = artifact_store(tmp_path)
    store.write_text("plan.md", "original")
    target = store.root / relative
    target.unlink()
    os.mkfifo(target)
    script = """
import sys
from pathlib import Path
from software_factory.artifacts import ArtifactStore, ArtifactIntegrityError
try:
    ArtifactStore(Path(sys.argv[1]), 'fct_evidence', storage_root=Path(sys.argv[2]), create=False)
except ArtifactIntegrityError:
    pass
else:
    raise SystemExit('special file was accepted')
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "workspace"), str(tmp_path / "controller")],
        capture_output=True,
        text=True,
        timeout=3,
    )
    assert result.returncode == 0, result.stderr
