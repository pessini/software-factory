# Review of issues 1 through 4

Updated on 10 September 2026 after the user-authorized live tests. The code for
#2–#4 has passed local and authenticated worker verification. No GitHub issue
was closed during this review.

Update on 11 September: issue #5 was implemented in `de2aeba` and issue #1 Phase 1
in `24628bf`. The table below preserves the earlier review result. Those code
dependencies are ready for [issue #6 acceptance](acceptance.md); later phases of
issue #1 are not prerequisites. GitHub issues #2–#5 still show open, so their
tracker state alone is not evidence that their implementation is missing.

| Issue | Current result | Remaining boundary |
| --- | --- | --- |
| #1: verification pipeline | Reviewed; Phase 1 and the later phases remain incomplete. | The issue puts Phase 1 after #2, #3, #4, and #5. It still requires baseline checks, stable criteria, structured findings, evidence coverage, content-bound review, and `ready_for_review`. Later phases follow #6. |
| #2: repository and evidence protection | Verified existing-file protection, read-only roles, evidence mounts, ignored-file preservation, and a valid real implementation. Provider placeholder creation was found and fixed. | New protected-path creation is detected and rejected after an operation. Other providers and remote environments remain unsupported. |
| #3: cancellation and recovery | Verified exact turn matching, a real controller crash and recovery with one dispatch, cancellation, and confirmed container exit. Fixed BB state handling and parent-notification interruption. | Uncertain dispatch still retains ownership and is never repeated. |
| #4: model and effort choices | Shared and per-step settings, saved choices, panel controls, and ACP settings pass tests. The live provider reported the selected model and effort. Same-Builder follow-up retained its conversation and used a distinct turn. | The protected worker supports local Codex models. Unsupported choices fail before dispatch. |

The planned order is **#2 → #3 → #4 → #5 → #1 Phase 1 → #6 → #7**.
Issue #5 can now use the verified worker and settings controls. Completing all
phases of #1 before #5 would change the issue's documented plan.

## Verification

- The regression suite passed: 170 tests passed and two optional container tests
  were skipped. Both container tests passed when run separately with Apple Container.
- Ruff checks, formatting checks, and type checks passed. The final `doctor`
  check passed with the protected worker enabled by default.
- The real demo run `fct_9c5911f51ff1` completed planning, implementation,
  configured tests, and review with status `accepted`.
- Plan and Build used `gpt-5.6-luna / low`; Review used `gpt-5.6-luna / medium`.
  The provider responses matched the saved choices.
- A separate controller was killed after dispatch. Restart recovered the same
  provider turn with one dispatch; cancellation stopped all owned work.
- Build and Repair reused one child and one provider conversation. Their turn
  IDs differed, the second turn recalled the first turn's marker, and no child
  notice interrupted the parent before cancellation.
- Local container checks cover writes, deletion, rename, permission changes,
  hard links, symbolic-link writes, and missing protected-path startup behavior.
- Standards and specification reviews found the startup and handoff faults;
  the fixes have regression coverage and live evidence.

See [saved live evidence](live-worker-acceptance.json) and
[worker setup and boundaries](protected-worker.md).
