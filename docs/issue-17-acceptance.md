# Issue #17 acceptance

Acceptance performed on 2026-10-31 for
[Migration 2/4](https://github.com/pessini/software-factory/issues/17).
The comparison starts at `4fbb996d52f89d2b67d614ec792fe7694884ee07`.
[Machine-readable evidence](acceptance/issue-17.json) records source and executable
SHA-256 hashes, scenario outcomes, operation dispatch counts, and actual Docker,
provider-thread and turn identities. Credentials and provider-home contents are
excluded. Reproduction commands and parity boundaries are in the
[checklist](issue-17-parity.md) and [runtime guide](rust-workflow.md).

## Automated verification

| Check | Result |
| --- | --- |
| Rust format, all-target check, Clippy with warnings denied | Passed; Rust/Cargo 1.99.1 |
| Rust credential-free suite | 37 passed; 3 opt-in Docker/provider tests ignored |
| Python Ruff, format, Mypy | Passed; 149 files formatted, 61 source files typechecked |
| Python credential-free suite | 362 passed, 2 failed, 22 skipped on first run; both failures passed on targeted rerun |
| Simulated native provider with real Docker/processes | 13 scenarios passed |
| Pinned native live provider with real Docker/processes | Full workflow recovered after controller kill; cancellation passed |

The two unchanged Python tests were
`test_later_batch_rechecks_unselected_findings_and_preserves_earlier_fix_criteria`
and `test_clean_success_path`. Their durable results recorded
`command time limit exceeded before dispatch` while the acceptance jobs ran
concurrently. Both passed on an isolated rerun in 9.78 seconds without changing
Python code. The first full run is retained as a failed run, not relabeled green.

The public Rust runtime/control and worker execution seams were agreed before
adding tests. Workflow regressions cover complete verification, saved question
answers, repair/revision bounds, focused/risk review, finding history, exact-byte
delivery, selected PR fixes, stale user work, uncertain operations, cancellation,
scheduler exhaustion and ownership. Review corrections include orphan Git-object
recovery, parent ownership during a waiting fix child, automatic authorized
publication, read-only reconciliation after cancellation, process-tree cleanup,
and CI refresh for the latest fix SHA without another push or PR creation.

## Docker and live evidence

The credential-free fixture explicitly simulates the native App Server protocol.
It uses real repositories, containers, broker processes and controller kills.
Its 13 cases are pass, repair, Planner questions, native questions, in-flight
controller death, four durable crash windows, protected-state attacks, publication
cancellation, worker cancellation and timeout. Assertions check stopped containers
with PID zero, original Builder thread reuse, no duplicate completed turn after
recovery, protected mounts, and termination of a blocked publication helper and
its descendant. The before-dispatch crash can record two controller dispatch
attempts because no worker launched on the first attempt; it still creates one
provider turn.

Live acceptance uses Codex **0.154.0**, **gpt-5.6-luna**, **low** effort, and image
`sha256:5f5c3b7fb6c78332c22d2e501b54543410842da511170c149f52e9391c2ab579`.
The full recovered workflow runs baseline, Planner, Builder, quality checks,
Reviewer and finalization in a generated repository. The controller is killed
after the Builder turn ID is durably saved; the replacement controller finishes
that same operation with dispatch count one and produces `ready_for_review`.
The cancellation case stops every recorded worker. All recorded containers are
confirmed stopped before fixture cleanup.

An earlier clean full live workflow also reached `ready_for_review`. An initial
live attempt exposed an inspect/kill race, which was corrected and recovered from
its saved receipt. An initial recovery fixture had an insufficient 60-second
budget; final live acceptance uses a saved 600-second budget. The final-source
recovery and cancellation records are the acceptance evidence.
A final-source replay recovered successfully but its cancellation setup then hit
the Planner's saved 120-second deadline before Builder started. The runtime stopped
that container cleanly. A fresh fixture retried the live checks without source
changes; the timeout attempt remains recorded separately.

PR delivery and selected review-fix behavior are exercised against controlled
remote adapters and real local Git; publication cancellation also uses a real
host process tree. These checks create no remote PRs, comments or pushes.
Named-volume and externally remapped linked-worktree combinations are not claimed
as live-verified. Python remains available for existing recovery; public-interface
migration and production packaging belong to issues #18 and #19.

## Standards

The independent review found three defects: premature parent ownership release,
missing worker cleanup on scheduler exhaustion, and rejecting harmless orphan
objects after interrupted commit preparation. All were corrected with public
regressions. Final re-review has no outstanding findings.

## Spec

The independent review found cancellation/reconciliation, automatic publication,
active publication interruption, and two CI refresh defects. All were corrected
with failing-then-passing regressions. Final re-review has no outstanding findings.

Standards: 0 outstanding findings. Spec: 0 outstanding findings.
