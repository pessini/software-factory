from pathlib import Path

import pytest

from software_factory.bb.client import BBError
from software_factory.worker_container import ContainerProvider


@pytest.mark.parametrize("failure", ["credentials", "mount", "dispatch"])
async def test_creation_record_follows_local_validation(tmp_path: Path, monkeypatch, failure):
    events = []

    async def absent(*args):
        return None

    async def create(*args, **kwargs):
        events.append("dispatch")
        raise BBError("create reply lost", "connection")

    monkeypatch.setattr("software_factory.worker_container.inspect_container", absent)
    monkeypatch.setattr("software_factory.worker_container.container_command", create)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "empty-login"))
    workspace = tmp_path / ("bad:path" if failure == "mount" else "workspace")
    workspace.mkdir()
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    provider = ContainerProvider(
        "container",
        "request",
        workspace,
        evidence,
        tmp_path / "provider",
        [],
        read_only=True,
        authenticate=failure == "credentials",
    )
    with pytest.raises(BBError):
        await provider.start(before_create=lambda: events.append("saved"))
    assert events == (["saved", "dispatch"] if failure == "dispatch" else [])
