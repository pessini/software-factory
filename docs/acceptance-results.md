# Recorded Phase 1 acceptance — issue #6

Issue #1 is the tracking issue. The implementations of #2–#5 and #1 Phase 1
were already present at `24628bf`; this work verifies that combined workflow.
Later reviewer phases and PR delivery (#7) remain outside this work.

The final local series tested unchanged commit `7f1d622` on 2026-09-11.
**The recorded Phase 1 release passed local and hosted acceptance.**
[GitHub CI](https://github.com/pessini/software-factory/actions/runs/34643097838)
passed on macOS and Linux at commit `1772d18`. This result applies to the tested
setup and commits; later reviewer phases remain outside this acceptance.

## Results

- Hosted CI: 227 tests passed and 11 live cases were skipped on each of
  `macos-15` and `ubuntu-24.04`. Both jobs published all seven check logs.

- Fresh clone, frozen dependencies: Ruff lint and format, Mypy, and 227 tests
  passed; 11 opt-in live cases were skipped. The suite took 402 seconds.
- All nine real-provider scenarios passed in separate disposable clones.
- Both container permission cases passed without a model request. Existing
  source stayed visible, allowed writes reached the host, protected writes
  failed, and ordinary Python imports did not create cache files.
- A separate controller process was killed after dispatch and restarted.
  The same provider execution completed without another dispatch.
- A real BB parent displayed the saved report and current content version.
  Stopping and reopening that parent returned the same report.

| Scenario | Run | Final state | Check / review attempts |
| --- | --- | --- | --- |
| `feature` | `fct_live_49cf9dd20f82` | ready_for_review | 1 / 1 |
| `no_change` | `fct_live_7d8e90fb8e4d` | ready_for_review | 1 / 1 |
| `repair` | `fct_live_2bddc6600bc4` | ready_for_review | 2 / 1 |
| `revision` | `fct_live_8920ea779dbf` | ready_for_review | 2 / 2 |
| `restart_after_dispatch` | `fct_live_a7555e9667e2` | ready_for_review | 1 / 1 |
| `restart_after_result` | `fct_live_2d4a269b4c18` | ready_for_review | 1 / 1 |
| `connection_loss` | `fct_live_01991a087375` | ready_for_review | 1 / 1 |
| `question_restart` | `fct_live_da04d63ef882` | ready_for_review | 1 / 1 |
| `cancel` | `fct_live_de64afe46f66` | cancelled | 0 / 0 |

The machine-readable [acceptance record](phase1-acceptance.json) contains exact
versions, image digest, model settings, child and execution IDs, content hashes,
raw-record locations, and log hashes. The final live groups each passed three
cases. The process-kill and parent-display checks were recorded after the
worker mount fix and before later report and prompt clarifications; their
source notes are preserved in the record. The later source-change safeguard
only rejects invalid test evidence and does not change the workflow runtime.

## Tested setup

macOS 26.6.2 on ARM64, Python 3.14.7, uv 0.12.13, BB 0.42.1, Apple Container
1.0.0, and provider CLI 0.154.0. The worker image was
`software-factory-worker:0.154.0`. The actual model was `gpt-5.6-luna`:
Plan and Build used low effort, Repair and Review used medium, and Revision
used high. Every completed provider step reported the requested settings.
Provider usage totals were unavailable.

## Failures found and corrected

Earlier failures are preserved in local records and are not counted as passes.
The work corrected BB sender-prefix parsing, waiting for worker receipts after
an idle BB response, missing review context, and an Apple Container file-mount
problem that hid surrounding source files. It also made the timing and source
of report notes clear and exposed saved Git snapshots as review evidence.

One Builder explicitly ran `compileall`, creating protected ignored cache
files. The controller correctly blocked that run. Worker instructions now
explicitly prohibit such generated files. Protection rules were retained.

Acceptance checks now verify a running provider execution before cancellation,
match actual BB children and worker receipts, preserve execution IDs through
recovery, and clean up timed-out test commands. A final review added checks
that reject source changes during a live case.

## Reproduce and inspect

Follow [the acceptance runbook](acceptance.md). It gives setup, the opt-in
commands, expected results, inspection, question answering, cancellation,
and process-restart steps. The raw records and disposable workspaces are
local to this machine; other maintainers must generate their own records.

Live support is verified only for the setup above. Complete BB server outages,
remote BB environments, other providers, and live Linux workers are unverified.
The connection-loss tests drop a transport response at the BB API boundary;
provider responses remain real.
