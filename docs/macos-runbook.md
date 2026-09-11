# macOS runbook

This setup uses the [protected local worker](protected-worker.md), verified with
BB 0.42.1 and Apple Container 1.0.0. Build the worker image and register both the
Factory parent and `software-factory-worker` ACP agent before starting a run.
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

The factory uses a standalone `bb` executable when one is on `PATH`; otherwise
it automatically uses `npx --package bb-app bb`. Override this only when needed:

```bash
export FACTORY_BB_COMMAND="/absolute/path/to/bb"
```

Authenticate a supported coding provider through that provider's normal CLI.
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
uv tool install --editable .
command -v software-factory
```

This uses `uv`; the project never invokes `pip install`.

## 3. Register the ACP provider

Merge this entry into `~/.bb/config.json`, replacing the command with the
absolute path printed above:

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

1. Start BB with `npx bb-app@latest` and open the target repository.
2. Create a thread in a new managed worktree.
3. Select **Software Factory** as the provider.
4. Submit a bounded feature request with observable acceptance criteria.

The parent thread shows graph phases and tool activity. Planner, Builder, and
Reviewer appear as visible child threads in the same managed worktree. The
successful result is `ready_for_review`. A run can also finish as `failed`,
`blocked`, or `cancelled`, or pause as `waiting_for_input` or `recovery_needed`.
Old `accepted` records do not prove Phase 1 acceptance.

Version-2 artifacts are written to `$BB_THREAD_STORAGE/factory/artifacts/<run-id>/`; durable checkpoints and the
side-effect ledger are written to
`$BB_THREAD_STORAGE/factory/checkpoints.sqlite`. No LangSmith account is used.

## 6. Operate a run

Inside the owning BB thread environment:

```bash
software-factory doctor
software-factory inspect fct_...
software-factory resume fct_...
software-factory answer fct_... INTERACTION_ID '{"question-id":"answer"}'
software-factory retry fct_...
software-factory cancel fct_...
```

In the BB parent task, use `/status`, `/continue`, `/retry`,
`/answer INTERACTION_ID ANSWER`, or `/cancel`. Use `/new REQUEST` only after the
current run ends. Set model choices through the Factory panel; `/settings` is
not a supported task command. Stop the parent task to release a connection that
BB still shows as active after cancellation; inspect its queue before sending
anything again.

`resume` reuses persisted specialist thread IDs, preserves the original absolute
run deadline, and reconciles a child by its
run-scoped title if a process crashed before saving the returned ID. `cancel`
persists cancellation before stopping the active BB child or process group, so
the running workflow cannot advance afterward; it leaves the worktree intact
for inspection.

## Current verification boundary

Automated tests cover clean success, deterministic repair, bounded routing,
invalid-envelope correction, preservation of unexpected file/index/reference
changes, ignored-file checks without Git object writes, evidence integrity, in-process and cross-process
cancellation (including spawn races), and crash-after-spawn reconciliation. A
real-provider run requires the installed protected worker, a running local BB
server, and an existing provider login. Follow [issue #6 acceptance](acceptance.md)
to run and record those checks. Passing the simulated suite alone does not mark
the release usable.
