# BB LangGraph Software Factory

A macOS-first MVP that exposes a deterministic, resumable software-development
workflow to [BB](https://github.com/get-bb/bb). LangGraph owns routing and durable
state; BB owns visible Planner, Builder, and Reviewer threads; Python owns all
quality gates.

Live runs use the [protected local BB worker](docs/protected-worker.md), tested
with BB 0.42.1, Apple Container 1.0.0, and the Codex provider CLI 0.154.0.
The worker enforces read-only roles and protects controller storage. Other
providers and remote BB environments are not currently supported.

See [run ownership and recovery](docs/run-recovery.md) for cancellation and restart
rules, [live acceptance evidence](docs/live-worker-acceptance.json) for the
verified run, and the [issues 1–4 review](docs/issues-1-4-review.md) for the
remaining dependency order.

The MVP provides:

- an ACP agent registered as **Software Factory** in BB;
- a resumable LangGraph with Planner, Builder, deterministic tests, repair,
  Reviewer, revision, finalization, and terminal failure nodes;
- visible, reusable BB child threads for every specialist;
- a SQLite LangGraph checkpointer plus a durable spawn-intent ledger and
  deterministic BB-thread reconciliation that prevent duplicate children after
  process crashes;
- strict Pydantic handoffs, file and Git change detection, versioned evidence,
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
command. It stops on unexpected file, index, or reference changes and preserves the workspace for inspection.
Operator-reviewed quality programs are outside that guarantee and must not publish
or mutate repository metadata indirectly.

Model and effort choices for Plan, Build, Repair, Review, and Revision are
available through the [BB Factory panel](bb-plugin-factory-controls/README.md).
See [model settings](docs/model-settings.md) for saved settings and presets,
and [BB compatibility](docs/bb-settings-compatibility.md) for the tested interface
and the remaining live-execution limit.
