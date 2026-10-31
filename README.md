# LangGraph Software Factory

A macOS-first controller for a deterministic, resumable software-development
workflow. LangGraph and Python own saved state, protected workers, and quality
checks. The CLI, [BB](https://github.com/get-bb/bb) ACP interface, and local Codex MCP
interface use the same Factory control operations and run IDs.

See [interface setup](docs/factory-interfaces.md) and
[existing-run compatibility](docs/interface-migration.md). The independent native
path passed real CLI and BB workflow tests, including saved questions, reconnect,
and cancellation. See the short [before-and-after explanation](docs/factory-explained.html).
Codex desktop control and BB/Codex cross-interface continuation passed in both
directions using a read-only control workspace. Optional Router support remains
deferred for issue #9.

Earlier live acceptance used the [protected local BB worker](docs/protected-worker.md)
with BB 0.42.1, Apple Container 1.0.0, and the Codex provider CLI 0.154.0.
New runs use Factory-owned workers with the same container protection. See the
[issue 9 verification record](docs/issue-9-acceptance.md) for current evidence.
Other providers and remote execution are not currently supported.

See [run ownership and recovery](docs/run-recovery.md) for cancellation and restart
rules, [live acceptance evidence](docs/live-worker-acceptance.json) for the
verified run, and the [issue completion check](docs/issue-1-completion-check.md)
for implementation and verification status.
The [focused/risk measurement study](docs/review-measurements.md) records eight
passing real-provider cases, earlier failed attempts, and the decision to retain
the starting 10/100/20 limits with risk selection still opt-in.

The [evidence workflow](docs/review-evidence.md) records baseline
checks, structured criterion results and findings, and the exact tested file
version. Successful runs finish at `ready_for_review`. Old `accepted` results
remain available as unverified historical records.
[Phase 1 acceptance passed](docs/acceptance-results.md) with a real provider in issue #6.
The [focused review workflow](docs/focused-review.md) adds Request, Code, and
Security reviews followed by a read-only judge. New configurations select it;
existing configurations and saved runs keep their single-reviewer mode.
The opt-in [risk selection policy](docs/risk-review.md) chooses the required
reviewers from change size and sensitive paths. It adds Documentation Review
when relevant and records review quality and user feedback for threshold tuning.
The opt-in [PR delivery policy](docs/pr-delivery.md) publishes verified changes,
tracks CI, and completes selected review fixes under saved authorization.

**Plan before building.** `software-factory web` serves a local planning UI. A
read-only Grill interviewer settles the decisions with you in adaptive rounds.
Approving the exact plan revision starts one implementation run with that plan as
settled input. BB (`/plan`), Codex (`factory_plan_*` tools), and the CLI control the
same saved sessions. See [planning](docs/planning.md) and the
[issue 10 verification](docs/issue-10-acceptance.md).

The MVP provides:

- an ACP agent registered as **Software Factory** in BB;
- a resumable LangGraph with baseline checks, Planner, Builder, deterministic tests, repair,
  Reviewer, revision, finalization, and terminal failure nodes;
- Factory-owned protected worker processes for every specialist;
- a SQLite LangGraph checkpointer plus a durable spawn-intent ledger and
  exact execution reconciliation that prevent duplicate requests after
  process crashes;
- strict Pydantic handoffs, file and Git change detection, versioned evidence,
  trusted `argv` quality commands, bounded loops, and process-tree cancellation;
- inspect, resume, cancel, repository-check, config-generation, and graph CLI
  operations with versioned run events and atomic artifacts.

See [`docs/macos-runbook.md`](docs/macos-runbook.md) for the macOS setup and the
[`examples/demo-target`](examples/demo-target) fixture for a first run.

The experimental [Rust/ADK durable runtime](docs/rust-runtime.md) retains the
step 1 fake slice (#16) and adds the [implementation workflow and protected Docker
workers](docs/rust-workflow.md) for step 2 (#17). It uses a separate database.
See the [parity checklist](docs/issue-17-parity.md). Python remains supported;
interface integration and production cutover remain #18/#19.

## Development

```bash
bash scripts/check.sh
uv run software-factory graph --format mermaid
```

The same checks run in CI on macOS and Linux with Python 3.14 and a frozen
`uv.lock`. Logs and test results are saved under `.checks/`. Normal checks make
no model requests. The separate [acceptance runbook](docs/acceptance.md) covers
opt-in real-provider runs and the remaining release checks for issue #6.

No command proposed by an agent is executed. Only reviewed `argv` arrays from
`.factory/factory.yaml` are eligible for deterministic execution. Factory agents
cannot use BB's `full` permission mode, and quality commands cannot directly invoke
a shell interpreter or Git. Quality commands remain an operator-controlled trust
boundary and must be verification-only.

## CLI

```text
software-factory acp
software-factory mcp --workspace /path/to/target
software-factory models
software-factory doctor
software-factory graph --format mermaid
software-factory init
software-factory repo-check
software-factory run "request"
software-factory start "request" --request-id <stable-request-id>
software-factory inspect <run-id>
software-factory resume <run-id>
software-factory cancel <run-id>
software-factory review-quality <run-id>
software-factory review-feedback <run-id> <attempt> <reviewer> <finding-id> <verdict> <reason>
software-factory deliver <run-id> [--policy delivery.json]
software-factory review-threads <run-id>
software-factory fix-reviews <run-id> --thread <thread-id>
software-factory web [--port 4317]
software-factory plan "request" [--request-id <id>]
software-factory plan-status [<planning-session-id>]
software-factory plan-answer <planning-session-id> <interaction-id> '{"question-id":"answer"}'
software-factory plan-revise <planning-session-id> "feedback"
software-factory plan-approve <planning-session-id> <plan-sha256>
```

Agents cannot commit, push, or run an LLM-proposed command through the controller.
The controller commits and pushes only under the saved PR delivery policy.
Merge and deployment remain outside the workflow. Unexpected file, index, or
reference changes stop delivery and preserve the workspace for inspection.
Operator-reviewed quality programs are outside that guarantee and must not publish
or mutate repository metadata indirectly.

Model and effort choices for every active step, including the focused reviewers
and judge, are
available through the [BB Factory panel](bb-plugin-factory-controls/README.md).
See [model settings](docs/model-settings.md) for saved settings and presets,
and [current BB verification](docs/issue-9-acceptance.md) for the tested interface.
