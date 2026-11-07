# Factory interfaces

The installed Rust `software-factory` CLI, BB ACP and Codex MCP connect to one
Rust service. See the [native product guide](native-product.md) for startup,
authentication, storage, upgrade and the complete command inventory.

Register BB's ACP agent with an absolute native executable and arguments
`["acp", "--workspace", "/absolute/target"]`. Configure the same service token,
origin and client presentation directory for the BB server and settings panel.

For Codex MCP:

```sh
codex mcp add software-factory -- /absolute/bin/software-factory \
  mcp --workspace /absolute/target
```

Set `FACTORY_HTTP_TOKEN` and, if needed, `FACTORY_CONTROL_URL` in the server's
environment. `factory_start/status/answer/resume/cancel/models/settings` and all
existing `factory_plan_*` names are preserved. Workspace selection is fixed in
registration; tool arguments cannot select paths or execution configuration.

Save `event_position` and send `after_event` on status calls. Disconnecting,
reloading a chat or reading status does not resume or cancel a run. ACP uses
`/attach RUN_ID`, `/status`, `/continue`, `/answer ID ANSWER`, `/cancel`, and
`/plan`, `/plans`, `/plan-status`, `/plan-answer`, `/plan-revise`, `/plan-approve`.
ACP protocol cancellation ends presentation; only explicit `/cancel` stops work.
Old Python chat presentation databases remain in isolated legacy recovery;
attach saved Rust run IDs from a new native chat.

## Enforced control-only setup

Use a separate **control workspace** for the Codex app and CLI. It is a folder
outside the target whose project configuration sets a read-only sandbox, registers
the Factory MCP server for one fixed target, and installs the `factory-control` skill:

```sh
bash scripts/codex-control-workspace.sh --target /abs/target --control /abs/control
```

Open the control folder in Codex, not the target, and **trust** it. Codex ignores
an untrusted folder's `.codex/config.toml`; the session then silently uses the
user-level sandbox, which may be full access. Confirm the effective setting without
a model request:

```sh
cd /abs/control && codex debug prompt-input hi | grep -o 'sandbox_mode` is `[a-z-]*`'
```

The result must be `read-only`.

In the Codex desktop app, open the folder with `codex app /abs/control` and set the
chat's permission menu to **Custom (config.toml)**. The other app modes (**Ask for
approval**, **Approve for me**, **Full access**) replace the project setting with
workspace write; a tested chat under **Approve for me** could write the control
folder. The generated configuration keeps `approval_policy = "never"`, so it also
sets `default_tools_approval_mode = "approve"` on the Factory server only.
Without it, Codex denies every `factory_*` call because approval is disabled.

The project layer sets `sandbox_mode` rather than a
permission profile because Codex ignores `default_permissions` whenever any loaded
layer sets `sandbox_mode`, as a user-level `danger-full-access` default does.
The skill also tells the assistant to stop if its permission text is not read-only.

A permission profile is still valid for a Codex home that sets no `sandbox_mode`.
For the tested CLI, a profile file such as `~/.codex/factory-control.config.toml`
can contain:

```toml
default_permissions = "factory_control"
approval_policy = "never"

[permissions.factory_control.filesystem]
":root" = "read"

[permissions.factory_control.network]
enabled = false
```

With either method, the MCP server runs as a trusted local tool process outside the
assistant's shell sandbox. Only its fixed target
and shared control methods may perform Factory work. Do not grant extra writable
roots or separate write-capable target tools to this control-only assistant.
The skill describes routing; the filesystem profile enforces the write boundary.

The local permission probe uses `codex sandbox -P factory_control` with the same
filesystem and network settings. It attempts a real target write and checks that
the original file remains unchanged. See `tests/live/test_control_permissions.py`.
This command shape was checked against local Codex CLI 0.155.1; the protected
worker remains pinned to 0.154.0.

## Current support boundary

The Rust service uses the protected Docker worker boundary with pinned Codex App
Server 0.154.0. Native ACP and MCP do not require a Python SDK. Router remains
separate work. See [Rust workflow](rust-workflow.md) and
[entry-point validation](issue-25-acceptance.md). Earlier Apple Container/Python
interface acceptance remains historical in [issue 9](issue-9-acceptance.md).
