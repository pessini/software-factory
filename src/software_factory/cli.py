from __future__ import annotations

import asyncio
import json
import sys
from contextvars import ContextVar
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import uuid4

import typer
from rich.console import Console
from rich.table import Table

from software_factory.acp_agent import serve_acp
from software_factory.bb import BBContext
from software_factory.config import CONFIG_PATH, DeliveryPolicy, default_config, load_config
from software_factory.control import FactoryControl
from software_factory.doctor import run_doctor
from software_factory.engine import FactoryEngine
from software_factory.graph import MERMAID
from software_factory.review_quality import ReviewFeedback

app = typer.Typer(
    no_args_is_help=True, help="Factory controller with optional BB and MCP interfaces"
)
console = Console()
DEFAULT_WORKSPACE = Path.cwd()
_cli_options: ContextVar[dict[str, Any] | None] = ContextVar("factory_cli_options", default=None)


@app.callback()
def global_options(
    ctx: typer.Context,
    storage: Annotated[
        Path | None,
        typer.Option(
            "--storage", help="Private Factory storage outside the target; defaults to FACTORY_HOME"
        ),
    ] = None,
    session: Annotated[str | None, typer.Option("--session", help="Factory session ID")] = None,
) -> None:
    token = _cli_options.set({"storage_root": storage, "session_id": session})
    ctx.call_on_close(lambda: _cli_options.reset(token))


def _control(workspace: Path) -> FactoryControl:
    return FactoryControl(_workspace(workspace), **(_cli_options.get() or {}))


def _engine(workspace: Path, run_id: str) -> FactoryEngine:
    return asyncio.run(_control(workspace).engine(run_id))


def _workspace(value: Path) -> Path:
    return value.expanduser().resolve()


def _successful(result: dict[str, Any]) -> bool:
    return result.get("status") == "ready_for_review" and result.get("delivery", {}).get(
        "status", "manual"
    ) in {"manual", "no_change", "delivered"}


def _context_or_none() -> BBContext | None:
    try:
        return BBContext.from_environment()
    except ValueError:
        return None


def _required_context() -> BBContext:
    context = _context_or_none()
    if context is None:
        raise typer.BadParameter(
            "missing BB context; run inside a BB thread or set BB_THREAD_ID, BB_PROJECT_ID, "
            "BB_ENVIRONMENT_ID, and BB_THREAD_STORAGE"
        )
    return context


@app.command()
def doctor(
    workspace: Annotated[Path, typer.Option("--workspace", "-w")] = DEFAULT_WORKSPACE,
) -> None:
    """Validate the local machine, worker, repository, policy, and Factory storage."""
    checks = run_doctor(_workspace(workspace), _control(workspace).context)
    table = Table(title="Software Factory doctor")
    table.add_column("Check")
    table.add_column("Status")
    table.add_column("Detail")
    for check in checks:
        table.add_row(check.name, "OK" if check.ok else "FAIL", check.detail)
    console.print(table)
    if not all(check.ok for check in checks):
        raise typer.Exit(1)


@app.command("repo-check")
def repo_check(
    workspace: Annotated[Path, typer.Option("--workspace", "-w")] = DEFAULT_WORKSPACE,
) -> None:
    """Validate only a repository's factory policy."""
    config = load_config(_workspace(workspace))
    console.print(
        f"[green]valid[/green] workflow={config.workflow} commands={len(config.quality.commands)}"
    )
    for command in config.quality.commands:
        console.print(f"  {command.name}: {command.display}")


@app.command("init")
def init_repository(
    workspace: Annotated[Path, typer.Option("--workspace", "-w")] = DEFAULT_WORKSPACE,
    force: Annotated[bool, typer.Option("--force")] = False,
) -> None:
    """Generate a strict repository policy template."""
    path = _workspace(workspace) / CONFIG_PATH
    if path.exists() and not force:
        raise typer.BadParameter(f"configuration already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(default_config(), encoding="utf-8")
    console.print(f"created {path}")


@app.command("graph")
def graph_command(
    format: Annotated[Literal["mermaid"], typer.Option("--format")] = "mermaid",
) -> None:
    """Print the compiled workflow topology."""
    del format
    console.print(MERMAID, markup=False)


@app.command("run")
def run_command(
    request: Annotated[str, typer.Argument(help="Bounded feature request")],
    workspace: Annotated[Path, typer.Option("--workspace", "-w")] = DEFAULT_WORKSPACE,
) -> None:
    """Save a workflow and return its ID while the controller runs independently."""
    result = asyncio.run(_control(workspace).start(request, request_id=uuid4().hex))
    console.print_json(json.dumps(result))
    if result["status"] in {"failed", "blocked"}:
        raise typer.Exit(1)


@app.command("resume")
def resume_command(
    run_id: str,
    workspace: Annotated[Path, typer.Option("--workspace", "-w")] = DEFAULT_WORKSPACE,
) -> None:
    """Resume a run from its durable LangGraph checkpoint."""
    result = asyncio.run(_control(workspace).resume(run_id))
    console.print_json(json.dumps(result))
    if result["status"] in {"failed", "blocked", "cancelled"}:
        raise typer.Exit(1)


@app.command("deliver")
def deliver_command(
    run_id: str,
    policy: Annotated[
        Path | None,
        typer.Option(help="Reviewed JSON delivery policy; saved on first authorization"),
    ] = None,
    workspace: Annotated[Path, typer.Option("--workspace", "-w")] = DEFAULT_WORKSPACE,
) -> None:
    """Deliver verified changes or refresh the saved PR's CI result."""
    authorization = DeliveryPolicy.model_validate_json(policy.read_text()) if policy else None
    engine = _engine(workspace, run_id)
    result = asyncio.run(engine.deliver(run_id, authorization=authorization))
    console.print_json(json.dumps(result))
    if not _successful(dict(result)):
        raise typer.Exit(1)


@app.command("review-threads")
def review_threads_command(
    run_id: str,
    workspace: Annotated[Path, typer.Option("--workspace", "-w")] = DEFAULT_WORKSPACE,
) -> None:
    """List PR review thread IDs and findings for operator selection."""
    engine = _engine(workspace, run_id)
    console.print_json(json.dumps(asyncio.run(engine.review_threads(run_id))))


@app.command("fix-reviews")
def fix_reviews_command(
    run_id: str,
    thread: Annotated[
        list[str],
        typer.Option("--thread", help="Accepted review thread ID; repeat for each finding"),
    ],
    workspace: Annotated[Path, typer.Option("--workspace", "-w")] = DEFAULT_WORKSPACE,
) -> None:
    """Implement, verify, push, reply to, and resolve selected authorized findings."""
    engine = _engine(workspace, run_id)
    result = asyncio.run(engine.fix_reviews(run_id, thread))
    console.print_json(json.dumps(result))
    if not _successful(dict(result)):
        raise typer.Exit(1)


@app.command("inspect")
def inspect_command(
    run_id: str,
    workspace: Annotated[Path, typer.Option("--workspace", "-w")] = DEFAULT_WORKSPACE,
    after_event: Annotated[int, typer.Option("--after-event")] = 0,
) -> None:
    """Read saved state and events without dispatching any work."""
    console.print_json(
        json.dumps(asyncio.run(_control(workspace).status(run_id, after_event=after_event)))
    )


@app.command("start")
def start_command(
    request: str,
    request_id: Annotated[str, typer.Option("--request-id")],
    workspace: Annotated[Path, typer.Option("--workspace", "-w")] = DEFAULT_WORKSPACE,
) -> None:
    """Save one workflow for a caller-supplied request ID."""
    console.print_json(
        json.dumps(asyncio.run(_control(workspace).start(request, request_id=request_id)))
    )


@app.command("models")
def models_command(
    workspace: Annotated[Path, typer.Option("--workspace", "-w")] = DEFAULT_WORKSPACE,
) -> None:
    """List supported protected worker models without BB."""
    console.print_json(json.dumps(asyncio.run(_control(workspace).models())))


@app.command("mcp")
def mcp_command(
    workspace: Annotated[Path, typer.Option("--workspace", "-w")] = DEFAULT_WORKSPACE,
) -> None:
    """Serve local MCP tools for one authorized workspace over stdio."""
    from software_factory.mcp_server import create_mcp
    from software_factory.planning_control import PlanningControl

    options = _cli_options.get() or {}
    create_mcp(
        _control(workspace),
        PlanningControl(_workspace(workspace), storage_root=options.get("storage_root")),
    ).run(transport="stdio")


def _planning(workspace: Path) -> Any:
    from software_factory.planning_control import PlanningControl

    options = _cli_options.get() or {}
    return PlanningControl(_workspace(workspace), storage_root=options.get("storage_root"))


@app.command("plan")
def plan_command(
    request: str,
    request_id: Annotated[str, typer.Option("--request-id")] = "",
    workspace: Annotated[Path, typer.Option("--workspace", "-w")] = DEFAULT_WORKSPACE,
) -> None:
    """Start a Grill planning interview; answers and approval continue it."""
    result = asyncio.run(_planning(workspace).start(request, request_id=request_id or uuid4().hex))
    console.print_json(json.dumps(result))


@app.command("plan-status")
def plan_status_command(
    session_id: Annotated[
        str, typer.Argument(help="Planning session ID; omit to list sessions")
    ] = "",
    workspace: Annotated[Path, typer.Option("--workspace", "-w")] = DEFAULT_WORKSPACE,
) -> None:
    """Show a planning session, or list sessions for the workspace."""
    planning = _planning(workspace)
    result = asyncio.run(planning.status(session_id) if session_id else planning.sessions())
    console.print_json(json.dumps(result))


@app.command("plan-answer")
def plan_answer_command(
    session_id: str,
    interaction_id: str,
    answers: Annotated[str, typer.Argument(help="JSON object mapping question IDs to answers")],
    workspace: Annotated[Path, typer.Option("--workspace", "-w")] = DEFAULT_WORKSPACE,
) -> None:
    """Submit one planning round."""
    payload = json.loads(answers)
    if not isinstance(payload, dict):
        raise typer.BadParameter("answers must be a JSON object")
    result = asyncio.run(_planning(workspace).answer(session_id, interaction_id, payload))
    console.print_json(json.dumps(result))


@app.command("plan-revise")
def plan_revise_command(
    session_id: str,
    feedback: str,
    workspace: Annotated[Path, typer.Option("--workspace", "-w")] = DEFAULT_WORKSPACE,
) -> None:
    """Create a new plan revision from feedback."""
    console.print_json(json.dumps(asyncio.run(_planning(workspace).revise(session_id, feedback))))


@app.command("plan-approve")
def plan_approve_command(
    session_id: str,
    plan_hash: Annotated[str, typer.Argument(help="The exact plan sha256 being approved")],
    workspace: Annotated[Path, typer.Option("--workspace", "-w")] = DEFAULT_WORKSPACE,
) -> None:
    """Approve the exact plan revision and start its one implementation run."""
    console.print_json(json.dumps(asyncio.run(_planning(workspace).approve(session_id, plan_hash))))


@app.command("web")
def web_command(
    workspace: Annotated[Path, typer.Option("--workspace", "-w")] = DEFAULT_WORKSPACE,
    port: Annotated[int, typer.Option("--port")] = 4317,
) -> None:
    """Serve the local planning web UI for one workspace on 127.0.0.1."""
    from software_factory.web import serve_web

    options = _cli_options.get() or {}
    serve_web(_workspace(workspace), storage_root=options.get("storage_root"), port=port)


@app.command("answer")
def answer_command(
    run_id: str,
    interaction_id: str,
    answers: Annotated[str, typer.Argument(help="JSON object mapping question IDs to answers")],
    workspace: Annotated[Path, typer.Option("--workspace", "-w")] = DEFAULT_WORKSPACE,
) -> None:
    """Save an answer and continue the same run."""
    payload = json.loads(answers)
    if not isinstance(payload, dict):
        raise typer.BadParameter("answers must be a JSON object")
    result = asyncio.run(_control(workspace).answer(run_id, interaction_id, payload))
    console.print_json(json.dumps(result))
    if result["status"] in {"failed", "blocked", "cancelled"}:
        raise typer.Exit(1)


@app.command("review-quality")
def review_quality_command(
    run_id: str,
    workspace: Annotated[Path, typer.Option("--workspace", "-w")] = DEFAULT_WORKSPACE,
) -> None:
    """Show saved review results and user feedback for threshold tuning."""
    engine = _engine(workspace, run_id)
    console.print_json(json.dumps(asyncio.run(engine.review_quality(run_id))))


@app.command("review-feedback")
def review_feedback_command(
    run_id: str,
    attempt: int,
    reviewer: str,
    finding_id: str,
    verdict: str,
    reason: str,
    workspace: Annotated[Path, typer.Option("--workspace", "-w")] = DEFAULT_WORKSPACE,
) -> None:
    """Record false_positive or useful feedback without changing review decisions."""
    feedback = ReviewFeedback.model_validate(
        {
            "attempt": attempt,
            "reviewer": reviewer,
            "finding_id": finding_id,
            "verdict": verdict,
            "reason": reason,
        }
    )
    engine = _engine(workspace, run_id)
    console.print_json(json.dumps(asyncio.run(engine.record_review_feedback(run_id, feedback))))


@app.command("retry")
def retry_command(
    run_id: str,
    workspace: Annotated[Path, typer.Option("--workspace", "-w")] = DEFAULT_WORKSPACE,
) -> None:
    """Retry recovery of saved operations in a supported state."""
    result = asyncio.run(_control(workspace).resume(run_id))
    console.print_json(json.dumps(result))
    if result["status"] in {"failed", "blocked", "cancelled"}:
        raise typer.Exit(1)


@app.command("cancel")
def cancel_command(
    run_id: str,
    workspace: Annotated[Path, typer.Option("--workspace", "-w")] = DEFAULT_WORKSPACE,
) -> None:
    """Stop active external work and mark a run cancelled."""
    result = asyncio.run(_control(workspace).cancel(run_id))
    console.print_json(json.dumps(result))
    if result.get("status") == "recovery_needed":
        raise typer.Exit(1)


@app.command("acp")
def acp_command() -> None:
    """Serve the Software Factory agent over ACP stdio."""
    asyncio.run(serve_acp())


@app.command("worker-acp", hidden=True)
def worker_acp_command() -> None:
    """Serve a protected provider worker for a saved BB factory operation."""
    from software_factory.bb.worker_acp import serve_worker

    serve_worker(_required_context())


@app.command("legacy-inspect")
def legacy_inspect_command(
    run_id: str,
    storage: Path,
    parent_task: str,
    workspace: Annotated[Path, typer.Option("--workspace", "-w")] = DEFAULT_WORKSPACE,
) -> None:
    """Read old BB records in place without changing their files or version."""
    from software_factory.legacy import inspect_legacy

    console.print_json(
        json.dumps(inspect_legacy(_workspace(workspace), storage, run_id, parent_task))
    )


@app.command("legacy-resume")
def legacy_resume_command(
    run_id: str, workspace: Annotated[Path, typer.Option("--workspace", "-w")] = DEFAULT_WORKSPACE
) -> None:
    """Reconcile an old run through its original BB context and storage."""
    from software_factory.bb import BBClient

    context = _required_context()
    engine = FactoryEngine(
        workspace=_workspace(workspace),
        context=context,
        execution=BBClient.from_context(context, _workspace(workspace)),
    )
    console.print_json(json.dumps(asyncio.run(engine.resume(run_id))))


@app.command("legacy-cancel")
def legacy_cancel_command(
    run_id: str, workspace: Annotated[Path, typer.Option("--workspace", "-w")] = DEFAULT_WORKSPACE
) -> None:
    """Stop an old run through its original BB context and confirm worker exit."""
    from software_factory.bb import BBClient

    context = _required_context()
    engine = FactoryEngine(
        workspace=_workspace(workspace),
        context=context,
        execution=BBClient.from_context(context, _workspace(workspace)),
    )
    console.print_json(json.dumps(asyncio.run(engine.cancel_external(run_id))))


@app.command("convert-config")
def convert_config_command(
    workspace: Annotated[Path, typer.Option("--workspace", "-w")] = DEFAULT_WORKSPACE,
) -> None:
    """Print a version-2 policy copy; leave the repository policy unchanged."""
    from software_factory.config import upgrade_config

    console.print_json(upgrade_config(load_config(_workspace(workspace))).model_dump_json())


def main() -> None:
    app()


@app.command("settings-panel", hidden=True)
def settings_panel(
    workspace: Annotated[Path, typer.Option("--workspace", "-w")] = DEFAULT_WORKSPACE,
) -> None:
    """Read a bounded JSON request on stdin and serve the local BB panel."""
    from software_factory.panel import PanelRequest, panel_request

    request = PanelRequest.model_validate_json(sys.stdin.read(65537))
    result = asyncio.run(panel_request(_workspace(workspace), _control(workspace).context, request))
    print(json.dumps(result))


if __name__ == "__main__":
    main()
