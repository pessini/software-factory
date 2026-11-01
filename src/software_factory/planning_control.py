"""Planning session commands shared by the web UI, MCP tools, BB, and the CLI."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, cast

from software_factory.artifacts import ArtifactIntegrityError, ArtifactStore
from software_factory.control import FactoryControl
from software_factory.planning import PLANNING_WORKFLOW, handoff_criteria, handoff_request
from software_factory.remote import RemoteTransport
from software_factory.state import FINAL_STATUSES


def _digest(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()


class PlanningControl:
    """A planning session is a series of immutable planning runs (revisions).

    Each revision finishes as "planned" with a plan snapshot. Approving the exact
    snapshot hash starts one implementation run through the shared start operation.
    """

    def __init__(self, workspace: Path, *, storage_root: Path | None = None, **options: Any):
        self.remote = RemoteTransport.from_environment(workspace)
        self.workspace = workspace.expanduser().resolve(strict=True)
        self.options = {key: value for key, value in options.items() if key != "session_id"}
        self.base = FactoryControl(self.workspace, storage_root=storage_root, **options)
        self.storage_root = self.base.context.storage_root
        self.store = self.base.store

    def _control(self, session: str) -> FactoryControl:
        return FactoryControl(
            self.workspace, storage_root=self.storage_root, session_id=session, **self.options
        )

    async def start(self, request: str, *, request_id: str) -> dict[str, Any]:
        if self.remote:
            return await self.remote.call(
                "plan_start",
                request=request,
                request_id=request_id,
                client_session_id=self.base.context.session_id,
            )
        if not request.strip() or not request_id.strip() or len(request_id) > 200:
            raise ValueError("a request and a request ID of 1 to 200 characters are required")
        session_id = "pln_" + _digest(str(self.workspace), request_id)[:12]
        return await self._start_revision(session_id, 1, request, "plan:" + request_id, {})

    async def _start_revision(
        self,
        session_id: str,
        revision: int,
        request: str,
        request_id: str,
        extra: dict[str, Any],
    ) -> dict[str, Any]:
        planning = {"session_id": session_id, "revision": revision, **extra}
        started = await self._control("planning:" + session_id).start(
            request, request_id=request_id, workflow=PLANNING_WORKFLOW, planning=planning
        )
        await self.store.add_planning_revision(session_id, revision, started["run_id"])
        return await self.status(session_id)

    async def sessions(self) -> list[dict[str, Any]]:
        if self.remote:
            return await self.remote.listing("plan_list")
        await self.store.initialize()
        result = []
        for item in await self.store.planning_sessions():
            revisions = await self.store.planning_revisions(item["session_id"])
            first = await self.store.get_run(revisions[0]["run_id"])
            latest = await self.store.get_run(revisions[-1]["run_id"])
            if first is None or latest is None:
                continue
            if Path(str(first["workspace"])).resolve() != self.workspace:
                continue
            request = json.loads(first["result_json"] or "{}").get("request", "")
            result.append(
                {
                    "session_id": item["session_id"],
                    "revision": item["revision"],
                    "status": latest["status"],
                    "request": request,
                    "created_at": item["created_at"],
                }
            )
        return result

    async def _revisions(self, session_id: str) -> list[dict[str, Any]]:
        await self.store.initialize()
        revisions = await self.store.planning_revisions(session_id)
        if not revisions:
            raise ValueError(f"unknown planning session: {session_id}")
        return revisions

    def _snapshot(self, run_id: str) -> dict[str, Any] | None:
        try:
            artifacts = ArtifactStore(
                self.workspace,
                run_id,
                storage_root=self.storage_root / "factory" / "artifacts",
                create=False,
            )
            return cast(dict[str, Any], json.loads(artifacts.read_text("plan-snapshot.json")))
        except FileNotFoundError, ArtifactIntegrityError, ValueError:
            return None

    async def status(self, session_id: str, *, after_event: int = 0) -> dict[str, Any]:
        if self.remote:
            return await self.remote.call(
                "plan_status", session_id=session_id, after_event=after_event
            )
        revisions = await self._revisions(session_id)
        current = revisions[-1]
        run = await self.base.status(current["run_id"], after_event=after_event)
        planning = run.get("planning") or {}
        snapshot = self._snapshot(current["run_id"]) if run["status"] == "planned" else None
        handoff = await self.store.planning_handoff(current["run_id"])
        implementation = None
        if handoff:
            impl = await self.base.status(handoff["implementation_run_id"])
            implementation = {
                "run_id": impl["run_id"],
                "status": impl["status"],
                "current_step": impl.get("current_step"),
                "error": impl.get("error"),
                "final_report_path": impl.get("final_report_path"),
                "pull_request": (impl.get("delivery") or {}).get("pr_url"),
                "interactions": impl.get("interactions", []),
            }
        history = []
        for item in revisions:
            record = await self.store.get_run(item["run_id"])
            saved = json.loads(record["result_json"] or "{}") if record else {}
            history.append(
                {
                    "revision": item["revision"],
                    "run_id": item["run_id"],
                    "status": record["status"] if record else "unknown",
                    "feedback": (saved.get("planning") or {}).get("feedback"),
                    "plan_hash": (saved.get("planning") or {}).get("plan_hash"),
                    "handoff": await self.store.planning_handoff(item["run_id"]),
                }
            )
        rounds = []
        for item in run.get("interactions", []):
            round_file = None
            if item["interaction_id"].rsplit(":", 1)[-1].isdigit():
                round_file = f"grill-round-{item['interaction_id'].rsplit(':', 1)[-1]}.json"
            envelope = self._read_artifact(current["run_id"], round_file) if round_file else None
            rounds.append({**item, "envelope": envelope})
        return {
            "session_id": session_id,
            "revision": current["revision"],
            "run_id": current["run_id"],
            "status": run["status"],
            "controller_state": run.get("controller_state"),
            "error": run.get("error"),
            "request": run.get("request"),
            "feedback": planning.get("feedback"),
            "plan_hash": planning.get("plan_hash"),
            "rounds": rounds,
            "pending": next(
                (item for item in rounds if item["state"] == "waiting"),
                None,
            ),
            "snapshot": snapshot,
            "handoff": handoff,
            "implementation": implementation,
            "history": history,
            "events": run.get("events", []),
            "event_position": run.get("event_position", 0),
        }

    def _read_artifact(self, run_id: str, name: str) -> dict[str, Any] | None:
        try:
            artifacts = ArtifactStore(
                self.workspace,
                run_id,
                storage_root=self.storage_root / "factory" / "artifacts",
                create=False,
            )
            return cast(dict[str, Any], json.loads(artifacts.read_text(name)))
        except FileNotFoundError, ArtifactIntegrityError, ValueError:
            return None

    async def answer(
        self, session_id: str, interaction_id: str, answers: dict[str, str]
    ) -> dict[str, Any]:
        if self.remote:
            return await self.remote.call(
                "plan_answer", session_id=session_id, interaction_id=interaction_id, answers=answers
            )
        current = (await self._revisions(session_id))[-1]
        if not interaction_id.startswith(current["run_id"] + ":"):
            raise ValueError("this question belongs to an earlier plan revision")
        await self.base.answer(current["run_id"], interaction_id, answers)
        return await self.status(session_id)

    async def resume(self, session_id: str) -> dict[str, Any]:
        if self.remote:
            return await self.remote.call("plan_resume", session_id=session_id)
        current = (await self._revisions(session_id))[-1]
        await self.base.resume(current["run_id"])
        return await self.status(session_id)

    async def cancel(self, session_id: str) -> dict[str, Any]:
        if self.remote:
            return await self.remote.call("plan_cancel", session_id=session_id)
        current = (await self._revisions(session_id))[-1]
        await self.base.cancel(current["run_id"])
        return await self.status(session_id)

    async def revise(self, session_id: str, feedback: str) -> dict[str, Any]:
        """Start a new revision from the latest one; the earlier snapshot stays unchanged."""
        if self.remote:
            return await self.remote.call("plan_revise", session_id=session_id, feedback=feedback)
        if not feedback.strip():
            raise ValueError("revision feedback is required")
        revisions = await self._revisions(session_id)
        current = revisions[-1]
        record = await self.store.get_run(current["run_id"])
        assert record is not None
        if record["status"] not in FINAL_STATUSES:
            raise ValueError("answer, resume, or cancel the current revision before revising")
        saved = json.loads(record["result_json"] or "{}")
        snapshot = self._snapshot(current["run_id"])
        previous = {
            "run_id": current["run_id"],
            "revision": current["revision"],
            "status": record["status"],
            "summary": snapshot["summary"] if snapshot else saved.get("error"),
            "plan": snapshot["plan"] if snapshot else None,
            "decisions": snapshot["decisions"] if snapshot else [],
            "glossary": snapshot["glossary"] if snapshot else [],
            "rounds": snapshot["rounds"] if snapshot else [],
        }
        request_id = "revise:" + _digest(current["run_id"], feedback)[:32]
        return await self._start_revision(
            session_id,
            current["revision"] + 1,
            str(saved["request"]),
            request_id,
            {"previous": previous, "feedback": feedback},
        )

    async def approve(self, session_id: str, plan_hash: str) -> dict[str, Any]:
        """Start exactly one implementation run for the exact approved plan revision."""
        if self.remote:
            return await self.remote.call(
                "plan_approve", session_id=session_id, plan_hash=plan_hash
            )
        current = (await self._revisions(session_id))[-1]
        existing = await self.store.planning_handoff(current["run_id"])
        if existing and existing["plan_hash"] == plan_hash:
            return await self.status(session_id)
        record = await self.store.get_run(current["run_id"])
        assert record is not None
        saved = json.loads(record["result_json"] or "{}")
        if record["status"] != "planned":
            raise ValueError("only a completed plan revision can be approved")
        if (saved.get("planning") or {}).get("plan_hash") != plan_hash:
            raise ValueError("the plan changed; review the current revision before approving")
        snapshot = self._snapshot(current["run_id"])
        if snapshot is None:
            raise ValueError("the saved plan snapshot is missing or was modified")
        request_id = f"handoff:{current['run_id']}:{plan_hash[:16]}"
        started = await self._control("implementation:" + session_id).start(
            handoff_request(snapshot, plan_hash),
            request_id=request_id,
            required_criteria=handoff_criteria(snapshot),
        )
        await self.store.save_planning_handoff(
            current["run_id"], plan_hash, request_id, started["run_id"]
        )
        return await self.status(session_id)


def planning_summary(state: dict[str, Any]) -> str:
    """Plain text for chat interfaces; the web UI renders the same saved state."""
    lines = [
        f"Planning session {state['session_id']} revision {state['revision']}: {state['status']}."
    ]
    pending = state.get("pending")
    if pending:
        lines.append(f"Questions ({pending['interaction_id']}):")
        for question in pending["questions"]:
            lines.append(f"- {question['id']}: {question['question']}")
            if question.get("options"):
                lines.append("  Choices: " + "; ".join(json.dumps(o) for o in question["options"]))
            if question.get("recommendation"):
                lines.append(f"  Recommended: {json.dumps(question['recommendation'])}")
        lines.append(
            f"Reply with /plan-answer {state['session_id']} "
            '{"QUESTION_ID": "answer", ...}; start an answer with [discuss] to ask about it.'
        )
    snapshot = state.get("snapshot")
    if snapshot and not state.get("handoff"):
        plan = snapshot["plan"]
        lines.append(f"Plan ready: {plan['goal']}")
        lines.extend(
            f"- check {item['id']}: {item['description']}" for item in plan["completion_checks"]
        )
        lines.append(
            f"Approve this exact revision with /plan-approve {state['session_id']} {state['plan_hash']}, "
            f"or change it with /plan-revise {state['session_id']} FEEDBACK."
        )
    implementation = state.get("implementation")
    if implementation:
        lines.append(
            f"Implementation run {implementation['run_id']}: {implementation['status']}."
            " Use /attach with that run ID for its controls."
        )
    if state.get("error") and state["status"] in FINAL_STATUSES:
        lines.append(f"Error: {state['error']}")
    return "\n".join(lines)
