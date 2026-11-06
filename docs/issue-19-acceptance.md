# Issue 19 production packaging and cutover evidence

The first sections preserve the original packaging candidate's evidence. See
[the follow-up results](#follow-up-independent-brokers-and-release-gate-execution)
for the final image and current gate status. Python/LangGraph remains available,
including its default CLI path and original-run recovery environment.

## Inventory and reproducibility

On 2026-11-05, GitHub main was revalidated as
`a9358d80fde74bf4ebfa5f7e967f2c98d8a515f0`, the branch's agreed review baseline.
No intervening main behavior needed to be added to the parity inventory.
Issues 16, 17 and 18 were closed when checked. Their evidence remains relevant,
with the limitations recorded in each record:

- [#16 durability and schema guards](issue-16-acceptance.md).
- [#17 workflow parity, protected workers and authorized live-provider runs](issue-17-acceptance.md),
  including Codex 0.154.0 / gpt-5.6-luna / low. These are native-controller live
  records, not proof of the new production controller container.
- [#18 interface/planning parity](issue-18-acceptance.md) and its
  [real-Docker, simulated-provider cross-client record](acceptance/issue-18.json).
  The browser gate in that record remained open despite issue closure.

The [deployment runbook](production-deployment.md) defines the topology and exact
commands. `containers/rust-runtime.Dockerfile`, `containers/worker.Dockerfile`,
Cargo.lock and rust-toolchain.toml pin the application/toolchain/provider inputs.
Retain the resulting image digest because Debian security packages can change
between rebuilds. CI keeps Python behavior coverage and Rust formatting,
typechecking, Clippy, full tests, legacy-container durability, protected-worker
acceptance and the new production Compose acceptance.

## Automated evidence

Public seams agreed for this work: HTTP health/lifecycle, deployment and snapshot
commands, and the existing Docker acceptance harness.

- `tests/deployment.rs`: unauthenticated minimal health responses with Host checks,
  actual container-style listener configuration, secret-free configured-token
  startup logs and successful SIGTERM exit. Local networking is required.
- `tests/unit/test_deployment_snapshot.py`: committed data resident in the WAL,
  whole receipt/checkout recovery, and refusal to overwrite existing state.
- `scripts/accept-rust-production.py`: real Compose controller image, host socket
  group/non-root identity, stable same-path binds, saved planning/approval state
  across clean-directory restore (same planned revision/hash awaiting approval), exact revision approval, implementation checks
  and report, actual in-flight provider-turn controller recreation, operation
  dispatch counts, bounded catalog admission, SSE reconnect/replay, cancellation
  remaining `stopping` until cleanup, and worker mount/resource/cleanup inspection. Provider is
  explicitly simulated. Generated evidence is `.checks/issue-19-production.json`.

An initial arbitrary-timing recreation landed in the saved Docker-create window
before any confirmed output. The run correctly retained ownership and became
`recovery_needed` without blindly resubmitting. Automatic progress through every
create/attach ambiguity is **not** claimed. The deterministic restart case pins
the crash after the Builder provider turn ID has been saved. Preserve this
distinction when assessing the host-restart and live-provider release gates.

A second probe found that the existing native-broker assumption checked surviving
worker logs only once after controller-container death. The worker was still
running, so recovery stopped it before its output arrived. A delayed-restart probe
completed the same operation, confirming the cause. Recovery now observes the
same identity/labels until completion or the saved deadline, checks cancellation,
and never starts another turn. The production acceptance keeps the immediate
restart as the regression; no diagnostic delay remains.

Final validation passed on 2026-11-05; see the [saved production record](acceptance/issue-19.json).

| Check | Result |
| --- | --- |
| `bash scripts/check-rust.sh` | Formatting, all-target typecheck, Clippy; **57 passed**, 4 opt-in tests ignored |
| `bash scripts/check.sh` | Lint, formatting, mypy; **378 passed**, 22 opt-in tests skipped |
| `cargo test --locked -p factory-runtime --test docker_worker docker_quality -- --ignored` | **2 passed**, real Docker quality/timeout/output/cleanup regressions; no provider calls |
| `scripts/accept-rust-container.py --image software-factory-controller:issue-19` | **6 cases passed**, real production image, volumes, abrupt deaths and dispatch recovery |
| `scripts/accept-rust-production.py` | **Passed**: restored exact plan hash/revision, 5 implementation operations each dispatched once, 10 stopped worker inspections, SSE replay, in-flight recreation, cancellation and deterministic catalog admission |

Tested Rust is 1.99.1, ADK revision is
`bd1bd5f450a6489d08e14220809033401282ed67`, Docker Engine is 29.1.3, and the
retained Python checks used 3.14.10. Controller image ID is
`sha256:7213c322cd6694dd6792ac0b0d1fcbcfa1118281d66e71eecd262fe964d5af39`.
The JSON records the simulated worker image ID and final source hashes; its
`source_base` is the implementation commit before the reviewed corrections.
Main was checked again after final acceptance and remained at the SHA above.

## Standards

No outstanding findings. The review corrected recovery's deadline clamp to the
saved current run budget and replaced a scheduling-dependent catalog concurrency
test with a deterministic external-provider gate. Final re-review passed.

## Spec

No outstanding actionable implementation findings. The review corrected unbounded
catalog admission and changed the backup acceptance from a saved interview to a
completed exact plan awaiting approval. Final re-review confirmed both fixes.
The documented release-evidence gap below remains open.

Standards: **0 findings**. Spec: **0 actionable code findings, 1 release-evidence gap**.

## Gates remaining before production cutover

| Gate | Required evidence |
| --- | --- |
| Fresh Linux host installation | Follow the runbook without pre-existing images/cache; verify clients and UI |
| Real-provider production topology | Authorized planning → exact approval → implementation → test/review → report using the production controller and worker images; record model/settings/image IDs |
| Browser client parity | Visual browser journey against the packaged service; browser inventory in this environment returned no enabled apps or browsers |
| Actual host restart | Reboot a dedicated acceptance host with saved approval, session, evidence, ownership and worker receipts; inspect exact reconciliation and cancellation cleanup |
| Rollback and legacy recovery | Exercise prior-image rollback and whole-snapshot recovery on a dedicated host; inspect an existing Python run using its retained original environment |
| Final main revalidation | Repeat the SHA/parity comparison immediately at cutover, then rerun affected acceptance |

No deployment, host reboot, remote publication or removal of legacy recovery is
performed by the automated acceptance. Do not declare issue 19's production
release gate passed until these records exist. Keep these gates in issue 19;
Router #13 and Wayfinder #15 remain outside scope.

## Follow-up: independent brokers and release-gate execution

The preceding section records the original packaging candidate merged in PR #23.
It is superseded by the results below for the follow-up branch based on
`17b296fc0da56cc922a706e417392944632f9c96`.

Actual provider testing exposed a gap in the original simulation: killing the
controller closed the native broker's stdin, so Codex interrupted the turn. The
fixture now reproduces that EOF behavior. Production uses a separately confined
Docker broker, pinned to the controller image, with a durable container ID. It
survives controller recreation without dispatching another provider turn.
Recovery also rechecks completed receipts after asynchronous liveness probes and
confirms auto-removed brokers by their saved Docker ID. Identity errors still
fail closed. These fixes were independently reviewed.

| Gate/check | Follow-up evidence |
| --- | --- |
| Final runner simulated regression | [Production record](acceptance/issue-19-production-restart.json): final runner passed recreation, restore, dispatch, isolation and cancellation checks |
| Real provider in production topology | [Live record](acceptance/issue-19-live-restart.json): Codex 0.154.0, explicitly selected gpt-5.6-sol/low, 600s step/3600s run budgets; exact approval, restored plan, all five implementation operations, controller SIGKILL during an actual Builder turn, surviving broker, report, SSE replay and cancellation passed |
| Fresh Linux installation | [Clean-host record](acceptance/issue-19-clean-host.json): checksum-verified Ubuntu Noble VM, initially empty Docker cache, exported immutable artifacts installed, full Compose acceptance passed using explicit software-emulation budgets |
| Actual host restart | Same record: three actual guest reboots, four distinct boot IDs; saved question/deadline, exact plan awaiting approval, restored workflow and native implementation question ownership survived; dispatch counts stayed one; explicit cancellation cleanup confirmed |
| Prior-release rollback | [Rollback record](acceptance/issue-19-rollback.json): whole pre-broker snapshot restored, original production image healthy, saved result/report available |
| Existing clients | [Client record](acceptance/issue-19-clients.json): CLI reads same session; MCP reconnects twice; ACP attaches original run |
| Legacy recovery | Retained Python environment: 13 focused saved-run/history recovery checks passed |
| Rust validation | Formatting, all-target typecheck and Clippy passed; 57 tests passed, four opt-in tests ignored |
| Python validation | Full rerun: 378 passed, 22 skipped. An earlier run hit a negative elapsed-duration assertion in an unchanged legacy report test; its focused rerun and the complete rerun passed |
| Protected native worker regression | All 13 scenarios passed, including cancellation, timeout and cleanup |
| Browser client | [Browser record](acceptance/issue-19-browser.json): user completed the fresh final-image journey; public HTTP inspection confirmed the saved implementation and report are ready_for_review |

The final product image is
`sha256:680bdec93e13a68d84f1b111a765ff0edb2f585790af0ef677860b86ebbeba09`,
built from product source at `830e5f6153597ab5431a35e2d7ed2348c78d31f2`.
Subsequent acceptance-runner changes expose explicit model/time budgets and apply
the run budget consistently to planning polling; they do not change that image.
The JSON records retain the source hashes actually used for each execution.

Earlier live Luna attempts hit bounded interview/Builder deadlines or produced an
invalid ready plan with unsettled decisions. Those attempts did not pass and did
not trigger an automatic model fallback. The successful Sol run explicitly chose
the catalog's default model. Model latency/output remains a bounded workflow
failure, not evidence of universal provider reliability.

The first manual browser attempt failed authentication through a private-file
preview. A localhost launcher addressed that delivery issue. The second reached
the interview but used a stale controller image and expired after the answer.
That fixture was replaced; the final-image HTTP preflight passed before asking
for another manual check. These earlier attempts are not counted as passes.

The clean VM test installed build-host artifacts; it does not claim an independent
source build inside the VM. Host shutdown can interrupt an active external turn;
the reboot tests prove saved-state/ownership recovery, not transparent continuation
of every provider turn through host power loss. Unknown dispatches remain owned
for explicit reconciliation. Rollback uses a complete pre-upgrade snapshot because
an older binary does not understand the new broker configuration field.

All recorded acceptance gates passed. Immediately before final publication,
GitHub main was revalidated at `17b296fc0da56cc922a706e417392944632f9c96`, the
follow-up branch's base; no intervening changes require parity reconciliation.
This completes release acceptance for the pinned artifacts. No real production
workspace was migrated, and the Python recovery environment remains available.
Any later main change or production installation still requires the runbook's
source/image and workspace checks.
