# Issue 19 production packaging and cutover evidence

This branch delivers the production packaging candidate. **Cutover is not yet
authorized by the evidence below.** Python/LangGraph remains available, including
its default CLI path and original-run recovery environment.

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
  across clean-directory restore, exact revision approval, implementation checks
  and report, actual in-flight provider-turn controller recreation, operation
  dispatch counts and worker mount/resource/cleanup inspection. Provider is
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

Final validation commands/results and the saved production record are added after
the final review and checks.

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
