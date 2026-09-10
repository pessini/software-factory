# Review of issues 1 through 4

Reviewed on 10 September 2026. This report separates implemented code from
completion evidence. No issue was closed during this review.

| Issue | Result of this review | Work still required |
| --- | --- | --- |
| #1: verification pipeline | The current single-Reviewer workflow does not meet Phase 1. The issue explicitly puts Phase 1 after #2, #3, #4, and #5. | Baseline checks, stable criteria, structured findings, complete evidence coverage, content-bound final review, and the `ready_for_review` result. Phases 2 and 3 also remain; Phase 2 starts after #6 acceptance. |
| #2: repository and evidence protection | Existing protection was reviewed. Added ignored-file change detection. Added an opt-in container worker and local tests that enforce read-only roles, protected paths, and evidence mounts. | Authenticated BB/provider acceptance of the new worker. The default live adapter remains disabled until that acceptance is complete. |
| #3: cancellation and recovery | Fixed missed repair-child cleanup, process identity checks, timeout reporting, ownership release after uncertain cleanup, and interrupted startup claims. Added exact provider-turn records and worker crash cleanup. | Verify the real BB/provider dispatch, restart, waiting, and stop behavior with an authenticated test. Uncertain creation deliberately retains ownership. |
| #4: model and effort choices | Shared and per-step settings, saved choices, model options, BB panel controls, and ACP settings were implemented. Fresh repair and revision prompts now include the original request, saved plan, and current changed files. | Verify the saved choices against an authenticated execution. The new protected worker currently supports local Codex models only. |

The issue numbers do not define implementation order. Issue #1 records this
order: **#2 → #3 → #4 → #5 → #1 Phase 1 → #6 → #7**. Later phases of #1 are
separate from first-release acceptance. Completing all phases of #1 before #5
would change that plan; the requested scope decision is still pending.

See [the protected worker](protected-worker.md) for the concrete implementation,
local checks, supported configuration, and the authenticated test awaiting
authorization.

## Verification recorded for this review

- Full repository suite: 155 passed, 2 optional container checks skipped.
- Final worker checks after the last startup fix: 18 passed, including both real
  unauthenticated container permission checks and the new startup-validation cases.
- Ruff checks and formatting checks passed. Mypy passed for all 33 source files.
- Standards and specification reviews found cancellation and startup-cleanup
  faults in the initial worker implementation. These were fixed and checked again.
- No authenticated model run was used to claim completion.
