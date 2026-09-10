# Protected BB worker: acceptance pending

The protected worker is an opt-in implementation for local BB 0.42.1, Apple
Container 1.0.0, and the Codex provider CLI 0.154.0. It is not yet approved for
routine use. The authenticated BB/provider acceptance test has not run.

The default adapter remains disabled. Set `FACTORY_PROTECTED_WORKER=1` on the
Software Factory parent agent only when testing the new worker. `doctor` then
checks the worker image and BB registration. A successful prerequisite check
does not establish that a complete model run passed.

## What the worker does

BB starts a visible child using the custom ACP provider
`acp-software-factory-worker`. The message contains the parent task ID and the
saved operation ID. The worker reads the original prompt and fixed model choice
from the parent's controller storage. It checks the child title, parent,
environment, repository, and any previously saved child ID before dispatch.

Each operation gets a named container and a saved creation record. Planner and
Reviewer get a read-only workspace. Builder gets a writable workspace with
read-only mounts for Git metadata, configured protected paths, existing ignored
files, and controller evidence. The provider's command permissions also enforce
these restrictions and disable command network access. Provider communication
with OpenAI still requires network access outside the command sandbox.

The existing Codex login file is mounted read-only into a separate provider
directory. It is not copied into the repository, Git objects, or evidence.
Provider commands cannot read the login file. The provider directory is separate
from the worker's request and result records.

The worker saves the actual provider thread and turn IDs. Only a matching turn
can supply output. A completed request can return its saved result after restart;
an uncertain request is never sent again. Repair and revision can resume a
previous provider conversation when the saved settings match.

Cancellation first saves a stop request. Cleanup checks each container's name
and operation label, stops it, and verifies its stopped state. After a worker
crash, the controller must acquire the worker lock before updating its saved
result. A record that proves container creation never started can be stopped
without a container. An uncertain creation remains in recovery and retains
ownership.

Questions from the matching provider turn are saved and reported as waiting for
input. Answering and continuing them remains part of issue #5. Other providers,
remote BB environments, and automatic model substitution are not supported by
this worker.

## Prepare the local test

Build the pinned image from this repository:

```bash
container build -f containers/worker.Dockerfile -t software-factory-worker:0.154.0 containers
```

Add a BB custom ACP agent with these fields, preserving existing agents:

```json
{
  "id": "software-factory-worker",
  "displayName": "Software Factory Worker",
  "command": "/ABSOLUTE/PATH/TO/software-factory",
  "args": ["worker-acp"]
}
```

The parent agent must have `FACTORY_PROTECTED_WORKER=1` in its environment.
Use the absolute executable path from `command -v software-factory`. Keep the
worker's repository environment the same as the parent environment.

Run the local protection checks without a login or model request:

```bash
FACTORY_LIVE_CONTAINER=1 uv run pytest tests/live/test_worker_permissions.py
```

These checks passed on 10 September 2026 for Builder and read-only roles. They
verify allowed edits, denied protected-file edits, unchanged dummy secrets and
evidence, and confirmed container exit. Simulated provider tests separately
cover exact turn matching, duplicate requests, child authorization, and crash
cleanup. They do not replace authenticated acceptance.

## Remaining acceptance

Use a disposable copy of `examples/demo-target`, with dummy data only. Register
the worker, select available Codex models and efforts, run the complete factory,
and retain the saved operation IDs, choices, check output, and final report.
Also test a stopped run and a controller restart. Verify that no owned worker or
container remains active after completion or cancellation.

Automatic approval review rejected the earlier authenticated probe because it
could send workspace and instruction data to an external service without
explicit user authorization. That test remains pending. The proposed test sends
only the disposable demo source, its project instructions, and the test request
to OpenAI through the existing Codex login. Do not treat the local checks above
as approval to run it.
