"""Read historical records in place; never manufacture new ownership or evidence."""

import json
import os
import sqlite3
from pathlib import Path
from typing import Any


def inspect_legacy(workspace: Path, storage: Path, run_id: str, parent_task: str) -> dict[str, Any]:
    database = storage.expanduser().resolve() / "factory" / "checkpoints.sqlite"
    if database.stat().st_uid != os.getuid():
        raise ValueError("legacy storage belongs to a different local user")
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM factory_runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise ValueError("unknown legacy run")
        if (
            Path(row["workspace"]).resolve() != workspace.resolve()
            or row["parent_thread_id"] != parent_task
        ):
            raise ValueError("legacy run belongs to a different workspace or parent task")
        record = dict(row)
        return {
            "mode": "legacy_read_only",
            "run": record,
            "result": json.loads(record.get("result_json") or "{}"),
            "recovery": "Keep this storage in place. Use legacy-resume or legacy-cancel with the original BB context. See docs/interface-migration.md.",
        }
