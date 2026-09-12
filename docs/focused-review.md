# Focused reviews and judge — issue #1, Phase 2

Phase 2 follows the [passed Phase 1 acceptance](acceptance-results.md). The
acceptance work found missing review context and evidence limits. Focused scopes
and a judge now make each review's claims and the final decision separately
inspectable. Phase 1 live acceptance remains evidence for the earlier workflow;
it does not establish real-provider acceptance of these new roles.

## Enable the workflow

New configurations from `software-factory init` contain `review_mode: focused`.
For an existing project, add that entry to `.factory/factory.yaml` before starting
a new session. Omission, or `review_mode: single`, retains Phase 1 behavior.
Saved runs retain their mode. No active run acquires extra reviewers during restart.

The route is Planner → Builder → required checks → Request Review → Code Review
→ Security Review → Judge. Execution is sequential. Each reviewer and the judge
has a separate BB conversation. Only Builder, including its repair and revision
steps, may make intended implementation edits.

| Role | Required scope |
| --- | --- |
| Request Reviewer | Original request, completion criteria, project rules, scope, and required evidence |
| Code Reviewer | Correctness, reliability, performance, and meaningful test coverage in changed or affected code |
| Security Reviewer | Concrete security risks introduced or exposed by the change |
| Judge | Verify each claim against source and evidence; remove unsupported claims and duplicates; decide whether to accept or revise |

All four roles use the existing protected worker, file checks, model settings,
operation records, time limits, correction limit, cancellation, and recovery.
Use [model settings](model-settings.md) to choose each step's provider, model,
and effort. There is no second process-control or recovery system.

## Decisions and saved evidence

Each round saves `review/request-NN.json`, `review/code-NN.json`, and
`review/security-NN.json`. These use `FocusedReviewEnvelope` version 1. Request
Review returns every criterion result. Code and Security return findings within
their scopes. None of these records grants final approval.

The judge uses `JudgeEnvelope`, which extends the version 2 final review with
an assessment for every input finding. Each assessment identifies the reviewer
and its local finding ID, gives a reason and current evidence references, and
marks the finding as retained, duplicate, or unsupported. A retained assessment
assigns a canonical final finding ID. Duplicate assessments point to that ID.
Unsupported claims are removed with an explanation. Reviewers may use the same
local ID for different defects; the pair of reviewer and ID keeps them distinct.

The controller requires one retained assessment per final finding. Each retained
critical or warning finding blocks acceptance and must have an explanation.
Suggestions do not block. The judge independently verifies every criterion and
accounts for earlier findings as unresolved, fixed, or reopened. It should accept
when the evidence is complete and no concrete blocking defect remains. A
reviewer's unsupported claim does not force a Builder revision.

The controller saves `review/judge-NN.json`, the final decision in
`review/attempt-NN.json`, and canonical finding history in `review/findings-NN.json`.
Only retained blocking findings go to Builder. Every revision must pass the
required checks and all three reviews plus the judge again, within the saved
revision limit. Earlier rounds remain available.

## Failure and restart

A missing, failed, invalid, or stale required review cannot count as approval.
Invalid output gets the existing single correction attempt. A confirmed failure
blocks the run. An uncertain dispatch requires recovery and keeps ownership.
Completed earlier reviews remain saved in both cases. The judge cannot waive a
missing required review or a failed controller check.

Before each reviewer, before the judge, before Builder revision, and before final acceptance, the
controller verifies the tested file version. Unexpected edits stop the run and
remain in place for inspection. Restart reconciles the same saved operations;
it does not dispatch completed reviews again. Cancellation stops active owned
work and prevents finalization, including between review steps.

The final saved handoff includes all required reviews, the judge's assessments,
canonical finding history, exact content version, and execution evidence for
every active step. The report is rendered from that saved state.

The Phase 2 tests use the public workflow interface with a scripted provider.
They cover partial failure, unsupported and repeated findings, invalid output,
file protection, stale or missing evidence, model settings, restart, and
cancellation. [Phase 3](risk-review.md) adds opt-in risk thresholds, a relevant
Documentation Reviewer, and saved review-quality results. Fixed focused mode
keeps all three reviewers. Concurrent execution remains outside the current
workflow. PR delivery remains issue #7.
