"""Explicit read-only export of quiescent Python planning revisions for Rust import."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from software_factory.artifacts import ArtifactStore
from software_factory.planning import snapshot_hash
from software_factory.planning_control import PlanningControl
from software_factory.state import FINAL_STATUSES


def _require_quiescent(run: dict[str, Any]) -> None:
    run_id = run["run_id"]
    if run["controller_state"] in {"running", "unavailable", "recovery_needed"}:
        raise ValueError(f"{run_id}: reconcile the original controller before export")
    if any(op["state"] not in {"completed", "stopped"} for op in run["operations"]):
        raise ValueError(f"{run_id}: reconcile worker operations before export")


async def export_planning(planning: PlanningControl) -> dict[str, Any]:
    if planning.remote:
        raise ValueError("export must read the original local Python storage")
    sessions = []
    for summary in await planning.sessions():
        revisions = []
        for revision in await planning.store.planning_revisions(summary["session_id"]):
            run_id = revision["run_id"]
            run = await planning.base.status(run_id)
            if run["status"] not in FINAL_STATUSES | {"waiting_for_input"}:
                raise ValueError(f"{run_id}: stop or finish active Python work before export")
            _require_quiescent(run)
            handoff = await planning.store.planning_handoff(run_id)
            implementation = None
            if handoff:
                implementation = await planning.base.status(handoff["implementation_run_id"])
                implementation["pull_request"] = (implementation.get("delivery") or {}).get(
                    "pr_url"
                )
                if implementation["status"] not in FINAL_STATUSES:
                    raise ValueError(f"{run_id}: finish or cancel its implementation before export")
                _require_quiescent(implementation)
                report_path = implementation.get("final_report_path")
                if report_path:
                    impl_artifacts = ArtifactStore(
                        planning.workspace,
                        handoff["implementation_run_id"],
                        storage_root=planning.storage_root / "factory" / "artifacts",
                        create=False,
                    )
                    implementation["report"] = impl_artifacts.read_text(Path(report_path).name)
                    implementation["final_report_path"] = "legacy-report"
            artifacts = ArtifactStore(
                planning.workspace,
                run_id,
                storage_root=planning.storage_root / "factory" / "artifacts",
                create=False,
            )
            snapshot = planning._snapshot(run_id) if run["status"] == "planned" else None
            if run["status"] == "planned" and (
                snapshot is None or snapshot_hash(snapshot) != run["planning"]["plan_hash"]
            ):
                raise ValueError(f"{run_id}: saved plan integrity check failed")
            artifacts.verify()
            if (
                run["status"] in {"failed", "blocked", "cancelled"}
                and not artifacts.path("documents.json").exists()
            ):
                documents = {}
            else:
                documents = json.loads(artifacts.read_text("documents.json"))
            rounds = []
            for question in run.get("interactions", []):
                round_number = question["interaction_id"].rsplit(":", 1)[-1]
                envelope = json.loads(artifacts.read_text(f"grill-round-{round_number}.json"))
                rounds.append({**question, "envelope": envelope})
            revisions.append(
                {
                    "source_run_id": run_id,
                    "status": run["status"],
                    "request": run["request"],
                    "feedback": (run.get("planning") or {}).get("feedback"),
                    "snapshot": snapshot,
                    "snapshot_content": json.dumps(snapshot, sort_keys=True) if snapshot else None,
                    "plan_hash": run.get("planning", {}).get("plan_hash"),
                    "documents": documents,
                    "rounds": rounds,
                    "configuration": json.loads(artifacts.read_text("config.json")),
                    "legacy_handoff": handoff,
                    "legacy_implementation": implementation,
                    "source_record": run,
                }
            )
        sessions.append(
            {
                "session_id": summary["session_id"],
                "created_at": summary["created_at"],
                "revisions": revisions,
            }
        )
    return {"version": 1, "workspace": str(planning.workspace), "sessions": sessions}
