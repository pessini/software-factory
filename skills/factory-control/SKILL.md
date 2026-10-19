---
name: factory-control
description: Operate Software Factory runs through its MCP tools. Use to start Factory work, inspect a saved run, answer its questions, continue it, or stop it.
---

Use the `factory_*` tools for the workspace selected when this MCP server was registered. Factory owns implementation, checks, worker cleanup, saved model choices, and delivery authorization.

For new work, choose one request ID and call `factory_start`. Keep that ID if the response is lost. A different request needs a different ID. Save the returned run ID for later connections.

For an existing run, call `factory_status` first. Keep its event position and pass it as `after_event` on later reads. Show pending questions to the user. Send their answers with the exact interaction and question IDs through `factory_answer`.

Use `factory_resume` to continue or reconcile saved work. A status read does not resume it. A `recovery_needed` result requires the saved recovery procedure; a new start does not replace an uncertain operation.

Use `factory_cancel` for an explicit stop request. Report `stopping` until a later status confirms `cancelled`. Closing this conversation or ending a tool call does not stop the run.

Use `factory_models` and `factory_settings` to inspect choices. Saved settings for a started run are fixed. `ready_for_review` means verified changes are ready for human review; it grants no new publication permission.

The outer assistant must use the configured read-only control profile. Route target edits through Factory; do not make a separate implementation in this conversation. This instruction is not a filesystem access control. If the tool connection or enforced profile is missing, report the setup requirement before starting work. Your permission instructions must state a read-only sandbox. If they report full access or workspace write, the control folder is probably untrusted and its project configuration was ignored, or the desktop app's permission menu is not set to **Custom (config.toml)**: stop and report this before calling any `factory_*` tool.
