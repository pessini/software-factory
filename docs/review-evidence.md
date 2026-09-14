# Single-reviewer evidence

Issue #1, Phase 1 completes the Planner → Builder → checks → Reviewer workflow.
The real-provider acceptance run passed in issue #6. Phase 2 adds the
[focused reviewers and judge](focused-review.md) using the same evidence rules.
This page describes the common evidence rules and the retained single-reviewer mode.

## Completion policy

`ready_for_review` means that the saved request, plan, project rules, required
checks, review, and current files agree. It does not record human approval.
The default local workflow stops at that handoff. A separately authorized
[delivery policy](pr-delivery.md) can commit, push, and open a regular PR.
Merge and deployment remain outside the workflow.

Each observable completion criterion has a stable ID. The Reviewer returns one
result per criterion: `satisfied`, `unmet`, or `not_verified`. A satisfied result
must cite evidence of the implementation or its checks. A request or plan alone
does not prove completion.

Each finding records an ID, optional criterion ID, category, severity, title,
evidence and evidence references, required change, confidence, and file/line
when applicable. `critical` means a concrete outage, data-loss, or security risk.
`warning` means a concrete defect or measurable risk. Both block completion.
`suggestion` is a useful improvement and does not block completion. Confidence
does not change these rules. Findings must concern defects introduced or exposed
by the requested change. Unrelated defects and personal style preferences are
outside the review scope.

## Saved evidence

Before Builder edits, the controller saves the exact request, linked issue when
present, project instruction sources and their directory scope, starting commit,
reviewed check commands, and baseline check results. The plan records criteria,
scope exclusions, a short set of reviewable work units, and test requirements.
The user request defines the outcome. Named project instruction files supply
project rules; ordinary source, logs, and previous agent output are data.

Each check attempt saves hashes for tracked and untracked files, file modes,
symlink targets, Git state, and the authorized inputs. It also saves the changed
file list and per-file patches, including added files. Checks and reviews refer
to this exact version. Changes during checks, between checks and review, after
review, or before a resumed attempt invalidate that evidence. The controller
stops and preserves the files for inspection.

Baseline and current results remain separate. A failed check is classified as
new, changed from baseline, or unchanged from baseline. Failure of a check that
already failed at baseline still blocks completion, even when its output changes.
Changed text, including durations, does not prove a new failure. These failures
require inspection and are not sent to Builder as unrelated repairs. Only a
check that passed at baseline can trigger automatic repair. A request already satisfied can finish with no file
changes when all required checks and criterion evidence pass.

Only blocking findings go to Builder for revision. Reviewer reuses finding IDs
and explicitly accounts for earlier findings. The saved history records
unresolved, fixed, and reopened findings. A Builder claim alone cannot close a
finding. Each repair or revision must pass checks and review again, within the
saved attempt limits.

## Handoff and compatibility

The report is rendered from the committed run record. It includes the change,
criterion results and evidence, tested and reviewed versions, baseline/current
checks, finding history, manual work, saved settings for every agent step,
reported effective settings, duration, attempt counts, and provider usage when
available. Missing effective settings are unverified; missing usage is
unavailable.

Phase 1 introduces versioned plans, reviews, and run evidence. Old plans and
string-based review output must be replaced with the current contracts. Active
runs without the Phase 1 evidence version cannot resume. Historical `accepted`
runs remain inspectable, but cannot become `ready_for_review` without a new run.
