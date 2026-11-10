# Native Software Factory product (#25)

`software-factory` is the Rust executable. The CLI, browser, BB ACP and Codex MCP
use the version-1 Rust control API and the same saved runs/planning sessions.
Python, uv and LangGraph are not controller/client dependencies. Target projects
and optional worker toolchains remain free to use them.

## Install, upgrade and executable collisions

Build from a verified checkout with the pinned toolchain in `rust-toolchain.toml`:

```sh
cargo install --path rust/factory-runtime --locked --bin software-factory
software-factory --version
command -v software-factory
```

The version output must include `Rust/ADK`. Cargo refuses to overwrite an existing
executable by default. Before installing, locate old executables with
`type -a software-factory`. The Python package is retired. Recover old runs only with an isolated historical
release and its original data; see [recovery](interface-migration.md).
Remove obsolete Python launchers from the product PATH, refresh the shell command
cache with `hash -r`, then install Rust. Configure BB and Codex with the absolute
native path. Do not overwrite an active recovery environment with `--force`.

Upgrade by stopping/draining the service and backing up the entire private data
directory, including SQLite WAL/SHM, control records, worker receipts and evidence.
Build/install the selected release with the same command plus `--force`, verify
the version and restart with unchanged workspace/data paths and backend. Keep the
previous binary and pre-upgrade snapshot for rollback. Control schema 1 upgrades
transactionally to schema 2 for append-only review feedback; older binaries must
use their matching backup. Never run different releases concurrently over one data
volume. See [deployment backup/rollback](production-deployment.md) for the existing
single-host topology. Production cutover is tracked separately in #27.

Release assets (web HTML, JS, CSS, font/license, policy template and graph) live
under `rust/factory-runtime/assets` and are embedded at compile time. Copying the
installed executable to a compatible OS/architecture needs no Python source tree.
The Docker release includes both `software-factory` and the retained low-level
`factory-runtime` operator executable.

For full native/Compose prerequisites and recipe examples, see [startup](startup.md).

## Start and connect

```sh
software-factory init -w /absolute/target
# Edit .factory/factory.yaml to use reviewed target verification-only argv.
software-factory repo-check -w /absolute/target
software-factory configure-docker /absolute/backend.json \
  --storage /absolute/private/data -w /absolute/target
export FACTORY_HTTP_TOKEN='your-private-token-at-least-24-characters'
software-factory web --storage /absolute/private/data -w /absolute/target
```

The [protected backend guide](rust-workflow.md) documents backend configuration.
Canonical service startup requires a configured Docker or supported Linux native backend. `FACTORY_ALLOW_FAKE_WORKER=1`
is an explicit development-only opt-in for protocol tests, not implementation
acceptance. `FACTORY_DATA_DIR` can replace the service `--storage` argument.
Service storage must be absolute, private, outside the canonical workspace, and
separate from legacy storage. `web` and `serve` both run the independent Rust
service. `--port N` sets a loopback listener; `FACTORY_HTTP_BIND` configures a full
bind address when `--port` is absent.

Clients use `FACTORY_HTTP_TOKEN` and `FACTORY_CONTROL_URL`. An unset URL means
`http://127.0.0.1:4317`, always Rust. There is no automatic daemon launch, token-file
discovery or legacy fallback. Start the service explicitly and configure the
same token in each client. Without a configured token, loopback service startup
prints an ephemeral browser link; stdio/CLI clients still need an operator-set
token. An operator-configured token is never printed. For a browser, open the
service URL with `?token=YOUR_TOKEN`; exchange removes it from the URL and sets an
HttpOnly cookie. Do not share token-bearing links.

Explicit origins must have no path/query/credentials. Non-loopback connections
require HTTPS; redirects are never followed with credentials. Public services
retain required host/origin/caller configuration from the
[shared interface security contract](rust-interfaces.md). Every client operation
checks the service's canonical workspace before sending a mutation. Paths in
client registrations convey no worker authorization.

## CLI inventory and compatibility decisions

Options `-w/--workspace` and `--session` select the fixed target and client settings
session. Client commands return saved JSON; start generates a request ID unless
`--request-id` supplies one. Reuse IDs only with identical inputs. Start/resume
return before workflow completion; poll `inspect` for the outcome. A JSON result
is successful transport even if a saved run is failed/blocked/cancelled; inspect
its `status` (intentional exit-code consistency across CLI/MCP/ACP).

| Command | Native behavior / deliberate compatibility change |
| --- | --- |
| `doctor` | Checks authenticated service/workspace, local policy and worker catalog. Controller backend diagnostics remain service/operator responsibilities; no Python/BB interpreter probing. |
| `init [--force]` | Writes embedded version-2 focused-review policy. Default target quality argv is `cargo test --locked`; operators must review/change it for the target. Refuses existing policy without force and refuses symlink paths. |
| `repo-check` | Validates policy using Rust's execution validator; does not run quality commands. |
| `convert-config` | Prints a version-2 copy with pinned native worker connection, clearing stale resolved settings; leaves policy/saved runs unchanged. |
| `graph [--format mermaid]` | Prints the embedded Rust SDLC topology (including waits and bounded repair/review routes). |
| `run REQUEST`, `start REQUEST [--request-id ID]` | Saves implementation work through the Rust service. |
| `inspect/status RUN_ID`, `list` | Reads saved state; never dispatches/resumes. |
| `answer RUN_ID INTERACTION_ID ANSWERS_JSON` | Saves answers to the exact interaction. |
| `resume/retry RUN_ID`, `cancel RUN_ID` | Requests durable reconciliation or cancellation; cleanup stays service-owned. |
| `models`, `settings` | Reads catalog or settings. Saved run snapshots remain immutable. |
| `review-quality RUN_ID` | Returns integrity-checked native review evidence by artifact path plus append-only feedback; historical Python metric/round layout is intentionally not reproduced. |
| `review-feedback RUN_ID ATTEMPT REVIEWER FINDING_ID VERDICT REASON` | Appends `useful`/`false_positive` feedback only for a finding in that saved reviewer/attempt. Does not alter review approval or publication authority. |
| `deliver RUN_ID [--policy FILE]` | Explicitly authorizes/refreshes PR delivery through the service's existing policy/evidence checks. Policy JSON is reviewed operator input. |
| `review-threads RUN_ID` | Reads threads for the saved authorized PR; no arbitrary repository/PR arguments. |
| `fix-reviews RUN_ID --thread ID [--thread ID ...]` | Uses saved publication/fix authorization and exact selected current threads. |
| `plan REQUEST [--request-id ID]` | Starts a planning session with selected client settings. |
| `plan-status [SESSION_ID]` | Reads one revision or lists sessions. |
| `plan-answer SESSION_ID INTERACTION_ID ANSWERS_JSON` | Answers the exact pending planning round. |
| `plan-revise SESSION_ID FEEDBACK` | Saves a distinct immutable revision. |
| `plan-approve SESSION_ID SHA256` | Approves the exact current hash; repeated approval returns the same implementation identity. |
| `plan-resume/plan-cancel/plan-report SESSION_ID` | Reconciles, cancels, or reads the verified implementation report. |
| `web/serve`, `configure-docker FILE`, `import-planning FILE` | Explicit local service/operator commands requiring `--storage` or `FACTORY_DATA_DIR`. Import only uses the existing validated export format, never checkpoint decoding. |
| `acp`, `mcp` | Native newline-delimited JSON-RPC stdio adapters. Stdout contains protocol frames only. |
| `settings-panel` | Bounded stdin JSON bridge used by the BB plugin, with the existing `sessionId/configOptions` response. |
| `export-planning`, `legacy-inspect/resume/cancel`, hidden `worker-acp` | Deliberately excluded from the native runtime; errors direct users to a historical release. Protected Rust workers use their native backend/broker. |

Service/worker errors return nonzero. Run outcomes are data, not transport errors.
`--storage` is rejected for client commands: they connect to the service instead
of opening any local workflow/checkpoint database. The retained low-level
`factory-runtime DATA WORKSPACE ...` syntax remains available for existing
operator/deployment tooling and operates only on Rust state.

Delivery/fix requests execute in service-owned tasks if the adapter disconnects.
Their saved runtime intents and worker/remote receipts retain existing recovery
semantics. A timeout can leave an operation in progress; inspect its saved run
before retrying. Only explicit cancellation requests cleanup.

## BB ACP, settings and Codex MCP

Register BB's agent command as the absolute native executable, with arguments
`["acp", "--workspace", "/absolute/target"]`. Point Factory Controls at that same
executable. Configure token/origin in the BB server environment. Existing slash
commands and the settings-panel stdin contract are retained; plain text starts
work only when no run is attached. Use `/new REQUEST` for a new run after one is
attached. ACP cancellation is presentation-only; `/cancel` stops the saved run.

Native chat state is a separately versioned, private SQLite presentation ledger
under `FACTORY_CLIENT_HOME` or `~/.local/state/software-factory/clients`.
BB thread references map deterministically to one client settings/chat ID per
origin/workspace. The panel and ACP must share that directory and service origin.
Loading a chat replays messages and reads settings; it never resumes a terminal
run. Python chat ledgers are not imported; start a native chat and `/attach` a saved
Rust run. Run IDs, planning IDs, hashes and service-side authorization survive.

Register Codex MCP with `/absolute/bin/software-factory mcp -w /absolute/target`.
Set the same token/origin in its environment. `--session ID` selects an independent
settings context. All 14 existing `factory_*` tool names/arguments remain; tools
never accept workspace paths, configurations or worker permissions. Plan approval
requires explicit user approval and the exact returned hash.

The optional `scripts/codex-control-workspace.sh` now discovers the native binary
(or accepts `--executable`), verifies its Rust version and passes the three client
environment variables to MCP. It does not install/start Python or a controller.

## Legacy recovery

Native clients reject known Python run/checkpoint identities with an actionable
error. They never read a LangGraph ledger. Keep old runs under their original
Python environment/context/storage in an isolated historical release; see
[isolated recovery](interface-migration.md). An explicit validated terminal/waiting
planning export may be imported through the existing operator import. Active
checkpoint ownership is never transferred. [Startup](startup.md) describes deployment
UX; #27 tracks actual production cutover. Historical release evidence does not
automatically certify a changed source/image or legacy retirement.
