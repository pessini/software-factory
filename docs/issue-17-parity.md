# Issue #17 parity and acceptance

Baseline: `4fbb996d52f89d2b67d614ec792fe7694884ee07` (main after #16), including
approved-plan input from PR #14. This checklist extends the
[current-main contract inventory](rust-contract-inventory.md). Python recovery
paths remain available; #18/#19 still own integration and production cutover.

| Current Python contract | Rust implementation / public behavioral verification |
| --- | --- |
| `graph.py`, `workflow_nodes.py`: bootstrap, baseline, plan/build/test/repair/review/revision/finalization/failure | ADK `factory-sdlc-v1`; `workflow.rs` full run, saved repair/revision bounds, exhaustion, no-change and baseline-failure cases |
| `config.py`, `settings.py`: reviewed argv, bounds, models/efforts, settings/presets and immutable snapshots | Strict Rust configuration, native paginated model catalog, effective choices saved before scheduling; Docker restart receipts keep requested/reported choices |
| `contracts/models.py`, `prompts.py`: validated specialist handoffs and scoped data | Typed Rust handoffs, retained JSON schemas and escaped data blocks; complete criterion/evidence/finding/judge validation and one correction turn |
| `focused_review.py`, `review_policy.py`: single/focused/risk selection and judge | Public workflow tests require each selected role and final judge; risk selection is deterministic, saved and content-bound |
| `evidence.py`, `artifacts.py`, `worktree_policy.py`, `git.py`: protected state, exact content version and current evidence | Controller-owned artifacts/hashes, source/Git/protected/ignored snapshots and instruction hashes; Docker protected-state attack probes and read-only verification checks |
| `planning_control.py`: exact approved-plan input | Input binds session/revision/content hash and retains completion checks; planning interfaces remain #18 |
| `persistence.py`, `runtime.py`, `worker_records.py`: intent, receipts, execution identity and reconciliation | Factory CAS ledger alongside ADK stores; per-operation native broker lock, exact thread/turn IDs, immutable request replay, and subprocess crash windows |
| `worker_manager.py`, `worker_container.py`: protected workers and continuation | Controller-only Docker broker; actual container IDs plus operation/owner/workspace labels; separate protected receipt/continuation metadata; same Builder thread for repair |
| `ownership.py`, `processes.py`: exclusive writers, cancellation, timeout and cleanup | OS lock plus durable workspace claim, independent brokers, deadline/cancel polling, stopped/PID-zero inspection; uncertain cleanup regression retains claim and excludes another run |
| `interactions.py`: durable questions and idempotent/conflicting answers | Public Runtime tests and native-protocol fixture park/restart/answer both Planner and provider questions; human wait pauses work time but not overall expiry |
| `delivery.py`, `delivery_git.py`, `github_delivery.py`: authorized exact-byte commits, pushes/PRs and CI | Controlled GitHub adapter tests exercise real local Git and public delivery; remote operations reconcile before mutation, commit tree verifies exact bytes, CI distinguishes all outcomes |
| `delivery_reviews.py`: selected, verified PR review fixes | Public fix test retains product criteria, adds selected finding criteria, reuses one child and leaves unselected threads open |
| Existing fake Rust slice and Python recovery | Graph versions remain separate; existing Rust/Python suites run alongside the new workflow checks |

The code supports native controllers with Linux/Docker workers. The bundled #16
controller image remains the development fake-slice image; production controller
packaging, public interface parity and deployment are separate milestones.

## Reproducible checks

See the [dated acceptance record](issue-17-acceptance.md) for executed results,
review outcomes and source-bound Docker/live evidence.

```bash
bash scripts/check-rust.sh
bash scripts/check.sh
cargo build --locked -p factory-runtime
docker build -f containers/test-worker.Dockerfile -t factory-test-worker:dev .
python3 scripts/accept-rust-workers.py
cargo test -p factory-runtime --test docker_worker docker_quality -- --ignored
```

The native-protocol fixture is explicitly simulated. It uses real Docker,
independent broker/controller processes, real repositories, no credentials and no
model calls. It covers pass, repair, saved Planner/provider questions, controller
kill during a coding turn, before-dispatch/after-dispatch/after-result/queue-finish
crashes, protected-state attacks, cancellation and timeout. The harness snapshots
the executable before launching subprocesses, saves exact worker IDs and receipts,
and removes only containers named in its private fixture receipts. A controlled
GitHub command also verifies that publication cancellation kills its process tree
and preserves the uncertain remote-operation claim.

## Live checks

With the pinned real worker image and an existing Codex file login:

```bash
cargo test -p factory-runtime --test docker_worker native_codex_protocol -- --ignored
cargo test -p factory-runtime --test docker_worker pinned_real_provider_workflow -- --ignored
python3 scripts/accept-rust-workers.py --live
```

These commands use generated repositories and saved `gpt-5.6-luna` / `low`
choices. The full workflow verifies the changed greeting and produces a local
`ready_for_review` handoff. Live recovery kills the controller after the Builder's
exact turn ID is saved, then reconnects to that same operation; cancellation stops
all recorded containers. They do not publish, merge, or deploy.

The first live run exposed an inspect/kill race and resumed successfully from its
saved receipt after correction. The final evidence record distinguishes that
attempt from clean runs of the independent broker. Acceptance JSON records include
source/binary identities and explicitly state whether the provider was simulated
or live. Named-volume and remapped external-worktree combinations are not claimed
as live-verified; their configuration limits are documented in the runtime guide.
