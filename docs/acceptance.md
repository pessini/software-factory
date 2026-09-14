# First-release acceptance — issue #6

Issue #1 is the tracking issue. Its Phase 1 implementation at `24628bf` follows
the implementations of #2–#5. Later reviewer phases and PR delivery (#7) are
outside this acceptance check. An open dependency ticket does not, by itself,
mean its code is missing.

See [the recorded local results](acceptance-results.md) and
[versioned evidence](phase1-acceptance.json).

Each release requires clean CI, every live scenario below, and the BB parent
display check to pass. The recorded Phase 1 acceptance passed these checks;
See the linked results for its exact commits and supported setup. The older
`accepted` run in `live-worker-acceptance.json` predates Phase 1 and is not a
release pass.

## Normal checks

From the factory repository, with Python 3.14 and uv installed:

```bash
bash scripts/check.sh
```

This installs `uv.lock` with `uv sync --frozen`, then runs Ruff lint and format
checks, Mypy, and the full automated suite. It explicitly disables live checks
and unrelated pytest plugins. Only the required asyncio plugin is loaded.
It needs no BB server, container runtime, provider login, or model credentials.
Do not set live-test flags to turn this command into an acceptance run.

The GitHub workflow runs the same command on `macos-15` and `ubuntu-24.04`.
Each job saves `.checks/`, including installation errors, separate check logs,
and `tests.xml`. A failed check does not prevent the remaining checks from
running. A failed installation stops the job before it can use stale packages.
The Actions page must show both jobs passing for the tested commit.

## Live setup

Use the [macOS runbook](macos-runbook.md) and
[protected worker setup](protected-worker.md). Start the local BB app and keep
an existing Codex login available. Register `software-factory` and
`software-factory-worker` as custom ACP providers. The installed factory command
must point to the checkout under test, for example through `uv tool install --editable .`.

Use `bb status --json`, `bb project list --json`, and `bb provider models codex
--json` to select an existing project and an available model. If `bb` is not on
PATH, set `FACTORY_BB_COMMAND` to its absolute command. For the tested macOS app:

```bash
export FACTORY_BB_COMMAND='node /Applications/bb.app/Contents/Resources/app.asar.unpacked/node_modules/bb-app/dist/bb.js'
```

The chosen model must support low, medium, and high effort. There is no silent
fallback. The protected worker verifies provider CLI 0.154.0 during startup.
Other providers, remote BB environments, and other provider CLI versions remain
unsupported. Normal Linux CI does not establish support for live Linux workers.

Run the local protection tests without sending model requests:

```bash
FACTORY_LIVE_CONTAINER=1 uv run --frozen pytest tests/live/test_worker_permissions.py
```

## Opt-in real-provider checks

These checks send demo source and task instructions to the selected provider
and use the existing login. Set the project and model from the actual BB catalog:

```bash
export FACTORY_ACCEPTANCE_PROJECT=PROJECT_ID
export FACTORY_ACCEPTANCE_MODEL=MODEL_ID
export FACTORY_ACCEPTANCE_OUTPUT=/absolute/path/to/acceptance-results
FACTORY_LIVE_PROVIDER=1 uv run --frozen pytest tests/live/test_provider_workflow.py -s
```

Each case makes a separate disposable Git clone of the demo target and creates
a BB parent task. It does not edit the factory checkout or the project's normal
workspace. A dummy ignored file and an outside file must retain their contents.
Plan and Build use low effort, Repair and Review use medium, and Revision uses
high. The test compares the requested settings with the real worker responses.

Use `-k feature` for the first run. Run the complete file after it passes.
Use a new result directory for each acceptance series. The JSON records contain
the factory commit and local diff hash, platform, versions, image identity,
actual model catalog, requested and reported settings, run state, operation IDs,
provider turn IDs, and cleanup results. Failed assertions remain failed results.
Keep the factory checkout unchanged until the full series finishes. A source
change detected before or after a case makes that case fail. Keep the disposable
clone and BB storage until the evidence has been reviewed.
Pytest can remove older temporary directories; use a fresh, explicitly chosen
`--basetemp` directory if longer retention is required. Never reuse a directory
that contains user work: pytest clears its base directory when a run starts.

| Case | Required result |
| --- | --- |
| `feature` | Real implementation, passing checks, satisfied criteria, and `ready_for_review`. |
| `no_change` | Existing greeting behavior verified, with no file changes. |
| `repair` | Deliberately incorrect first build fails tests; the real Builder repairs it and checks pass. |
| `revision` | First build omits a required export; real review rejects it, revision fixes it, and review records the finding as fixed. |
| `restart_after_dispatch` | Controller interruption during an observed provider execution; a new engine recovers the same child and execution ID without another dispatch. |
| `restart_after_result` | Controller interruption after a real Planner result; a new engine continues from the saved result. |
| `connection_loss` | The test drops a BB creation response; the controller recovers the real child without resending. |
| `question_restart` | Real Planner asks a blocking question; a new engine reloads it, accepts the answer, and completes the same run. |
| `cancel` | Cancellation during an observed provider execution stops owned work and preserves the workspace. |

Transport faults are deliberately introduced at the BB API boundary. The restart
cases replace the controller engine in the same test process; they do not prove
an operating-system process kill or a BB server outage. Provider replies are
never replaced with scripted output. Repair and revision requests explicitly
ask for an incomplete first build in the disposable fixture. A provider that
skips that stage makes the scenario fail; it is not counted as exercised.

## Focused and risk review measurements — issue #1

Use the same setup and a new output and temporary directory. Select only the
later review cases with `-k 'focused_revision or risk_'`:

```bash
FACTORY_LIVE_PROVIDER=1 uv run --frozen pytest tests/live/test_provider_workflow.py \
  -k 'focused_revision or risk_' -s --basetemp=/absolute/path/to/new-disposable-cases
```

These eight cases use real Planner, Builder, reviewer, and Judge responses. The
focused and normal-risk revision cases omit a required export on the first build, then
requires a retained finding, a revision, and saved resolution. Four documentation
cases require exact changes of 10, 11, 100, and 101 lines. A normal code case and
a small access-rule change check size selection and the sensitive-path override.
The test checks final behavior independently of the provider's own tests.

Request, Code, Security, Documentation, and Judge steps use medium effort. The
other settings and protection checks match Phase 1. Each record contains saved
review-quality results, elapsed time, source identity, worker receipts, and the
local evidence path. A new engine must read back identical saved results. Fixed
focused mode is measured from its review files; risk mode also checks the public
review-quality command. Repair and revision hand off unavailable worker checks
to the controller, which must run and pass them before acceptance.
The four exact documentation sizes isolate the routing limits; they do not
represent large application changes or prove review accuracy for ordinary work.
The 20/21-file boundary remains covered by the controlled workflow tests.

Inspect all findings against the saved source and patch before recording useful
or false-positive feedback. Do not infer accuracy from approval or an empty list
of findings. Report missing usage as unavailable. Keep failed cases and explain
any retry. Record a threshold decision only after inspecting the measurements;
retaining the starting limits is a valid decision when evidence is limited.

## Inspect, continue, and recover

Each JSON result identifies its BB parent, run ID, and workspace. In the owning
BB environment, use:

```bash
software-factory inspect RUN_ID
software-factory resume RUN_ID
software-factory answer RUN_ID INTERACTION_ID '{"question-id":"answer"}'
software-factory retry RUN_ID
software-factory cancel RUN_ID
```

Read the recorded error before continuing. `recovery_needed` keeps ownership
when the controller cannot prove what happened. Never delete ownership records
or resend an uncertain operation to force a pass. Cancellation is complete only
after child containers are confirmed stopped. The tests preserve failed work
and write cleanup errors in the result record.

## BB display and process-restart check

The automated live cases exercise the engine and real visible worker tasks.
They do not drive the BB parent display. Before release, use another disposable
demo clone to run `examples/demo-target/REQUEST.md` from a real **Software
Factory** parent task, with the same settings selected in the Factory panel.
Record that the parent text, stored state, final report, and current content
version agree. Repeat inspection after stopping and reopening the parent.

Also stop a controller process after dispatch, leave its worker running, and
continue through the same saved run. Record the before/after process and
operation identities. Confirm one logical dispatch and no stale output. Do
this only to a controller process whose identity and disposable workspace have
been established. A simulated interruption is not evidence of this check.

Record every untested capability as unverified. Issue #6 stays open if CI,
any required scenario, the display check, or process recovery remains unverified.

### Reproduce a real controller process restart

Use a new disposable demo clone, with dependencies installed and the selected
model settings saved. Create a Software Factory BB parent in that clone with
`/status`. Set `BB_THREAD_ID`, `BB_PROJECT_ID`, `BB_ENVIRONMENT_ID`, and
`BB_THREAD_STORAGE` from that parent's actual BB metadata. Keep the same values
for the commands below. Do not attach these commands to a normal project task.

First, from the factory checkout, capture its installed Python path:

```bash
export FACTORY_ACCEPTANCE_PYTHON="$(uv run --frozen python -c 'import sys; print(sys.executable)')"
```

In the disposable clone, start one controller process whose ID you own:

```bash
export FACTORY_RESTART_RUN="fct_restart_$(date +%s)"
"$FACTORY_ACCEPTANCE_PYTHON" - <<'PY' > "/tmp/$FACTORY_RESTART_RUN-before.log" 2>&1 &
import asyncio
import os
from pathlib import Path
from software_factory.bb import BBContext
from software_factory.engine import FactoryEngine

engine = FactoryEngine(workspace=Path.cwd(), context=BBContext.from_environment())
asyncio.run(engine.run(
    "Add and export is_even with tests for positive, negative, zero, even and odd integers.",
    os.environ["FACTORY_RESTART_RUN"],
))
PY
factory_controller_pid=$!
```

Inspect from a second terminal with the same BB variables and `FACTORY_RESTART_RUN`.
Save the output
outside the disposable workspace so the log does not change the files being
tested:

```bash
software-factory inspect "$FACTORY_RESTART_RUN" > "/tmp/$FACTORY_RESTART_RUN-dispatch.json"
```

Wait until the Planner operation has a nonempty `thread_id` and `execution_id`
and state `dispatched`. Record those values. In the first terminal, stop only
the process saved above and continue the same run. Confirm that `jobs -l` still
lists that PID as the running controller before sending the signal:

```bash
kill -KILL "$factory_controller_pid"
wait "$factory_controller_pid"  # A killed-process exit is expected here.
software-factory resume "$FACTORY_RESTART_RUN"
software-factory inspect "$FACTORY_RESTART_RUN" > "/tmp/$FACTORY_RESTART_RUN-resumed.json"
```

Confirm `ready_for_review`, the same Planner child and execution ID, one dispatch
per operation, and stopped owned workers. Compare the report's content version
with its saved check and review records. If any check fails, preserve the output
and workspace; do not count the run as passed.
