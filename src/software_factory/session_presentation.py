"""Render saved events with ACP's limited plan and tool status vocabulary."""

from __future__ import annotations

from typing import Literal, cast

from acp import plan_entry, start_tool_call, update_plan, update_tool_call
from acp.helpers import SessionUpdate

from software_factory.events import FactoryEvent
from software_factory.state import FINAL_STATUSES

PHASES = (
    "bootstrap",
    "baseline",
    "plan",
    "build",
    "test",
    "repair",
    "review",
    "revise",
    "finalize",
)


class EventPresentation:
    def __init__(self) -> None:
        self.phases = dict.fromkeys(PHASES, "pending")
        self.open_calls: dict[str, str] = {}

    def updates(self, event: FactoryEvent) -> list[SessionUpdate]:
        updates: list[SessionUpdate] = []
        node, status = event.node, event.status
        run_status = event.metadata.get("status") if node in {"state", "recovery"} else None
        if run_status in FINAL_STATUSES:
            for phase, current in self.phases.items():
                if current == "pending":
                    self.phases[phase] = "unused"
                elif current in {"started", "waiting", "recovery_needed"}:
                    self.phases[phase] = (
                        "completed" if run_status == "ready_for_review" else str(run_status)
                    )
            for name, call_id in self.open_calls.items():
                updates.append(
                    update_tool_call(
                        call_id,
                        title=f"{name.title()}: {run_status}",
                        status="completed" if run_status == "ready_for_review" else "failed",
                    )
                )
            self.open_calls.clear()
        elif run_status in {"waiting_for_input", "recovery_needed"}:
            for phase, current in self.phases.items():
                if current == "started":
                    self.phases[phase] = (
                        "waiting" if run_status == "waiting_for_input" else "recovery_needed"
                    )
            for name, call_id in self.open_calls.items():
                updates.append(
                    update_tool_call(
                        call_id,
                        title=f"{name.title()}: {str(run_status).replace('_', ' ')}",
                        status="in_progress",
                    )
                )
        if node in self.phases and status != "info":
            self.phases[node] = status
        call_id = f"{event.run_id}:event:{event.sequence}"
        if status == "info":
            updates.append(
                start_tool_call(call_id, event.message, kind="other", status="completed")
            )
        elif status == "started":
            previous = self.open_calls.pop(node, None)
            if previous:
                updates.append(
                    update_tool_call(
                        previous,
                        title=f"{node.title()}: interrupted; continuing saved work",
                        status="failed",
                    )
                )
            self.open_calls[node] = call_id
            updates.append(
                start_tool_call(
                    call_id,
                    event.message,
                    kind="execute",
                    status="in_progress",
                    raw_input={"run_id": event.run_id, "node": node, **event.metadata},
                )
            )
        else:
            existing = self.open_calls.get(node)
            tool_status = (
                "in_progress"
                if status == "waiting"
                else "completed"
                if status == "completed"
                else "failed"
            )
            if existing:
                updates.append(
                    update_tool_call(
                        existing,
                        title=event.message,
                        status=cast(Literal["in_progress", "completed", "failed"], tool_status),
                        raw_output=event.model_dump(mode="json"),
                    )
                )
            else:
                updates.append(
                    start_tool_call(
                        call_id,
                        event.message,
                        kind="other",
                        status=cast(Literal["in_progress", "completed", "failed"], tool_status),
                        raw_input=event.model_dump(mode="json"),
                    )
                )
                if status == "waiting":
                    self.open_calls[node] = call_id
            if status != "waiting":
                self.open_calls.pop(node, None)
        updates.append(
            update_plan(
                plan_entry(
                    f"{phase.title()}: {current.replace('_', ' ')}",
                    status="pending"
                    if current == "pending"
                    else "in_progress"
                    if current in {"started", "waiting", "recovery_needed"}
                    else "completed",
                )
                for phase, current in self.phases.items()
            )
        )
        return updates
