# Protected BB worker

This page records the legacy BB-owned worker path. New native runs use
[Factory-owned workers](factory-interfaces.md) with the same container protection.
The image build and permission checks below still apply; worker ACP registration
is required only for legacy recovery. See the [current setup](macos-runbook.md)
and [issue 9 live evidence](issue-9-acceptance.md).

The default adapter supports local BB 0.42.1, Apple Container 1.0.0, and the
Codex provider CLI 0.154.0. The authenticated acceptance tests passed on
10 September 2026. See [the saved evidence](live-worker-acceptance.json).

`doctor` checks the image, worker registration, repository environment, and
model choices. Setting `FACTORY_PROTECTED_WORKER=0` selects the old disabled
adapter for compatibility testing; it does not bypass protection.

## What the worker does

BB starts a visible child using the custom ACP provider
`acp-software-factory-worker`. The message contains the parent task ID and the
saved operation ID. The worker reads the original prompt and fixed model choice
from the parent's controller storage. It checks the child title, parent,
environment, repository, and any previously saved child ID before dispatch.

Each operation gets a named container and a saved creation record. Planner and
Reviewer get a read-only workspace. Builder gets a writable workspace with
read-only mounts for Git metadata, existing protected and ignored directories,
and controller evidence. Protected individual files use the provider's read-only
file rules: mounting them separately with Apple Container can hide the containing
workspace. Live checks cover source visibility and host-visible allowed writes,
as well as denied protected-file operations. The provider's command permissions also enforce
these restrictions and disable command network access. Provider communication
with OpenAI still requires network access outside the command sandbox. Creation
of a new protected path is detected and rejected by the controller after the
operation. Missing paths are not mounted: provider mount placeholders would
otherwise change the host checkout.

The existing Codex login file is mounted read-only into a separate provider
directory. It is not copied into the repository, Git objects, or evidence.
Provider commands cannot read the login file. The provider directory is separate
from the worker's request and result records.

The worker saves the actual provider thread and turn IDs. Only a matching turn
can supply output. A completed request can return its saved result after restart;
an uncertain request is never sent again. Repair and revision can resume a
previous provider conversation when the saved settings match.

BB prepends transport instructions to ACP requests; the factory separates them
from the submitted request. BB also steers a running parent when a child returns.
The worker therefore saves its completed result and waits to finish its ACP turn
until the controller ends, its deadline expires, or a follow-up arrives. The
controller verifies stopped provider containers before it consumes the result.
Parent system notices do not start another factory run.

Cancellation first saves a stop request. Cleanup checks each container's name
and operation label, stops it, and verifies its stopped state. After a worker
crash, the controller must acquire the worker lock before updating its saved
result. A record that proves container creation never started can be stopped
without a container. An uncertain creation remains in recovery and retains
ownership.

Questions from the matching provider turn are saved and reported as waiting for
input. Issue #5 implements saved answers and continuation; see
[session continuation](session-continuation.md). Other providers,
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

Use the absolute executable path from `command -v software-factory`. Keep the
worker's repository environment the same as the parent environment.
After changing the legacy BB registration file, run `bb-app config refresh`
and reload the `provider-acp` plugin so its provider list picks up the new entry.

Run the local protection checks without a login or model request:

```bash
FACTORY_LIVE_CONTAINER=1 uv run pytest tests/live/test_worker_permissions.py
```

These checks passed on 10 September 2026 for Builder and read-only roles. They
verify allowed edits, denied protected-file edits, unchanged dummy secrets and
evidence, deletion, rename, permission changes, hard links, symbolic-link writes, and
confirmed container exit. Simulated provider tests separately
cover exact turn matching, duplicate requests, child authorization, and crash
cleanup. The authenticated checks below add real BB/provider evidence.

## Authenticated acceptance results

The user authorized these tests after the initial approval review requested
explicit permission. Only disposable demo source and test instructions were used.

- Run `fct_9c5911f51ff1` completed Planner, Builder, configured checks, and Reviewer
  with status `accepted`. Plan and Build used `gpt-5.6-luna / low`; Review used
  `gpt-5.6-luna / medium`. The provider reported the matching choices.
- The controller-crash test recovered the same saved provider turn with one
  dispatch, then cancelled and confirmed the child stopped.
- Build and Repair reused one child and one provider conversation. They had
  distinct provider turn IDs, retained the test marker, and produced no parent
  notice before controller cancellation.
- All provider containers in these checks were inspected and confirmed stopped.

The demo and generated policy now use `uv run --no-sync python -B -m pytest -q
-p no:cacheprovider`. Run `uv sync` during repository setup. The quality command
then verifies code without synchronizing dependencies or writing Python/pytest
caches into the protected checkout.

These historical runs prove the earlier worker contract. They predate the
implemented issue #5 continuation and issue #1 Phase 1 evidence review. The
current first-release acceptance is tracked separately in [issue #6](acceptance.md).
