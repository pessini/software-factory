# Issue #1 and the final planned issue

Checked on 14 September 2026 against the GitHub issues and local source at
`9a6abae`, before implementing issue #7.

**Issue #7 is the final feature issue in the consolidated seven-issue sequence.**
It is not proof that every release experiment is complete.

| Scope | Evidence and remaining work |
| --- | --- |
| Issues #2–#6 | All five issues are closed on GitHub. Phase 1 local and hosted acceptance is recorded in `acceptance-results.md`. |
| #1 Phase 1 | Its checklist is complete. The accepted first release uses one Reviewer. |
| #1 Phase 2 | Implemented in `b279af0`: Request, Code, Security, and Judge roles, separate evidence, shared settings/recovery, and workflow tests. See `focused-review.md`. GitHub's checklist still shows unchecked boxes. |
| #1 Phase 3 | Implemented in `9a6abae`: configured risk boundaries, sensitive-path protection, relevant Documentation Review, generated-file handling, and saved review-quality feedback. See `risk-review.md`. GitHub's checklist still shows unchecked boxes. |
| #1 observed-result tuning | Still open as evidence work. The current 10/100/20 thresholds remain an opt-in experiment. Scripted tests establish policy behavior; they do not establish real-provider review quality or justify changed thresholds. |
| #7 | Adds authorized commit, push, regular PR, CI reporting, and complete selected-review-fix delivery. See `pr-delivery.md`. |

The Phase 1 real-provider acceptance from #6 does not cover the later reviewer
roles, risk policy, or PR delivery. Measure focused/risk review results with a
real provider and record the evidence before claiming those experiments passed.
Keep issue #1's tuning item open until observed results support a tuning decision,
including a documented decision to retain the starting thresholds.

No extra implementation ticket is identified by this comparison. Update the
existing #1 checklist to match the implemented phases, while retaining the
unmeasured acceptance and tuning work. This local check does not change GitHub's
issue state or claim that those checks have already run.

Sources: [issue #1](https://github.com/pessini/software-factory/issues/1),
[issue #7](https://github.com/pessini/software-factory/issues/7), the commits above,
and the linked repository evidence documents.
