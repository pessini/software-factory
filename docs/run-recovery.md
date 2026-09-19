# Run ownership, cancellation, and recovery

This page records the original BB-backed recovery design. For new context-version-2
runs, use [independent job recovery](interface-migration.md#independent-job-recovery)
and [shared control commands](factory-interfaces.md). In that path Factory owns
workers, `ready_for_review` is the successful outcome, and command return does
not mean the detached job has finished. The historical child-task and `accepted`
details below apply to old BB records.

SQLite is the source of truth for a run's status. A LangGraph checkpoint records
where to continue. A checkpoint cannot replace a stored stop request or final
outcome. Use `inspect` to see the current status, operations, processes, and events.

## States

| State | Meaning | Permitted next states |
| --- | --- | --- |
| `pending` | The run is saved but has not started work. | `running`, `waiting_for_input`, `stopping`, `recovery_needed`, `failed`, `blocked` |
| `running` | The executor is doing work or waiting for an external operation. | `waiting_for_input`, `recovery_needed`, `stopping`, `accepted`, `failed`, `blocked` |
| `waiting_for_input` | The Planner or a child needs a user's answer. | `running`, `recovery_needed`, `stopping` |
| `recovery_needed` | The outcome of external work or cleanup is not known. | `running`, `waiting_for_input`, `stopping` |
| `stopping` | A stop was accepted; cleanup is incomplete. | `recovery_needed`, `cancelled`, `failed`, `blocked` |
| `accepted`, `cancelled`, `failed`, `blocked` | A final outcome. | None |

State changes and their state events use one SQLite write transaction. A stop
request saves its intended final outcome separately as `stop_target`. A user
cancellation takes precedence over a pending failure stop. Once a stop is saved,
the run cannot become accepted. If acceptance was saved first, `cancel` returns
that accepted outcome. `cancel` can return `stopping` while the executor cleans up;
this does not mean that external work has stopped.

The executor checks for stop requests before and after each workflow node, and
while it waits for children or check commands. Final failure and cancellation
require confirmed cleanup. Uncertain cleanup leaves `recovery_needed` and keeps
the workspace reservation. Reports and command results use the SQLite status.
`resume` returns exit code 1 for every outcome other than `accepted`.

## One writer per workspace

Operating-system file locks protect the run ID and the resolved workspace path.
All parent tasks use the same per-user `software-factory` state directory from
`platformdirs`. This directory is independent of each task's checkpoint storage.
`FACTORY_OWNERSHIP_DIR` can select a directory for an isolated installation or
test. All executors that use one workspace must use the same directory.

A saved workspace claim contains the run ID and database path. It survives a
process crash. Locks have no expiry. A new run cannot replace an unfinished or
uninspectable claim. Resume of the same run acquires the locks, confirms the BB
workspace, accounts for old processes, and identifies outstanding child requests
before continuing. A child already doing that exact request is reattached. It
does not receive another implementation request.

The locks apply on one machine and within one user account. They do not provide
coordination across machines or prevent an unrelated program from editing files.
Repository and evidence checks continue to enforce the existing protection policy.

## External operations

Each create request, follow-up, output correction, and check command has a saved
record containing its run, step, attempt, type, request, deadline, and repeat rule.
The record moves through `prepared`, `dispatching`, `dispatched`, and `completed`
or `stopped`. The dispatch record is committed before the external call. A lost
response leaves an uncertain operation. Recovery must identify the operation;
it cannot infer success from a task's latest output or send the request again.

Child creation retains the existing exact-title lookup. A missing match after an
uncertain create does not authorize another create. Follow-up and correction
requests need a provider receipt or a provider lookup that links the saved
operation to one execution. The execution ID must match on reconciliation and
completion. Completed output is saved before the workflow can move to its next
node. If the node runs again after a crash, it uses that saved output.

Read retries reconnect to the same execution. The retry budget is saved per
operation. A retry does not select another model. Provider failure, bad settings
or credentials, connection failure, waiting for input, time limits, and user
cancellation have separate control paths. Dispatch counts include uncertain
dispatch attempts; they do not claim that the provider accepted every request.

Check commands default to `repeat_on_recovery: false`. An operator may set this to
`true` only for a verification-only command. The operation then saves the
`verification_only` repeat rule before dispatch. If its process has stopped but
its result was not saved, resume may repeat that check under the saved rule.
The logical attempt remains the same; `dispatch_count` increases. A repeated
check retains its original deadline.

## Deadlines and processes

`limits.step_timeout_seconds` defaults to 900 seconds. Each operation saves a
deadline limited by the run's original total deadline. Check commands also use
their configured timeout. `limits.connection_retries` defaults to 2. Restart does
not reset these deadlines or retry counts. A saved blocking question pauses the
remaining run and unfinished-operation time. Accepting its first answer shifts
those deadlines by the time spent waiting. Duplicate answers do not extend them.
The separate `limits.overall_run_timeout_seconds` limit defaults to seven days
and includes time waiting for input. The controller checks this expiry when work
resumes or an answer arrives; a live worker also checks it while waiting.
See [saved sessions and questions](session-continuation.md) for the commands.

Check commands and BB CLI commands run in new process groups. A pipe gate prevents
the command from executing until the group and its process identity are saved.
If the controller dies before opening the gate, the waiting process sees EOF and
exits. Cleanup sends TERM, then KILL if necessary, and checks for remaining live
group members. It does not treat the group leader's exit as sufficient. A changed
process identity or a failed inspection preserves the process record and requires
recovery. BB CLI commands have a 30-second process limit.

## Existing saved runs

Initialization adds the new tables and columns without deleting existing data.
New runs have `control_version = 2`. Existing rows receive version 1 because they
have no complete operation or ownership history. Inspection remains available.
Resume explicitly rejects version-1 rows; changing that version number cannot
reconstruct the missing evidence. Inspect old children and processes before
starting replacement work. Existing version-2 artifact protection remains in use.

A crash during artifact publication can leave a file and its manifest out of
agreement. Resume rejects that evidence and stops owned work; it does not repeat
implementation or accept the damaged result. Cancellation cleanup can use the
operation and process records without loading artifacts or configuration. Damaged
files remain available for inspection. The SQLite result is authoritative if a
report file could not be published.

## BB compatibility evidence

The BB CLI and SDK source was checked at
[`4ae0a7c893e9922721e0e367c72aa3a1a2990e7c`](https://github.com/get-bb/bb/tree/4ae0a7c893e9922721e0e367c72aa3a1a2990e7c).
The relevant definitions are
[`threads.ts`](https://github.com/get-bb/bb/blob/4ae0a7c893e9922721e0e367c72aa3a1a2990e7c/packages/sdk/src/areas/threads.ts),
[`actions.ts`](https://github.com/get-bb/bb/blob/4ae0a7c893e9922721e0e367c72aa3a1a2990e7c/apps/cli/src/commands/thread/actions.ts),
and [`environment.ts`](https://github.com/get-bb/bb/blob/4ae0a7c893e9922721e0e367c72aa3a1a2990e7c/apps/cli/src/commands/environment.ts).

- `environment show` returns the environment, including its `id`, `projectId`, and
  `path`. The adapter checks these fields before dispatch.
- `thread output` returns `{output: string | null}` without an execution ID.
- `thread tell` has no caller-supplied operation-key option. A successful command
  alone cannot prove that its latest output belongs to the new request.
- Waiting for `idle` can fail at `error`. A status of `idle` alone does not prove
  that pending interactions or scheduled work are absent.

The default adapter now uses the [protected worker](protected-worker.md). It
records actual provider thread and turn IDs outside the provider's writable
mounts, reads only the matching saved result, and checks container exit. The
[authenticated evidence](live-worker-acceptance.json) covers controller restart,
one dispatch after recovery, cancellation, and a same-Builder follow-up.

The generic BB CLI adapter remains disabled because its latest text cannot
establish request identity. No exactly-once guarantee is inferred from the BB
CLI alone. Uncertain dispatch remains a recovery state and is not repeated.
