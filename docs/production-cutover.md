# Production cutover to Rust (#27)

Use this procedure for an actual, named workspace. The disposable acceptance
helpers never migrate a deployment. Record results in
[issue 27 evidence](issue-27-acceptance.md); keep tokens, provider output,
configuration backups and whole-state snapshots private.

## Inventory and freeze

Record the host, operator UID/GID, canonical checkout, controller and client
hosts, all storage and ownership directories, service managers and launchers.
Inspect each configured storage path rather than inferring an empty inventory
from a missing default directory. Include `FACTORY_HOME`, `FACTORY_OWNERSHIP_DIR`,
`FACTORY_DATA_DIR`, XDG overrides, containers and custom service configuration.
The historical Python default was `~/.local/state/software-factory`; the Rust
compatibility default is `~/.local/share/swe-factory/rust-slice`.

For every legacy run record its ID, workspace, status, pending question/approval,
owned process/container/operation identities, disposition and recovery owner.
Choose finish, cancel with confirmed cleanup, or retain for explicit recovery.
Do not publish raw prompts, logs, checkpoints or credential-bearing configuration.
An empty inventory must state its inspected scope and any unverified locations.
Disposable `.checks` fixtures are historical test state, not production runs;
retain them separately rather than counting them as production adoption.

Freeze new work through old launchers. Let required legacy work finish or cancel
through its original controller, reconcile uncertain operations, stop all clients
and controllers, and verify worker cleanup. Rust and Python use different lock
protocols: the Rust service lock alone cannot exclude a legacy executor. Keep
legacy launchers off the normal PATH and disable any automatic legacy restart.

## Explicit legacy recovery

When a production run needs recovery, retain its original complete checkout,
Python interpreter, dependency lock/environment, provider/tool versions and
images, private storage including checkpoint WAL/SHM, evidence, receipts,
provider homes, ownership directory and original absolute paths. Preserve the
actual original environment before attempting reconstruction. A source commit
and a database alone do not establish recoverability.

The last pre-retirement reference is
`7b1dda21044351ce4c5ca605c85b3b543d94632a`; its package exposes
`software-factory-legacy`, Python 3.14 and `uv.lock`. Use the run's original release
if it differs. In a separate checkout of that reference, `uv sync --frozen`
reconstructs the locked environment, subject to availability of the pinned tools
and packages. Retain the existing environment/artifacts as well. Invoke only its
absolute `.venv/bin/software-factory-legacy` path with explicit original storage
and workspace; unset `FACTORY_CONTROL_URL` so recovery cannot route to Rust.
Inspection uses `--storage /original/data inspect RUN_ID -w /original/target`.
Inspect/cancel/resume must use the original identity; no checkpoint conversion,
claim deletion or speculative redispatch is allowed.

Before recovery that can run workers, freeze Rust clients, drain/cancel and
confirm all Rust workers/brokers stopped, then stop Rust. Start only the isolated
historical environment. After recovery, reconcile its operations and stop it
before restarting Rust. Record both transitions. Do not use legacy `run/start`,
planning, ACP, MCP or web as a route for new product work.

Retirement requires a disposition for every legacy run, confirmed cleanup, no
remaining recovery/retention obligation, verified archives where required, and an
operator decision naming exactly which environments and dependencies may be
deleted. A zero-run inventory can remove this gate for its stated scope; it cannot
certify other hosts or authorize deletion of historical test evidence.

## Release and backup

Immediately before switching, record current GitHub main and candidate SHA,
working-tree status, controller/client binary hashes, controller/worker/broker
image IDs, seccomp hash, tool versions and reviewed policy hash. Compare changed
product files against the accepted release and rerun affected acceptance. Pin
actual image IDs in configuration and retain image archives; a mutable tag is
insufficient. Reuse earlier evidence only for the artifacts and behaviors it
actually exercised. Track #26's current-candidate clean-host quickstart check
separately; an existing WSL development host does not satisfy it.

After freeze and confirmed cleanup, use the
[complete offline backup procedure](production-deployment.md#consistent-backup-and-restore).
Retain private client/service configurations, tokens and immutable binaries/images
alongside the whole data + checkout snapshot. For native execution, use its
documented offline filesystem/cgroup procedure instead of the Docker helper.
Verify integrity and perform a restore drill at the original absolute paths,
with both destination paths absent and all execution stopped. Preserve displaced
directories for investigation. Confirm saved identities, hashes and status after
restore before accepting new work. Never restore stale receipts over live workers.

Rollback order: freeze clients → reconcile/drain workers → stop controller →
preserve failed data and checkout → restore the complete matching snapshot and
configuration → select its retained release → start exactly one controller →
inspect saved status and ownership → reconnect clients. If rollback activates
legacy recovery, keep Rust stopped for the entire recovery window. Never combine
an old database with a newer checkout or reinterpret a LangGraph database as ADK.

## Switch clients and verify production

Start the selected Rust service with the canonical target, separate private Rust
storage and a reviewed real backend. Check health and authenticated `doctor`;
health alone does not prove provider availability. Record each old/new entry point:

| Client | Required connection and check |
| --- | --- |
| CLI | Absolute Rust executable or fixed launcher; `--version` includes Rust/ADK; `doctor/list/status` identify this target; refresh shell PATH cache |
| Codex CLI / ChatGPT desktop | Project-scoped MCP in a trusted control folder, fixed target and private token source; read the same saved session after reconnect |
| BB | Custom ACP provider using the absolute Rust launcher; dedicated project uses the existing checkout, not a new worktree; reload/attach keeps the same Factory run |
| Browser | Authenticated service UI at the selected loopback origin; token exchange produces a clean URL; verify the saved plan/report visually |

Use the [supported startup and client instructions](startup.md#connect-and-submit-work).
Factory MCP is a local stdio adapter; the service HTTP API is not a remote MCP
endpoint. Keep client presentation storage outside the target/controller data.

Complete one useful task on the real target: planning → review the saved exact
revision/hash → explicit approval → implementation → verification/review →
`ready_for_review` report. Retain session/run IDs, approval hash, revision,
model/effort, checks and resulting target diff. Do not substitute a fake worker,
disposable fixture or direct manual edit for this journey. Approval does not
authorize commits, pushes, PR publication or deployment by the worker.

Restart/recreate the production controller with stable paths and immutable images.
Reconnect the relevant clients and confirm the same IDs, pending interaction/hash,
report and event cursor. Cancel a separate bounded run through a client, wait for
terminal cancellation and inspect its worker/broker receipts and containers;
`stopping` alone is not confirmed cleanup. Avoid interrupting an external turn
unless its reconciliation behavior is part of the intended check.

Publish a secret-free result naming workspace, date, release, old/new launchers,
backup/restore results, workflow IDs, client results, inventory/dispositions and
limitations. Use “production cutover complete; legacy recovery retained” only
when cutover passed and legacy recovery remains necessary. Leave incomplete
gates explicit; never infer full LangGraph retirement from code removal.
