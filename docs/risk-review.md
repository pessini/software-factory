# Risk selection and review quality — issue #1, Phase 3

## Policy and boundaries

Select `review_mode: risk_based` in `.factory/factory.yaml` before starting a new
session. Existing `single` and `focused` configurations and saved runs retain
their earlier behavior. The policy is an opt-in experiment. It does not change
models or waive any configured check.

The controller selects reviewers from the actual changes after required checks
pass. It counts added plus removed lines; replacing one line counts as two.
File counts include additions, deletions, and both sides of a rename. The first
matching rule wins:

| Level | Rule | Required reviewers, followed by Judge |
| --- | --- | --- |
| Full | Any sensitive path, sensitive changed content, or an unmeasurable change | Request, Code, Security |
| Trivial | Only documentation changes, at most 10 lines, and at most 20 files | Request, Documentation |
| Normal | At most 100 lines **and** at most 20 files | Request, Code |
| Full | Either normal limit is exceeded | Request, Code, Security |

These limits use measured size, which excludes only ordinary generated files.
The saved record includes both measured and total file counts. A change that
includes generated code cannot qualify as documentation-only.

Exactly 10 documentation lines remain trivial. Exactly 100 lines and 20 files
remain normal. A verified no-change result uses Request and Judge. Documentation
Review is also required at normal or full level when documentation or project
instructions are relevant.

Sensitive rules take priority over documentation and generated-file rules.
Built-in rules cover authentication, authorization, secrets, cryptography,
migrations, deployment controls, dependency manifests and lockfiles, and program
execution configuration. Project-specific sensitive patterns add to those rules.
Patch content that refers to these boundaries also requires Security Review.
This includes unchanged hunk context and function headers, so changing a return
value inside an authentication function does not lose its security scope.
These conservative rules do not prove that other code has no security defects.

Documentation Review checks instructions when tools, commands, environment
variables, directory structure, or CI change. It checks whether the saved project
instructions and user documentation still describe the changed behavior. It has
read-only permissions and returns findings to the same judge.

Ordinary generated files receive less attention in prompts and do not increase
the measured size. Their names, full patches, and content hashes remain in the
saved evidence. Sensitive generated files always receive full review and count
toward size. Binary or other unmeasurable non-generated changes require full
review. No evidence is discarded, and all configured checks still run.

## Saved policy and tuning

The configuration can set `review_policy.trivial_max_lines`, `normal_max_lines`,
`normal_max_files`, `sensitive_paths`, `documentation_paths`, and `generated_paths`.
Paths are case-insensitive repository-relative glob patterns. A pattern without
a slash matches a name at any depth. A pattern ending in `/` matches a directory
and its contents. Sensitive patterns are additive; they cannot remove built-in
security rules. The trivial line limit cannot exceed the normal line limit.

Each review round records the policy version and values, measured size, selected
level, reasons, sensitive files, generated files, and required reviewers in the
check bundle. Selection is saved before dispatch. Restart reuses that selection
and the existing operation records. Revisions create fresh checks and a fresh
selection; earlier evidence remains available. The judge and final handoff
require every selected review for the same tested content version.

Start with the issue's proposed 10/100/20 limits. Compare saved results at each
level before changing a project's limits. Record why each change is justified,
then use the revised configuration for new sessions. Threshold tuning is an
operator decision based on observed results, not an automatic model decision.
Do not treat simulated-provider tests as measured live review quality.

Model fallback remains disabled. Every possible review step has its own saved
provider, model, and effort through the existing settings controls.

## Review quality and feedback

The run report records review rounds, all reported findings, the judge's retained
blocking findings, fixed findings, duration, and provider usage when available.
Missing usage is reported as unavailable. Incomplete rounds remain incomplete.

Use `software-factory review-quality RUN_ID` to inspect those results and saved
user feedback. Use `software-factory review-feedback RUN_ID ATTEMPT REVIEWER
FINDING_ID VERDICT REASON` to record `false_positive` or `useful` feedback about
a specific reviewer finding. Feedback includes the review round and content
version, and is kept separately from authoritative review decisions. Corrections
append a new record; the latest response per finding is used in counts. Feedback
does not waive a check, resolve a finding, approve a run, or change a threshold.

The workflow tests use configuration loading, public run/resume operations, and
the feedback/report interface. They cover threshold boundaries, sensitive paths,
generated changes, optional reviewers, recovery, and evidence preservation.

## Verification — 12 September 2026

`bash scripts/check.sh` passed lint, formatting, type checking, and the full
suite: 295 tests passed and 11 opt-in live tests were skipped. The independent
standards and specification reviews passed after correcting sensitive hunk
context detection and interrupted-round reporting. Both corrections have
regression tests that failed before the fixes and passed afterward.

The workflow tests use a scripted provider. Live-provider review quality and
threshold tuning remain experiments to measure with the saved results; this
check does not establish live-provider acceptance of the new review policy.
