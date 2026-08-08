# BB LangGraph Software Factory

A macOS-first MVP that exposes a deterministic, resumable software-development
workflow to [BB](https://github.com/get-bb/bb). LangGraph owns routing and durable
state; BB owns visible Planner, Builder, and Reviewer threads; Python owns all
quality gates.

The MVP provides:

- an ACP agent registered as **Software Factory** in BB;
- a resumable LangGraph with Planner, Builder, deterministic tests, repair,
  Reviewer, revision, finalization, and terminal failure nodes;
- visible, reusable BB child threads for every specialist;
- a SQLite LangGraph checkpointer plus a durable spawn-intent ledger and
  deterministic BB-thread reconciliation that prevent duplicate children after
  process crashes;
- strict Pydantic handoffs, protected-path enforcement, read-only-role rollback,
  trusted `argv` quality commands, bounded loops, and process-tree cancellation;
- inspect, resume, cancel, repository-check, config-generation, and graph CLI
  operations with versioned run events and atomic artifacts.

See [`docs/macos-runbook.md`](docs/macos-runbook.md) for the macOS setup and the
[`examples/demo-target`](examples/demo-target) fixture for a first run.

## Development

```bash
uv sync
uv run ruff check .
uv run mypy src
uv run pytest
uv run software-factory graph --format mermaid
```

No command proposed by an agent is executed. Only reviewed `argv` arrays from
`.factory/factory.yaml` are eligible for deterministic execution. Factory agents
cannot use BB's `full` permission mode, and quality commands cannot directly invoke
a shell interpreter or Git. Quality commands remain an operator-controlled trust
boundary and must be verification-only.

## CLI

```text
software-factory acp
software-factory doctor
software-factory graph --format mermaid
software-factory init
software-factory repo-check
software-factory run "request"
software-factory inspect <run-id>
software-factory resume <run-id>
software-factory cancel <run-id>
```

The workflow never commits, pushes, merges, deploys, or runs an LLM-proposed
command. It restores agent-created Git commits and ref changes before continuing.
Operator-reviewed quality programs are outside that guarantee and must not publish
or mutate repository metadata indirectly.
