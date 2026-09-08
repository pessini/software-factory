from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated, Literal

import typer
from rich.console import Console
from rich.table import Table

from software_factory.acp_agent import serve_acp
from software_factory.bb import BBContext
from software_factory.config import CONFIG_PATH, default_config, load_config
from software_factory.doctor import run_doctor
from software_factory.engine import FactoryEngine, checkpoint_path
from software_factory.graph import MERMAID
from software_factory.persistence import RunStore

app = typer.Typer(no_args_is_help=True, help="Deterministic BB + LangGraph software factory")
console = Console()
DEFAULT_WORKSPACE = Path.cwd()


def _workspace(value: Path) -> Path:
    return value.expanduser().resolve()


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
    """Validate the local machine, BB context, repository, policy, and persistence."""
    checks = run_doctor(_workspace(workspace), _context_or_none())
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
    """Run the complete workflow locally using the current BB context."""
    engine = FactoryEngine(workspace=_workspace(workspace), context=_required_context())
    result = asyncio.run(engine.run(request))
    console.print_json(json.dumps(result))
    if result.get("status") != "accepted":
        raise typer.Exit(1)


@app.command("resume")
def resume_command(
    run_id: str,
    workspace: Annotated[Path, typer.Option("--workspace", "-w")] = DEFAULT_WORKSPACE,
) -> None:
    """Resume a run from its durable LangGraph checkpoint."""
    engine = FactoryEngine(workspace=_workspace(workspace), context=_required_context())
    result = asyncio.run(engine.resume(run_id))
    console.print_json(json.dumps(result))


@app.command("inspect")
def inspect_command(run_id: str) -> None:
    """Inspect compact run state, child IDs, artifacts, and events."""
    context = _required_context()
    store = RunStore(checkpoint_path(context))

    async def inspect() -> dict[str, object]:
        await store.initialize()
        record = await store.get_run(run_id)
        if record is None:
            raise ValueError(f"unknown run ID: {run_id}")
        return {
            "run": record,
            "children": await store.list_children(run_id),
            "events": await store.events(run_id),
            "artifacts": str(context.thread_storage / "factory" / "artifacts" / run_id),
        }

    console.print_json(json.dumps(asyncio.run(inspect()), default=str))


@app.command("cancel")
def cancel_command(
    run_id: str,
    workspace: Annotated[Path, typer.Option("--workspace", "-w")] = DEFAULT_WORKSPACE,
) -> None:
    """Stop active external work and mark a run cancelled."""
    engine = FactoryEngine(workspace=_workspace(workspace), context=_required_context())
    asyncio.run(engine.cancel_external(run_id))
    console.print(f"cancelled {run_id}")


@app.command("acp")
def acp_command() -> None:
    """Serve the Software Factory agent over ACP stdio."""
    asyncio.run(serve_acp())


def main() -> None:
    app()


if __name__ == "__main__":
    main()
