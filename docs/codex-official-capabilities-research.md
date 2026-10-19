# Codex desktop compatibility: official capability evidence

Research date: 2026-09-17. This note covers OpenAI capabilities. The [project feasibility review](codex-app-feasibility.md) establishes what this repository already implements and records its local test results. No live model calls or integration tests were performed for this capability note.

Current official documentation describes Codex inside the ChatGPT desktop app. This report uses “Codex desktop” to include that documented surface. Installed versions and workspace policy can affect availability.

## Findings

| Question | Evidence and consequence |
| --- | --- |
| Can the app run local project commands? | **Verified.** Local environments support setup scripts and named actions. Actions run in the integrated terminal. This is a supported entry point for an existing project command. It does not replace that command's dependencies. [Local environments](https://learn.chatgpt.com/docs/environments/local-environment) |
| Can the project supply reusable instructions? | **Verified.** A skill contains `SKILL.md`, with optional scripts and resources. Repository skills are discovered under `.agents/skills`; personal skills can live under `~/.agents/skills`. A project-specific skill can tell Codex how to start and inspect a factory run. [Build skills](https://learn.chatgpt.com/docs/build-skills) |
| Can the app call a local project service? | **Verified.** Custom MCP servers can use a local process over STDIO or Streamable HTTP. Add them through Settings → MCP servers or configuration. App, CLI, and IDE share the configuration. Project configuration requires a trusted project. [Model Context Protocol](https://learn.chatgpt.com/docs/extend/mcp) |
| Can this be packaged for installation? | **Verified.** A plugin can package skills and MCP connections. Local marketplaces support development and testing. This requires a valid package; opening a Python repository does not install it as a plugin. [Build plugins](https://learn.chatgpt.com/docs/build-plugins) |
| Can Python control Codex workers? | **Verified.** The stable `openai-codex` Python SDK uses the local app-server protocol and includes a pinned CLI runtime. It requires Python 3.10 or newer and provides synchronous and asynchronous clients. For this repository's package workflow, installation would use `uv add openai-codex`. This command is a proposal, not a completed change. [Codex SDK](https://learn.chatgpt.com/docs/codex-sdk) |
| Can a worker return a checked data shape? | **Verified.** `codex exec --output-schema` requests a final result that matches JSON Schema. `--json` streams run events. Saved authentication is reused, and sessions can be resumed. An adapter must still validate results against the factory's own data types. [Non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode) |
| Does app-server support deeper integration? | **Verified.** It provides conversation history, streamed events, approvals, thread creation, and turn execution. `turn/start` accepts `outputSchema` for that turn. The client must implement the protocol and handle failures and approval requests. [Codex App Server](https://learn.chatgpt.com/docs/app-server) |
| Will programmatically created workers automatically appear as desktop tasks? | **Unknown.** The inspected public documentation does not promise this behavior. The app-server has stored thread APIs and source filters, but that does not establish desktop sidebar behavior for a separately launched process. Treat native task visibility as a separate acceptance test. [Codex App Server](https://learn.chatgpt.com/docs/app-server) |
| Can the app directly host this project's ACP server? | **Not established.** Searches of the official documentation for ACP and Agent Client Protocol did not find a documented desktop host interface. MCP and app-server have documented interfaces. Absence of a documented ACP path is not proof that no future or private support exists. |

## Local execution and persistent state

Codex can create a Git worktree for an isolated task. Worktrees have separate files but share Git metadata. Local environment setup scripts run when the app creates a new worktree, so dependencies and required files need explicit setup. [Worktrees](https://learn.chatgpt.com/docs/environments/git-worktrees), [Local environments](https://learn.chatgpt.com/docs/environments/local-environment).

**Design inference:** A factory that owns durable run state should decide where that state lives before using temporary worktrees. Use stable run identifiers and an explicit state directory. Do not assume the current directory is constant across resumed tasks. Decide whether the desktop app or the factory creates each implementation worktree; two independent owners make cleanup and branch ownership harder to reason about.

The app supports scheduled tasks in the local project or a worktree. It can return to an existing chat or start independent runs. Local scheduled work requires the computer and app to stay running. Skills and plugins can be used by scheduled tasks. Runs use the configured sandbox and are unattended; their approval behavior depends on workspace policy. [Scheduled tasks](https://learn.chatgpt.com/docs/automations).

**Design inference:** A schedule can start or check a factory run. It does not supply the factory's saved graph state, retry policy, duplicate-run protection, or approval records. Keep those in the project if they are part of its requirements. Test recovery after app shutdown and computer restart separately.

## MCP boundary for long jobs

The documented MCP tool timeout is 60 seconds by default, and startup timeout is 10 seconds. Both are configurable. STDIO configuration can set a command, arguments, environment variables, and working directory. [Model Context Protocol](https://learn.chatgpt.com/docs/extend/mcp).

**Proposed design:** Expose narrow operations such as `start_run`, `get_run_status`, `get_run_artifacts`, `resume_run`, and `cancel_run`. Starting a run should quickly return a durable run ID. The factory should continue independently and store its result. A tool timeout must not silently start duplicate work on retry. Pass approval decisions through explicit operations that check the intended run and pending step.

This is a proposed wrapper, not a claim that those tools already exist. A skill can use existing CLI commands first; MCP is useful when typed inputs, clear status results, and repeated use justify the additional service.

## Worker interface selection

The repository review confirms that the protected worker already uses raw App Server pinned to CLI 0.154.0. The options below are alternatives, not missing prerequisites. Retaining that transport is the lowest-change starting point.

| Route | Suitable use | What remains to implement |
| --- | --- | --- |
| Existing CLI command from a desktop task | First compatibility trial | Environment setup, dependency checks, permissions, result inspection |
| Skill around existing commands | Repeated manual use | Instructions, input rules, status and recovery procedure |
| MCP wrapper, optionally packaged as a plugin | Reusable desktop integration | Tool contracts, durable run storage, timeout and duplicate-call handling |
| `codex exec` worker adapter | Small initial worker replacement | Process lifecycle, output schema, event parsing, timeout, resume mapping |
| Python SDK worker adapter | Python integration with richer control | Factory-specific role mapping, cancellation, output conversion, approvals, saved worker IDs |
| Raw app-server client | Custom client with detailed event control | Full protocol handling and version compatibility |

The official SDK documentation says the former `codex mcp-server` command and `codex-mcp-server` binary have been removed. Do not base a new integration on older examples that start Codex in that way. This is separate from exposing this factory as an MCP server for Codex to call. [Codex SDK](https://learn.chatgpt.com/docs/codex-sdk).

## Permissions and approval semantics

Codex applies filesystem and network restrictions to spawned shell commands as well as file operations. Approval policy determines when a request can cross those restrictions. A command launched from Codex does not automatically gain access to other project directories, credentials, or network services. [Sandbox](https://learn.chatgpt.com/docs/sandboxing).

**Design inference:** A factory-level approval and a Codex execution approval answer different questions. “Approve the implementation plan” does not grant network access or filesystem access. Preserve the factory's approval records, and define how worker requests for additional access reach a person or fail cleanly. Avoid claiming that a plugin makes an external service obey the desktop shell sandbox: the service's own permissions need separate verification.

## Required acceptance tests before claiming full compatibility

1. Start a documented offline or mock run from a Codex local task and inspect all expected output files.
2. Repeat from a fresh app-created worktree with the proposed setup script.
3. Run one authorized live worker task through the selected Codex interface; verify the actual output against the factory contract.
4. Test worker failure, cancellation, approval denial, timeout, and resumed execution without duplicate work.
5. Restart the relevant processes and confirm that run state and worker IDs remain usable.
6. If native desktop worker tasks are required, verify their appearance, live status, resume behavior, and isolation in the installed app. Do not infer these from JSON thread IDs alone.

These are proposed tests. This research did not run them, install a plugin, change user configuration, or start a scheduled task.
