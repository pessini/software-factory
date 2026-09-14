# Completion of issues #1 and #7

Updated on 14 September 2026 after the real-provider review measurements and
the full local checks at `6be9c94`.

**The seven-issue plan has no remaining implementation or initial tuning work.**
Issue #7 is the last planned feature. The feature commits and the completion
evidence are delivered through a regular PR; merge remains a separate action.

| Scope | Evidence and remaining work |
| --- | --- |
| Issues #2–#6 | All five issues are closed on GitHub. Phase 1 local and hosted acceptance is recorded in `acceptance-results.md`. |
| #1 Phase 1 | Its checklist is complete. The accepted first release uses one Reviewer. |
| #1 Phase 2 | Implemented in `b279af0`: Request, Code, Security, and Judge roles, separate evidence, shared settings/recovery, and 32 focused workflow tests. The live staged-defect case completed revision and recorded the findings as fixed. See [focused review](focused-review.md). |
| #1 Phase 3 | Implemented in `9a6abae`: risk boundaries, sensitive-path protection, relevant Documentation Review, generated-file handling, and saved feedback. Supported by 27 risk workflow tests and the recorded live cases. See [risk review](risk-review.md). |
| #1 observed-result tuning | Complete initial decision: retain 10/100/20, keep risk mode opt-in, and keep fallback disabled. Eight cases passed; earlier failures and limits remain recorded. See [measurements and decision](review-measurements.md) and [exact evidence](review-measurements.json). |
| #7 | Implemented in `a5d24c1`: authorized commit, push, regular PR, CI reporting, and complete selected-review-fix delivery. All 24 delivery/adapter tests passed. See [PR delivery](pr-delivery.md). |

The new study supplies the focused/risk measurements that the earlier version of
this assessment identified as missing. Live runs exposed two Builder handoff gaps:
repair/revision instructions did not consistently explain controller-run checks
or require the complete file-change list. Commits `1f50116` and `6be9c94` corrected
those instructions. The strict test and file checks remain in place.

`bash scripts/check.sh` passed lint, formatting, types, and 319 tests, with 19
opt-in live tests skipped. The eight passing live cases are separate from that
suite. They used real responses on a controlled demo target, including exact
10/11/100/101-line boundaries, a small sensitive change, and required revisions.

The study does not establish optimal thresholds, a general false-positive rate,
other provider support, or live controller PR publication. The 20/21-file and
generated-file cases use controlled tests. Issue #7 explicitly permits a controlled
GitHub adapter for its completion checks. Broader production measurements remain
future evaluation, rather than an unfinished item in the initial tuning decision.

The Phase 2/3 and #7 implementation checkboxes can now be checked against this
evidence. The issues remain open until the delivery PR merges; the PR includes
the closing references for both issues.

Sources: [issue #1](https://github.com/pessini/software-factory/issues/1),
[issue #7](https://github.com/pessini/software-factory/issues/7), the commits above,
and the linked evidence documents. The original assessment at `a5d24c1` recorded
the state before these measurements.
