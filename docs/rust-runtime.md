# Experimental Rust durable slice (#16)

This page documents migration step 1, not a production replacement. Step 2 adds
[the implementation workflow and protected Docker brokers](rust-workflow.md),
with its own graph version and [parity checklist](issue-17-parity.md). The Python controller,
planning UI, BB/ACP and Codex/MCP interfaces remain available. See the
[contract inventory](rust-contract-inventory.md) before extending the port.

## Dependency and scheduling decision

Upstream ADK head at implementation time is
`bd1bd5f450a6489d08e14220809033401282ed67` (3.0.0 source metadata).
Cargo registry inspection found `adk-graph` 2.3.0 and rejected
`adk-session@3.0.0` as unpublished. All ADK crates therefore use one immutable Git
revision, including their internal path dependencies. Cargo.lock records the
latest compatible dependency resolution. Local tools were installed using
Homebrew: Rust/Cargo 1.99.1 and Docker CLI 29.9.2. The existing host Docker daemon
is 29.1.3. `rust-toolchain.toml` and the development container pin Rust 1.99.1.
Python dependencies remain unchanged in this migration milestone.

`BackgroundState` accepts `SqliteRunStore`, and `DurableWorker` provides leased
claims, recovery, cancellation and workflow `execute_durable`. At the selected
revision, `adk-server/src/background/worker.rs::run_workflow` maps every successful
custom workflow return to `Attempt::Done` and errors to retryable failure. Its
approval parking handles agent held-tool calls; a custom graph interrupt is
neither of those outcomes. Using it unchanged would finish or retry a graph that
should wait for a person.

The slice uses ADK's file-backed `SqliteRunStore` with a small Factory supervisor
that explicitly maps graph interrupts to `wait`, saved answers to `wake`, and
results to `finish`. The graph adapter overrides `WorkflowExecutor::execute_durable`,
uses the saved Factory checkpoint thread ID, and observes the supplied cancellation
token. Ordinary `execute` rejects calls without durable context. The supervisor
records checkpoint references on the claimed run with its lease epoch. This is an
application integration around the ADK queue. No ADK REST routes, in-memory stores,
or `GraphAgent` are used in deployment.

## Persistence and identity

One service, one local host, one configured workspace. Mount a private local data
volume separately from the workspace. No Docker socket or credentials are needed.

```text
/data/factory-rust.sqlite                 # Factory domain + ADK stores
/data/worker-records/fake-provider.sqlite  # independent external receipts
/data/artifacts/<run-id>.json             # final fake result
/workspace/.factory-rust.lock             # cross-process exclusive ownership
```

SQLite uses WAL, synchronous FULL, foreign keys, a five-second busy timeout and
bounded pools. Persist the directory, including WAL/SHM files. Use SQLite backup
or a quiesced snapshot, including receipts/artifacts. Network filesystems and
multi-host ownership are unsupported. Never remove or replace a live lock file.

Request/session IDs are bounded ASCII identifiers. Run identity derives from the
start request ID; workspace identity derives from its canonical absolute path.
Keep workspace mount paths stable across recreation. Graph threads are
`factory:<run-id>` and survive retries. Inputs, limits, wall-clock deadlines,
answers, event sequences and operation identities are saved. Identical repeated
starts/answers return current saved state; conflicting reused identities fail.
Terminal runs return saved results without invoking ADK on subsequent turns.

Factory SQL migration and state/graph envelope versions are separate from ADK
store migrations. Unknown versions reject recovery with an actionable error.
There is no automatic Python checkpoint conversion. ADK and LangGraph databases
must remain separate.

The OS workspace lock fences graph execution/dispatch across processes, including
controllers with different data directories. ADK uses a two-second renewable
lease; 250ms heartbeat checks renew it and propagate cancellation. Waiting
releases the lock and lease and holds no client connection. Cancellation is saved
before signalling active execution. The service polls saved runs independently
of command clients. `status` is read-only.

Every operation saves intent before dispatch. Recovery asks the worker to
reconcile the immutable key: completed receipts are reused, proven absence permits
dispatch within the saved budget, and uncertainty requires recovery. The fake
provider has its own transaction and unique receipt key. Shared SQLite does not
make provider side effects atomic with checkpoints. This proves one logical fake
operation with an idempotent/reconcilable provider, not universal exactly-once
execution for real providers. #17 must implement their reconciliation protocols.

The saved timeout includes human waiting. Service polling expires unattended runs.
Dispatch budgets and ADK recovery attempts are distinct, both bounded and durable.
A fake result is `ready_for_review`, not production acceptance or permission to
publish. Protected workers, provider acceptance, full workflow/interface parity,
production packaging, Router and Wayfinder belong to later milestones.

## Development

Install tools through Homebrew:

```bash
brew install rust docker
# If no Docker daemon is running (for example on macOS):
brew install colima
colima start
```

```bash
bash scripts/check-rust.sh
cargo run -p factory-runtime -- /tmp/factory-data /absolute/workspace serve
# In another terminal; all arguments after the workspace are control commands:
cargo run -p factory-runtime -- /tmp/factory-data /absolute/workspace start \
  '{"request_id":"demo","session_id":"demo","request":"Build a greeting","timeout_seconds":3600,"max_dispatches":3}'
# Read the run ID and saved interaction ID from status, then answer and resume:
cargo run -p factory-runtime -- /tmp/factory-data /absolute/workspace status RUN_ID
cargo run -p factory-runtime -- /tmp/factory-data /absolute/workspace answer RUN_ID INTERACTION_ID true
cargo run -p factory-runtime -- /tmp/factory-data /absolute/workspace resume RUN_ID
```

`serve` is an independent polling process; commands can also run without it and
explicitly `resume`. `recovery_needed` requires explicit resume, so uncertain
operations do not automatically spin through their recovery budget. The current
slice has one approval and one fake operation. No worker touches repository files.
The only workspace control file is `.factory-rust.lock`, which must remain in place.

```bash
docker build -f containers/rust-runtime.Dockerfile -t factory-rust-slice:dev .
python3 scripts/accept-rust-container.py --image factory-rust-slice:dev
# For an interactive development service:
docker run --rm --name factory-slice \
  -v factory-slice-data:/data -v factory-slice-workspace:/workspace factory-rust-slice:dev
# Another terminal:
docker exec factory-slice factory-runtime /data /workspace list
```

The image runs as UID/GID 10001 with no host socket and no network listener. The
service uses only the mounted data/workspace volumes. This is development
packaging; authentication, production health/lifecycle endpoints, backups and
protected Docker workers remain later work. CI runs formatting, typechecking,
Clippy and Rust tests using Homebrew on macOS, and builds/tests the actual
persistent-volume container on Linux. A newer Homebrew toolchain requires updating
the tested pin; the version assertion prevents silently using a different compiler.

The acceptance harness creates uniquely named containers/volumes, kills and
recreates the service while waiting, submits duplicate/conflicting commands,
checks terminal replay, and crashes command containers at four persistence
boundaries. It writes JSON evidence and removes only its own resources.
`FACTORY_SLICE_CRASH_AT` is an opt-in acceptance hook, absent in normal execution;
its values are `before_dispatch`, `after_dispatch`, `after_result`, and
`after_queue_finish` (exit code 86 without destructors).

See the [issue #16 acceptance record](issue-16-acceptance.md),
[automated checks](issue-16-checks.json), and
[container evidence](issue-16-container-evidence.json) for verified results.
