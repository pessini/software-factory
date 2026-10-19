# Issue 9 verification

Verification dates: 2026-09-19 and 2026-10-19. The independent native controller
and BB controls passed real model workflows in September. In October the native
MCP workflow, Codex desktop setup, and both BB/Codex desktop directions passed.
Router installation and testing remain deferred. Issue 9 remains open for that work;
the full external-model requirement is not implemented.

See the [short visual explanation](factory-explained.html),
[compact live evidence](issue-9-live-acceptance.json), and
[desktop evidence](issue-9-desktop-acceptance.json).

## Environment

The checks used Python 3.14.7, BB 0.43.1, Apple Container 1.0.0, protected image
`software-factory-worker:0.154.0`, and MCP SDK 1.30.0. The outer read-only permission
probe used host Codex CLI 0.155.1. Worker initialization checks App Server 0.154.0.
All new live worker turns selected `gpt-5.6-luna / low`, the lowest effort listed
for that model in the native catalog. Receipts confirmed the model and effort.
Usage totals were not available in those receipts; no cost total is claimed.

The installed editable Factory command was refreshed with the frozen dependency
constraints. The existing Factory Controls plugin was built and reloaded. Its
SDK declaration remains 0.4.47; BB's build runtime reported 0.4.87 and a version
warning. Build and live panel operations succeeded. Existing providers and
historical BB records were preserved.

## Real native workflow

`tests/live/test_independent_workflow.py -k cli` passed in **143.67 seconds**
(`.checks/native-live.xml`: one passed, one deselected). Run
`fct_108a6cd480f7` implemented and tested a small integer-doubling function in a
new temporary Git repository, then reached `ready_for_review`.

The start used a real CLI process. After that process exited, a fresh real MCP
stdio server inspected the detached run until completion. All `BB_*` variables
were removed; the native path called no BB services. BB itself remained open for
other work, so this is not a claim that the application was stopped.

Planner, Builder, and Reviewer each completed one model operation. Every saved
operation had dispatch count one. All three worker receipts confirmed stopped
containers and `gpt-5.6-luna / low`. The test granted no commit or push action.
The raw result remains in `/tmp/factory-issue9-acceptance-native/`; its hash is in
the compact evidence. Later pytest retention removed the temporary target and
controller directory. The exported JSON preserves the status and receipts; a
separate native final-report hash is unavailable.

## Real BB controls

Task **Issue 9 native controls acceptance** (`thr_y86n2fwdxq`) used target
`/tmp/factory-issue9-bb-90c4cdf9` and run `fct_fb6c2110e63c`.

1. `/inspect` created no run. The visible Factory panel saved the Shared choice
   `codex / GPT-5.6-Luna / low`; reloading the page retained it.
2. Starting work while the panel refreshed exposed a lock conflict. The new
   regression test reproduced the exact “active prompt” error. Shared controls
   now wait for the short session operation to finish. Repeating the real start
   during a panel reload succeeded.
3. Planner saved the requested prefix question. The BB task runtime was stopped
   and reopened with `/status`. The same run ID, question, status, operation
   records, work deadline, and overall expiry remained unchanged.
4. BB accepted `/answer fct_fb6c2110e63c:plan:1 {"prefix":"Item: "}`. Planner
   continued its existing conversation, then Builder and Reviewer completed.
   The run reached `ready_for_review` with passing behavior checks.
5. `/continue` did not duplicate existing work. Repeating the identical answer
   through the independent CLI left operations, jobs, deadlines, and final state
   unchanged. The work deadline increased once for the input wait; overall
   expiry stayed fixed.
6. The four model-operation receipts (Planner question, Planner continuation,
   Builder, Reviewer) all recorded the selected model/effort and confirmed exit.
   Each operation dispatched once. BB displayed the final report through `/status`.

The test also exposed a stale answer message: before the detached job resumed,
BB repeated the question even though the answer was saved. A public ACP regression
reproduced it. BB now confirms “Answer saved” during that interval. Waiting
questions are displayed once. The normal suite covers this presentation change.

A separate task, **Issue 9 cancellation acceptance** (`thr_s895snxs9h`), used
`/tmp/factory-issue9-cancel` and run `fct_a47949c35623`. `/cancel` was sent while
Builder had a dispatched operation. BB first returned `stopping`. Independent
inspection then returned `cancelled`; all matching receipts confirmed worker
exit, the active worker reference was cleared, and no review was dispatched.
Direct container inspection also matched each saved operation label and confirmed
stopped state. Both BB tests left no recorded live process. Their protected policy
and quality-check files matched the initial Git versions. Generated source was
retained for inspection.

The BB application was not closed, and other tasks were not stopped. The test
closed only its own task runtime. BB's generic **Agent default / Medium** menu
label does not select Factory worker settings; the **Factory** panel does.

## Codex desktop and cross-interface runs (2026-10-19)

The user approved these runs and the `gpt-5.6-luna / low` worker setting. Versions:
ChatGPT app 26.1013.40934 (Codex), Codex CLI 0.163.0, BB 0.46.1, Apple Container
1.0.0, worker image 0.154.0. The outer Codex assistant used the app default model.

**Native MCP.** `tests/live/test_independent_workflow.py -k mcp` passed in
**58.05 seconds** (`.checks/native-live-mcp.xml`). Run `fct_9560843310cf` reached
`ready_for_review`. Planner, Builder, and Reviewer each dispatched once, and all
three receipts confirmed a stopped worker and `gpt-5.6-luna / low`.

**Control workspace.** `scripts/codex-control-workspace.sh` created a separate
trusted folder for a disposable target. `codex sandbox` in that folder could read
the target but could not write the target or the control folder, and network was
blocked. The setup exposed three problems, now fixed or documented:

1. The user's Codex home sets `sandbox_mode = "danger-full-access"`, so Codex
   ignored the documented `default_permissions` profile. The project layer now
   sets `sandbox_mode = "read-only"`. An untrusted folder ignores that layer and
   silently keeps full access; the skill now tells the assistant to stop.
2. In the desktop app, **Approve for me** replaced the project setting with
   `workspace-write`. The assistant's probe write in the control folder succeeded,
   and it refused the `factory_*` tools under the new skill rule. **Custom
   (config.toml)** applied `read-only` / `never`. A repeated write was denied with
   “Operation not permitted.” The target was unchanged throughout.
3. With approval disabled, Codex denied the MCP calls too. Pre-approving only the
   Factory server's tools (`default_tools_approval_mode = "approve"`) fixed this.

BB 0.46.1 also stopped reading `customAcpAgents` from `~/.bb/config.json`, which
unregistered Software Factory. Moving the same entries to `bb plugin config
provider-acp set customAgents` restored `acp-software-factory`.

| Direction | Run | Start | Question answered | Final view | Result |
| --- | --- | --- | --- | --- | --- |
| BB → Codex | `fct_a0d8190947d1` | BB task message | Codex desktop `factory_answer` (`Item: `) | BB `/status` | `ready_for_review` |
| Codex → BB | `fct_4968c5c750c5` | Codex desktop `factory_start` | BB `/attach`, `/answer` (`Name: `) | Codex desktop `factory_status` | `ready_for_review` |

Each run had six operations, each dispatched once. Each had four model receipts
(Planner question, Planner continuation, Builder, Reviewer). All receipts confirmed
`gpt-5.6-luna / low` and stopped workers. Repeating `factory_start` from Codex with
the same request ID returned `fct_4968c5c750c5`; no new run was created. Generated
code used the chosen prefix and tests. No commit was made, and protected
`quality_check.py` and `.factory/factory.yaml` remained unchanged. No Factory
worker container was left running.

The desktop app was driven with accessibility input; BB tasks were created with
the BB CLI in the target environment, and their messages were sent from the BB
window. BB displayed question choices as `Item: ; Name:`, hiding the trailing
spaces in the options.

## Other checks and evidence limits

| Check | Result |
| --- | --- |
| Shared controls without BB context | Simulated workers complete a workflow and retain its identity on reconnect. |
| BB-to-MCP continuation and MCP-to-BB attachment | Simulated cross-interface answer, continuation, saved settings, and event replay pass. Desktop Codex integration is deferred. |
| Duplicate starts and competing writers | Identical request IDs return one run. Conflicting input fails. A failed competing start can retry after workspace release. |
| Concurrent resume/cancel and recovery | Saved stops win; recovery after dispatch/result capture does not repeat implementation. Uncertain requests need recovery. |
| Real MCP restart | All seven tools are exposed. A cancellation job survives client exit; a new server reads its state. No model call in this check. |
| Native model discovery | Protected image returned its catalog without BB context. No model turn. |
| Protected worker permissions | Read-only and Builder container probes passed; Git data, policy, dummy credentials, and evidence stayed protected. No model call. |
| Outer assistant permissions | A real local read-only sandbox allowed reading source and denied writing it. This does not establish desktop Codex profile enforcement. |
| Required review | Standards and Spec reviews cover the native implementation and follow-up changes; deferred Router work is not counted as complete. |

Run `bash scripts/check.sh` for normal checks. All live flags are disabled there.
The final full run passed: **355 passed, 22 skipped** in **854.65 seconds**, recorded
in `.checks/tests.xml`. The skips are opt-in live cases; they are not counted as
live success. Ruff lint, formatting, Mypy (57 source files), plugin TypeScript,
and shell syntax checks also passed. The 13 warnings came from a third-party
LangSmith asyncio deprecation. The earlier run's obsolete CLI fixture was
corrected without changing its behavior assertions. Both live-discovered
regression cases pass in the final suite. The separate Standards and Spec reviews
reported no new findings in this follow-up.

## Deferred checks

Codex desktop setup and the `-k mcp` native test passed on 2026-10-19 (above).

Router is optional for native work. It is a model connection for protected
workers and can serve requests from either BB or Codex. It is not required to
switch control screens. Router support still needs implementation of the private,
authenticated host/container connection, pinned compatible versions, disabled
fallback/redirect/image substitution, proof of the actual upstream model, and
one authorized external-model workflow. Installing a desktop Router alone would
not configure the isolated worker's separate `CODEX_HOME`.

For a future approved native acceptance run:

```sh
FACTORY_ACCEPTANCE_MODEL=APPROVED_MODEL \
FACTORY_ACCEPTANCE_OUTPUT=/absolute/path/to/evidence \
FACTORY_LIVE_NATIVE=1 uv run --frozen pytest \
  tests/live/test_independent_workflow.py -k mcp
```

The native cases use generated temporary source, low effort, six-minute limits,
and no repair or revision rounds. A skipped or failed case is not a passing live
result. Select and authorize the external provider/model before Router acceptance.
