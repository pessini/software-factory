import asyncio
import hashlib

import pytest

from software_factory.legacy import inspect_legacy
from software_factory.persistence import RunStore


def test_historical_inspection_preserves_database_bytes_and_checks_workspace(tmp_path):
    store = RunStore(tmp_path / "storage/factory/checkpoints.sqlite")

    async def save():
        await store.initialize()
        await store.create_run("old-run", tmp_path / "target", "old-parent")
        await store.set_status("old-run", "failed", result={"evidence_version": 1})

    asyncio.run(save())
    digest = hashlib.sha256(store.path.read_bytes()).digest()
    result = inspect_legacy(tmp_path / "target", tmp_path / "storage", "old-run", "old-parent")
    assert result["result"]["evidence_version"] == 1
    assert hashlib.sha256(store.path.read_bytes()).digest() == digest
    with pytest.raises(ValueError, match="different workspace"):
        inspect_legacy(tmp_path / "other", tmp_path / "storage", "old-run", "old-parent")
