# Issue 9 verification

Verification date: 2026-09-19. This change implements the independent native
controller and shared CLI, BB, and MCP controls. It does not complete issue 9.
Router support and live workflow acceptance remain open.

## Environment

The checks use Python 3.14.7, Apple Container 1.0.0, and protected worker image
`software-factory-worker:0.154.0`. The worker checks the Codex App Server version
during initialization. The MCP dependency is locked at 1.30.0. The outer
assistant permission probe uses the installed Codex CLI 0.155.1.

## Evidence and limits

| Check | Result and evidence limit |
| --- | --- |
| Shared controls with BB variables absent | Simulated workers complete a workflow and preserve its identity on reconnect. |
| BB-to-MCP continuation | Simulated workers complete the same saved run after answering its exact pending question. Settings, overall expiry, and dispatch counts remain fixed. |
| MCP-to-BB attachment | Saved event history is attached and replayed once. No model is called. |
| Duplicate starts and competing writers | Simultaneous identical IDs return one run. Conflicting input is rejected. Failed competing starts can retry the original or a new request ID after release. |
| Resume and cancellation | Concurrent commands preserve the saved stop request; queued work sends no new worker request after cancellation. |
| Recovery | Simulated failures after dispatch and result capture recover without repeating implementation. Uncertain requests require recovery. |
| Real MCP connection | The stdio server exposes all seven tools. A detached cancellation job survives client exit and a new server reads its completed state. No model is called. |
| Native model discovery | The protected image returned a native catalog through the CLI with BB context absent. Discovery sends no model turn. |
| Real worker permissions | Both read-only and Builder profiles passed local container file-access probes. Protected policy, Git data, dummy credentials, and evidence remained unchanged. No model is called. |
| Real outer assistant permissions | The local read-only sandbox read a source file and denied a write to it. No model is called. |
| Review | Separate Standards and Spec reviews found defects that were corrected and covered by regression tests. |
| Native workflow through CLI and MCP | Not run. Live model authorization is pending. The opt-in cases are prepared in `tests/live/test_independent_workflow.py`. |
| External Router route | Not implemented or tested. No provider, model, or usage limit has been selected. |

Run the normal checks with `bash scripts/check.sh`. Results are written to
`.checks/`, including the full test report. Normal checks disable every live flag.
The full suite recorded 351 passed, 20 skipped, and one failed CLI fixture in
`.checks/tests.xml`. That fixture still selected legacy BB storage for a new CLI
command. It now creates a Factory context; its original behavior assertions are
unchanged. The final affected-test run recorded 15 passed and two opt-in live
cases skipped in `.checks/retest.xml`. This includes the corrected fixture and
the final shared-control regressions. The full suite was not repeated.

Final Ruff lint, formatting, Mypy, plugin TypeScript checks, and shell syntax checks
passed. The first check command also produced a shell line-offset error because
the live-test flags were edited while that script was running; the final script
passed `bash -n scripts/check.sh`. No test failure or skipped live case is counted
as a successful live workflow.

Run container and outer-assistant permission probes separately:

```sh
FACTORY_LIVE_CONTAINER=1 uv run --frozen pytest tests/live/test_worker_permissions.py
FACTORY_LIVE_SANDBOX=1 uv run --frozen pytest tests/live/test_control_permissions.py
```

## Pending native model acceptance

Approve the selected model, account usage, and generated source destination
before enabling these cases. Each case uses a new temporary Git repository with
synthetic Python source, its own Factory storage, low effort, a six-minute run
limit, and no repair or review-revision rounds. The selected model must support
the required permissions and effort. The request grants no commit or push action.

Run one approved case at a time. `cli` starts through a real CLI process; `mcp`
starts through a real MCP stdio process. Both then close that process and inspect
progress through a new MCP server. Each must reach `ready_for_review` with the
same model, one dispatch per operation, and stopped worker receipts.

```sh
FACTORY_ACCEPTANCE_MODEL=APPROVED_MODEL \
FACTORY_ACCEPTANCE_OUTPUT=/absolute/path/to/evidence \
FACTORY_LIVE_NATIVE=1 uv run --frozen pytest \
  tests/live/test_independent_workflow.py -k cli
```

Use `-k mcp` only after approval for the second run. Keep the generated JSON
records and temporary controller storage. A skipped or failed case is not a
successful acceptance result. These cases do not establish Router support or a
working outer-assistant MCP setup inside the desktop application.

## Pending Router acceptance

Select one external provider/model and an authorized usage limit. Then implement
and test the private authenticated host-to-container connection, pin the Router
and translation dependency versions, and disable fallback, native redirect, and
image substitution. Record actual upstream model and effort from route evidence,
not only the App Server's logical model name. The external route must complete
tool calls, tool results, structured output, continuation, and one isolated live
workflow before issue 9 can be closed.
