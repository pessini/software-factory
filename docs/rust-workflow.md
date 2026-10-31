# Rust implementation workflow and Docker workers (#17)

Migration step 2 extends the separate Rust database from [step 1](rust-runtime.md).
Python remains supported. Interface integration is #18; production packaging and
cutover are #19. Router and Wayfinder remain separate.

## Execution and recovery

New implementation requests save `factory-sdlc-v1`, the reviewed configuration,
resolved model catalog/settings, required criteria, and optional exact approved
plan content/hash. Existing `factory-fake-v1` runs retain their graph and behavior.
Both use file-backed ADK Graph checkpoints, sessions, the ADK durable queue and the
Factory compare-and-swap ledger. Neither reads Python checkpoints.

The graph runs bootstrap, baseline, validated planning, build, tests, bounded
repair, single/focused/risk review, bounded revision, and finalization/failure.
Every repair/revision returns to checks. Baseline failures cannot be waived;
changed failure output alone does not establish a new regression. A verified
no-change result is valid. Success means `ready_for_review`, not human approval.
The saved plan includes every required/approved-plan criterion with its original
ID and description. Approval content/hash mismatches are rejected before start.

Factory phase state and immutable operation inputs/results are authoritative when
an ADK checkpoint lags. Replayed specialists use the original prompt and guard,
not a prompt reconstructed from already edited files. Intent is saved before any
worker launch. Dispatch counts, wall deadlines, chosen models, provider thread
IDs, turn IDs and receipts survive restart. The provider receives the immutable
operation key as `clientUserMessageId`; output must match that exact thread/turn.
There is no model fallback or checkpoint-based exactly-once assumption.

A controller-owned broker process holds each native App Server connection. Its
file lock fences duplicate broker launches. Killing a control command leaves the
broker working; a reconnect reads its receipt rather than creating another turn.
Provider questions are saved before parking the graph. A broker can wait for the
saved answer without keeping a user client connected. Planner output questions
also park durably. Answer IDs and values are checked, including duplicate/conflicting
answers. Human waiting pauses work budgets but not the saved overall expiry.

The workspace has an OS execution lock plus a persistent run claim. The claim
survives controller death and human waiting. Review-fix children inherit the parent
claim. Cancellation is saved before stopping workers. Brokers observe it and the
saved deadlines. Cleanup uses actual Docker IDs, owner/workspace/operation labels,
and a final stopped/PID-zero inspection, never a PID from another namespace.
Uncertain create/inspect/cleanup results remain `recovery_needed` with the claim
intact. Do not delete live lock, claim, identity, receipt, or provider-home files.

## Isolation and mounts

Only the controller/broker can access Docker. Workers receive no Docker socket,
controller database, receipts, or publication credentials. They run as a non-root
UID/GID, with a private PID namespace, dropped capabilities, no-new-privileges,
a read-only root filesystem, bounded resources and writable temporary storage.
Read-only specialists and verification commands have a read-only workspace.
Builder gets the application workspace with Git metadata, policy, project
instructions, ignored paths and controller ownership files overmounted read-only.
All specialists receive controller evidence read-only. Authoritative receipt and
continuation identities stay outside worker-writable mounts.

Native Codex App Server **0.154.0** is retained. Its filesystem/network profile
restricts model tools further. Docker's default seccomp profile is retained with
the namespace/mount calls needed by rootless bubblewrap; see the adjacent NOTICE
and Apache license. The broker refuses provider model, effort or approval-policy
substitution and denies every approval/tool request except saved user questions.
`never` is the default approval policy. An enterprise account requiring
`on-request` can explicitly select it in the saved backend configuration; the
broker still denies approval requests. This does not change account requirements.

Configure daemon-visible bind sources explicitly when controller paths differ
from daemon paths. A controller-created identity marker verifies the actual
workspace before a coding turn or quality program starts. Optional named workspace
and data volumes use subpath mounts: workers receive only their own evidence and
provider-home subdirectories, never the controller storage root. Linked-worktree
Git directories outside a remapped/named workspace are rejected; same-path native
binds support external Git metadata. Mount sources must already exist in the
selected daemon. Keep mount paths stable across restart.

Use a Linux-compatible worker image and reviewed Linux-compatible quality argv.
Host virtual environments are not rebuilt, mutated, or silently replaced. Quality
commands are verification-only, have explicit timeouts, cannot directly invoke
Git or a shell, and can repeat after proven absence only when
`repeat_on_recovery` was saved. Quality containers have no network access.

The controller hashes source, ignored/protected files, Git state, scoped
AGENTS.md/CLAUDE.md sources, reviewed inputs and artifacts. Checks and reviews bind
to the exact content version. Criterion coverage, evidence references, finding
history and every judge assessment are validated. Tampering, stale evidence or
incorrect Builder file reports blocks acceptance and preserves the workspace.

## Native setup

Build with `cargo build --locked -p factory-runtime`. Install Docker and build the
existing `containers/worker.Dockerfile` as `software-factory-worker:0.154.0`.
Prepare an absolute backend JSON outside the target repository:

```json
{
  "executable": "docker",
  "image": "software-factory-worker:0.154.0",
  "daemon_workspace": "/absolute/target",
  "daemon_data_dir": "/absolute/factory-data",
  "workspace_volume": null,
  "data_volume": null,
  "daemon_auth_file": "/home/operator/.codex/auth.json",
  "uid": 1000,
  "gid": 1000,
  "seccomp_profile": "/absolute/software-factory/containers/worker-seccomp.json",
  "approval_policy": "never"
}
```

Use the actual non-root workspace UID/GID. `configure-docker` resolves the image to
its immutable ID and saves the profile hash and backend topology. A later command
cannot silently replace that backend. Existing file login is mounted only into
the native provider home, read-only, and denied to model tools.

```bash
target/debug/factory-runtime /absolute/factory-data /absolute/target configure-docker /absolute/docker.json
target/debug/factory-runtime /absolute/factory-data /absolute/target implement \
  '{"request_id":"work-17","session_id":"session-17","request":"Implement the request","timeout_seconds":3600,"max_dispatches":3}' \
  /absolute/target/.factory/factory.yaml
target/debug/factory-runtime /absolute/factory-data /absolute/target serve
```

`start` also accepts the full `StartRequest` JSON with `implementation.config`,
`implementation.required_criteria`, and optional `implementation.approved_plan`.
`status`, `list`, `resume`, and `cancel` work independently of `serve`.
`answer-questions RUN INTERACTION '{"question-id":"answer"}'` saves typed answers.
The default/fake slice still uses `answer RUN INTERACTION true/false`.

`/data/evidence/<run-id>/` contains immutable artifacts and a manifest. Hashes in
SQLite are authoritative. Final run JSON, including the handoff, is materialized
under `/data/artifacts/`. Persist the whole private data directory, including WAL,
receipts, provider homes, broker logs and evidence. The current controller remains
single-host; authentication, lifecycle/health endpoints and backup automation are
production packaging work, not implicit guarantees of this native CLI.

## Authorized delivery

The saved `delivery.mode: pull_request` policy automatically publishes a verified
successful run. `resume` and `serve` continue pending delivery and selected fix
batches under that same authorization; background delivery polls are bounded to
one pass per five seconds. Manual runs publish nothing. `deliver RUN [POLICY_JSON]` can save later explicit
publication authorization for a verified result. A saved destination cannot change.
Only adding `review_fixes: true` to the same policy is supported.
Explicit `resume` also refreshes failed or unavailable CI after checks change.
After review fixes, refresh validates the latest verified commit and existing PR
before reading CI, without repeating publication operations.

Delivery builds a separate temporary index, hashes exact verified bytes without
filters/hooks, validates the entire tree, saves the commit identity, and installs
only expected ref/index states. It never rewrites source files. Remote pushes use
an explicit target and expected prior head. PRs are regular, carry an operation
marker and linked issue closure, and never merge or deploy. CI checks/statuses are
paginated and tied to the exact commit; pending/failed/unavailable are distinct.

`fix-reviews RUN THREAD_ID ...` saves selected current discussions, creates one
ordinary implementation child with all original criteria plus those findings,
retests/reviews it, and publishes verified fixes to the existing PR. Replies,
resolutions and summary comments reconcile saved markers and the exact discussion
and PR head before mutation. Unselected threads remain open. `cancel-delivery RUN`
stops further mutations; uncertain remote operations retain recovery and ownership.
Repeat `deliver` or `fix-reviews` to reconcile an interrupted delivery.

See the [parity/acceptance checklist](issue-17-parity.md) for automated and live
coverage. Simulated protocol tests and real-provider evidence are labeled separately.
