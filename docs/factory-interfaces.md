# Factory interfaces

Factory now owns run storage and protected worker processes. The CLI, BB ACP agent,
and local MCP server use the same control operations. Native execution does not
use BB model discovery, task creation, environment lookup, or storage APIs.

## Local storage and identity

Set `FACTORY_HOME` or use `software-factory --storage /absolute/private/path ...`.
The default is `platformdirs.user_state_path("software-factory")`: on macOS,
`~/Library/Application Support/software-factory`; on Linux,
`~/.local/state/software-factory` unless `XDG_STATE_HOME` is set.
The directory must be owned by the current user, have mode `0700`, and be outside
the target workspace. Worker commands cannot mount this directory as writable.

Each new run saves a context version, Factory session, canonical workspace ID,
local user ID, and checkpoint ID. Checkpoint IDs are `factory:<run-id>`.
Application conversation IDs are not checkpoint or ownership keys.
`--session` selects a Factory session. A control connection can attach to a run
only in its configured workspace and local user's private storage. Global
workspace locks also exclude competing runs that use different storage roots.

## CLI and BB

```sh
software-factory --storage /private/factory start 'Bounded request' \
  --request-id my-request-1 --workspace /path/to/target
software-factory --storage /private/factory inspect RUN_ID --workspace /path/to/target
software-factory --storage /private/factory resume RUN_ID --workspace /path/to/target
software-factory --storage /private/factory answer RUN_ID INTERACTION_ID \
  '{"question-id":"answer"}' --workspace /path/to/target
software-factory --storage /private/factory cancel RUN_ID --workspace /path/to/target
```

Start and resume return saved state while a separate controller process does the
work. Keep the same request ID when retrying an identical start. Status reads have
no dispatch side effect. A stopped client does not stop its controller or workers.
Cancellation is saved before cleanup and stays `stopping` or `recovery_needed`
until matching worker processes and containers have stopped.

BB still registers `software-factory acp`. Set the same `FACTORY_HOME` for BB and
the MCP server. The panel uses Factory storage. In BB use `/status`, `/continue`,
`/answer ID ANSWER`, `/cancel`, `/new REQUEST`, or `/attach RUN_ID`. ACP cancellation
ends the current view; `/cancel` explicitly stops the Factory run. Specialist BB
tasks are no longer required. Use `/status` to refresh progress after reconnecting.
The panel passes the selected BB conversation as an optional interface reference.
Factory maps this reference to its own session so two conversations in the same
workspace retain separate settings. The reference does not authorize run access.

## Codex MCP registration

Register the installed absolute executable, not the ACP command:

```sh
codex mcp add software-factory -- /absolute/repo/.venv/bin/software-factory \
  --storage /private/factory mcp --workspace /path/to/target
```

The stdio server exposes `factory_start`, `factory_status`, `factory_answer`,
`factory_resume`, `factory_cancel`, `factory_models`, and `factory_settings`.
The workspace is fixed in registration, not accepted as a model-controlled tool
argument. These tools return structured data. Save `event_position` and send it
as `after_event` to retrieve missed events without repeating actions.
The normal MCP call does not remain open for the full workflow. Model discovery
and start validation can take several seconds while a protected container starts.

Codex supports command-based MCP servers and timeout configuration in its
[official MCP documentation](https://learn.chatgpt.com/docs/extend/mcp).

## Enforced control-only setup

Install `skills/factory-control` as a Codex skill. Run the outer assistant with a
read-only filesystem profile and no permission to expand it. For the tested CLI,
a profile file such as `~/.codex/factory-control.config.toml` can contain:

```toml
default_permissions = "factory_control"
approval_policy = "never"

[permissions.factory_control.filesystem]
":root" = "read"

[permissions.factory_control.network]
enabled = false
```

Select that profile for the outer assistant. Register the MCP server as a trusted
local tool process outside the assistant's shell sandbox. Only its fixed target
and shared control methods may perform Factory work. Do not grant extra writable
roots or separate write-capable target tools to this control-only assistant.
The skill describes routing; the filesystem profile enforces the write boundary.

The local permission probe uses `codex sandbox -P factory_control` with the same
filesystem and network settings. It attempts a real target write and checks that
the original file remains unchanged. See `tests/live/test_control_permissions.py`.
This command shape was checked against local Codex CLI 0.155.1; the protected
worker remains pinned to 0.154.0.

## Current support boundary

The worker uses Apple Container 1.0.0 and Codex App Server 0.154.0. MCP is locked
to Python SDK 1.30.0. Only the native model connection is enabled in this change.
Codex Router is not yet supported. No external route, private container bridge,
or live routed-model acceptance has been established. Do not configure a wider
router listener or mount host credential directories to work around this gap.
Real CLI and BB workflows passed with `gpt-5.6-luna / low` on 19 September 2026.
The BB check covered settings, start, status, a saved question, task reconnect,
answer, resume, and confirmed cancellation. Issue #9 remains open for the deferred
Codex desktop integration and Router work.
See [the verification record](issue-9-acceptance.md) for tested versions, simulated
results, local permission probes, and the remaining acceptance procedure.
