# Measured review results and threshold decision — issue #1

Recorded on 14 September 2026. **Retain 10 documentation lines, 100 normal-change
lines, and 20 files. Keep risk selection opt-in and model fallback disabled.**
This completes the initial measurement and tuning decision for issue #1. It does
not establish optimal limits or general production review accuracy.

Eight cases completed with real provider responses and passed their acceptance
checks. Earlier failed and interrupted attempts remain in the
[machine-readable record](review-measurements.json), with exact source versions,
run and provider execution IDs, local raw-record paths, and evidence hashes.

## Completed measurements

Findings and retained blockers are counts per round. The arrow separates the
first review from the review after revision. Fixed is the final count of resolved
canonical findings. Review seconds sum each review step and Judge attempt once.

| Case | Selected level | Reported findings | Retained blockers | Fixed | Review seconds |
| --- | --- | --- | --- | --- | --- |
| `focused_revision` | fixed focused | 3 → 0 | 2 → 0 | 2 | 239.0 |
| `risk_docs_10` | trivial | 0 | 0 | 0 | 81.8 |
| `risk_docs_11` | normal | 0 | 0 | 0 | 99.4 |
| `risk_docs_100` | normal | 0 | 0 | 0 | 161.4 |
| `risk_docs_101` | full | 0 | 0 | 0 | 181.9 |
| `risk_normal` | normal | 0 | 0 | 0 | 113.8 |
| `risk_sensitive` | full | 0 | 0 | 0 | 128.0 |
| `risk_revision` | normal | 2 → 0 | 1 → 0 | 1 | 170.3 |

The 10-line change selected Request and Documentation reviews. At 11 and 100
lines, Code Review was also required. At 101 lines, Security Review was added.
The 18-line, three-file code change selected normal review. The 11-line,
two-file access-rule change selected full review because its source and tests
contained sensitive changes. All cases also required the Judge and configured tests.

Both staged defect cases required an initial missing public export. The real
reviewers reported blocking findings, the Judge accounted for repeated reports,
and the Builder revision passed fresh checks and review. The final findings were
recorded as fixed. Each role had a separate saved review, and each passing case
verified its final content and reported model settings. No required review was waived.

## Observed failures and corrections

- A revision completed its edits but reported a blocker because the isolated
  worker could not run host pytest dependencies. Build, repair, and revision now
  share an explicit handoff rule. The controller still runs and requires the tests.
- Another revision listed only that turn's edited files. The controller rejected
  the incomplete report. Every Builder handoff now explicitly requires all files
  still changed from the starting commit. The strict file check remains in place.
- An earlier focused Judge retained an unnecessary final direct-import test
  requirement. The request described that import arrangement for the first build;
  the plan carried it into the final requirements. This study records one
  over-strict finding. The retry states the final test-import scope explicitly.
  It does not prove that the provider will reject every unsupported claim.
- The 100-line Request Review first echoed an incorrect content-version hash.
  The controller rejected it and used the single allowed output correction. The
  extra execution and time are included; the stale value did not become approval.
- The measurement reader initially confused focused artifacts with the risk-only
  report and compared reports with and without separately stored feedback. Those
  reader errors were corrected and the old failed attempts were preserved.
- The first command had a missing temporary parent directory and made no model
  requests. A later trial ran two live groups at once and queued the next workers
  while completed BB tasks held execution slots. The affected group was stopped
  with owned-worker cleanup. Remaining groups ran one at a time.

Three useful finding assessments were saved through the risk feedback interface
on a completed earlier run and read back through a new engine. Its status,
approval, content version, and protected review files remained unchanged. The
assessments are labelled as work by the task assistant, not a separate human
review. The focused false-positive assessment is in the study record because
the native feedback interface applies to risk mode. These are counts of inspected
findings, not a measured population false-positive rate.

## Why retain the limits

The boundary cases and the sensitive-file case selected the required reviews.
The normal-risk defect case still required correction of the known missing
export. The observed failures concern handoff instructions and request meaning;
changing line or file limits would not directly correct them.

This sample is too small and too controlled to justify either more skipped
reviews or more mandatory reviews. There are no live observations near the
20-file limit. Review time is one observation per case, includes corrections,
and does not establish a causal speed improvement. Provider usage is unavailable,
so token or monetary savings cannot be calculated. The supported decision is to
retain the starting policy and collect broader evidence before changing it.

Future tuning should compare representative repository changes near each limit,
including sensitive changes, defects found after review, retained false positives,
and review time. A repeat study should include cases whose defects are not disclosed
to the reviewers. No new threshold value is justified by the current results.

## Setup, checks, and scope

The setup was macOS 26.6.2 ARM64, Python 3.14.7, BB 0.42.1, Apple Container 1.0.0,
and provider CLI 0.154.0. Every completed provider step matched `gpt-5.6-luna`
with the requested effort: low for Plan/Build, medium for reviews/Judge, and high
for Revision. Provider usage totals were unavailable. The image digest and exact
settings are saved in the JSON record.

Passing runs used commits `1f50116` and `6be9c94`; each case checked that its
source identity stayed unchanged. The risk policy is identical between them.
The later commit clarified Builder change lists and the staged test request.
`bash scripts/check.sh` passed on `6be9c94`: lint, formatting, types, and
319 tests; 19 opt-in live tests were skipped. Hosted checks for the final PR
commit are reported in that PR, separately from this local record.

The [runbook](acceptance.md#focused-and-risk-review-measurements--issue-1) gives
the commands and independent expected results. Use new disposable directories
and one live group at a time. Raw workspaces and BB records are local to this
machine; another maintainer must produce their own raw runs.

This is an initial study on a demo target with one provider setup. The staged
defects are disclosed in the request. The 20/21-file and generated-file rules
remain supported by controlled tests. The study does not claim blind defect
detection, broad provider support, or live PR delivery. Issue #7 has 24 controlled
delivery/adapter tests, including commit identity, CI, restart, and complete
selected review-fix delivery; see [PR delivery](pr-delivery.md).
