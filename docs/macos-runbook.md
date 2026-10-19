# macOS runbook

New runs use Factory-owned protected workers. The current checks use BB 0.43.1,
Apple Container 1.0.0, and worker image `software-factory-worker:0.154.0`.
BB is an optional control screen. It does not own new worker processes or records.
The repository targets Python 3.14 and manages dependencies with `uv`.

## 1. Check existing prerequisites

Do not reinstall tools that are already present:

```bash
node --version       # BB currently supports Node 22.19, 24, or 26
git --version
uv --version
```

If one of those commands is genuinely missing, use Homebrew:

```bash
brew install node git uv
```

BB itself currently has no Homebrew formula or cask. Its official npm package
contains both the launcher and the `bb` CLI, so no global npm install is needed:

```bash
npx bb-app@latest
# In another terminal, against that running server:
npx --package bb-app bb --help
```

The installed macOS application also includes the CLI:

```bash
node /Applications/bb.app/Contents/Resources/app.asar.unpacked/node_modules/bb-app/dist/bb.js --help
```

Native Factory execution does not need the BB CLI. The old BB recovery path can
still use `FACTORY_BB_COMMAND` to select a different BB executable.
Authenticate the native Codex provider through its normal CLI.
The protected worker mounts the existing Codex login read-only; it never copies
it into factory configuration, Git objects, or evidence.

## 2. Build and verify the factory

From this repository:

```bash
bash scripts/check.sh
uv run software-factory graph --format mermaid
```

Expose the stable command path BB will launch:

```bash
uv export --frozen --no-dev --no-emit-project --no-hashes \
  --format requirements-txt --output-file /tmp/factory-constraints.txt
uv tool install --editable . --constraints /tmp/factory-constraints.txt
command -v software-factory
```

This uses `uv`; the project never invokes `pip install`.
For an existing tool installation, add `--reinstall` so new locked dependencies,
including the MCP server, are installed. Build and check the worker image using
the commands in [protected worker setup](protected-worker.md). The older worker
ACP registration in that document is needed only for legacy BB runs.

## 3. Register the ACP provider

BB 0.46 and later no longer read `customAcpAgents` from `~/.bb/config.json`.
Register the agent through the ACP provider plugin, replacing the command with
the absolute path printed above:

```bash
bb plugin config provider-acp set customAgents '[{"id":"software-factory","displayName":"Software Factory","command":"/ABSOLUTE/PATH/TO/software-factory","args":["acp"],"env":{"FACTORY_LOG_LEVEL":"INFO"}}]'
bb provider list
```

`bb provider list` must include `acp-software-factory`. On BB 0.46 also set
**Software Factory executable** in the Factory panel's plugin settings; the panel
can no longer read the command from `config.json`.

Before BB 0.46, merge this entry into `~/.bb/config.json` instead:

```json
{
  "customAcpAgents": [
    {
      "id": "software-factory",
      "displayName": "Software Factory",
      "command": "/ABSOLUTE/PATH/TO/software-factory",
      "args": ["acp"],
      "env": {
        "FACTORY_LOG_LEVEL": "INFO"
      }
    }
  ]
}
```

Apply the configuration to a running BB server:

```bash
npx bb-app config refresh
bb plugin reload provider-acp
```

BB prefixes the configured ID, so the provider is internally named
`acp-software-factory` and displayed as **Software Factory**.
Install or reload the [Factory panel](../bb-plugin-factory-controls/README.md).
There is no worker ACP registration requirement for new native runs.

## 4. Prepare a target repository

Generate policy, then review every command and protected path before committing:

```bash
cd /path/to/target-repository
software-factory init
software-factory repo-check
git add .factory/factory.yaml
git commit -m "Configure deterministic software factory checks"
```

The graph requires a clean worktree at bootstrap. `.factory/runs/` should be
ignored. Quality gates must be fixed, verification-only `argv` arrays; direct
shell-interpreter and Git invocations are rejected by the schema. The invoked
programs are an operator-controlled trust boundary, so wrappers and test scripts
must not publish or mutate Git metadata indirectly.

For BB-managed worktrees, add a setup hook like this:

```bash
#!/usr/bin/env bash
set -euo pipefail
uv sync
software-factory repo-check
```

The demo fixture already contains the policy, hook, and `.worktreeinclude`.

## 5. Run from BB

1. Open the installed BB app, or start it with `npx bb-app@latest`.
2. Create a thread in a new managed worktree.
3. Select **Software Factory** as the provider.
4. Open **New tab → Factory** in the right panel and choose the worker models.
5. Submit a bounded feature request with observable acceptance criteria.

Use `/inspect` as the first message if you want to create the task and set its
choices before starting work. It does not start a model request. The Factory
panel controls the actual worker model and effort; BB's general conversation
menu can still show **Agent default / Medium** for this ACP provider.

Factory returns the run ID. Use `/status` to read saved phases, questions, and
results. Planner, Builder, and Reviewer run in protected containers; BB child
tasks are not required. The successful result is `ready_for_review`.
A run can also finish as `failed`,
`blocked`, or `cancelled`, or pause as `waiting_for_input` or `recovery_needed`.
Old `accepted` records do not prove Phase 1 acceptance.

New records are in `~/Library/Application Support/software-factory/factory/`
on macOS: `checkpoints.sqlite` stores saved state and `artifacts/<run-id>/`
stores evidence. Use `FACTORY_HOME` or global `--storage` to select another
private directory outside the target. All control screens must use that same
location. See [storage and access rules](factory-interfaces.md).

## 6. Operate a run

From the target workspace, with or without BB:

```bash
software-factory doctor
software-factory start 'Bounded request' --request-id my-request-1
software-factory inspect fct_...
software-factory resume fct_...
software-factory answer fct_... INTERACTION_ID '{"question-id":"answer"}'
software-factory retry fct_...
software-factory cancel fct_...
```

In the BB task, use `/status`, `/continue`, `/retry`,
`/answer INTERACTION_ID {"question-id":"answer"}`, or `/cancel`. Use `/new REQUEST` only after the
current run ends. Set model choices through the Factory panel; `/settings` is
not a supported task command. `/attach RUN_ID` connects another BB task to the
saved run in the same workspace. Stopping the BB task releases its connection;
use `/cancel` to stop Factory work.

`resume` checks saved operation records and exact worker identities before it
continues. It keeps the saved model choices and overall expiry. An unanswered
question pauses the work budget; answering it once adjusts that budget once.
Repeated identical answers do not dispatch again. `cancel` saves the stop request
before cleanup and reports `cancelled` only after worker exit is confirmed.
The worktree remains available for inspection. Use the
[legacy recovery procedure](interface-migration.md) for old BB-owned runs.

## Current verification boundary

Automated tests cover clean success, deterministic repair, bounded routing,
invalid-envelope correction, preservation of unexpected file/index/reference
changes, ignored-file checks without Git object writes, evidence integrity, in-process and cross-process
cancellation (including spawn races), and crash-after-spawn reconciliation. A
real native run requires the installed protected worker and an existing provider
login. BB is required only when testing the BB control screen. See
[issue 9 verification](issue-9-acceptance.md) for the real native and BB evidence.
Codex desktop registration and external Router support remain separate work.
