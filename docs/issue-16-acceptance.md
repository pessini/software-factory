# Issue #16 acceptance

Migration step 1, based on main `1750eb3b9aeb1fec573c2fb5e5b6d5498a1dc78f`.
The [contract inventory](rust-contract-inventory.md) includes the shipped #9/#10
baseline. The [runtime guide](rust-runtime.md) explains native execution,
file-backed stores, identity, ownership, scheduling decisions and limitations.
Python remains the production controller.

## Automated checks

- `bash scripts/check-rust.sh`: formatting, all-target typechecking, Clippy with
  warnings denied, and 14 integration tests pass (5 persistence/control, 7
  execution/cancellation/deadline, 2 subprocess crash-recovery tests).
- `bash scripts/check.sh`: Python lint, formatting and strict typechecking pass;
  **364 tests passed, 22 opt-in live tests skipped**. The sandboxed attempt stalled
  on asynchronous SQLite wakeups; a minimal reproducer also stalled. The same
  three process-recovery cases passed outside the sandbox, then the entire suite
  passed outside it. No Python implementation or lockfile was changed.
- `python3 scripts/accept-rust-container.py`: **6 scenarios passed** on the final
  image. The [container evidence](issue-16-container-evidence.json) includes the
  tested image identity and saved run/operation/interaction snapshots. The
  [check record](issue-16-checks.json) binds results to source hashes.

| Release gate | Evidence |
| --- | --- |
| Kill/recreate while waiting | Same run, interaction, deadline and graph thread survive on named volumes |
| Duplicate/conflicting input | Identical starts/answers return saved state; conflicting reuse is rejected |
| Dispatch/persistence crash windows | Before dispatch, after dispatch, after result and after queue completion all reconcile |
| Exclusive workspace ownership / terminal guard | Concurrent active container resumes dispatch once; completed runs return saved results |
| Retry/deadline/cancellation persistence | Native tests cover saved budgets, restart, live cancellation and finalization races |
| Checkpoint incompatibility | A future graph checkpoint is rejected before execution |
| Native / container execution | Compiled process tests and the independent container service both pass |

The tests exercise public control and injected execution boundaries, plus compiled
native processes and the container service. No model/provider credentials or live
provider calls are involved. The tests first exposed missing checkpoint validation,
a finalization recovery gap, missing artifact repair, cancellation/deadline handling,
and both review findings below; each was corrected and verified.

## Standards

The parallel Standards review found two correctness problems: unattended
`recovery_needed` runs skipped deadline expiry, and cancellation saved during
finalization could be overwritten by completion. It also identified duplicated
live/recovery completion translation as a judgement-call smell. Both correctness
findings are covered by regression tests; terminal status now checks cancellation
inside the compare-and-swap mutation, service polling honors the saved deadline,
and a shared finalizer handles live/recovered results. The reviewer verified the
corrections and reported **zero outstanding findings**.

## Spec

The independent Spec review found the same two problems against issue #16's
persisted deadlines and cancellation requirements. It verified both fixes and
reported **zero outstanding findings** and no scope creep. The final timing checks
use bounded polling for actual lease expiry rather than fixed sleeps. The graph
rechecks wall time after a monotonic timer wakes before declaring its persisted
deadline expired.

## Limits

This proves the deterministic fake-worker slice, not full workflow parity or a
production release. Native controller execution is supported; the current `serve`
command runs the durable supervisor without an HTTP listener. The existing browser
UI/API port and a polished native server command remain #18/#19 work. Isolated
coding workers remain #17 work, independently of whether the controller runs
natively or in Docker.

The fake provider's independent SQLite receipt transaction supports idempotent
operation keys and reconciliation; it does not prove exactly-once side effects for
arbitrary providers. Uncertain operations need explicit recovery. Deadlines use
host wall time, while bounded test waits use monotonic time. Local filesystem
locking and a stable workspace mount path are required. Multi-host storage,
network filesystems, backup/restore automation, live providers, production
health/authentication/packaging, Router and Wayfinder are outside this milestone.
