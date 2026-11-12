# Issue 26 startup validation

Source baseline: `17dc3f9` (includes #25 native product and #28 native execution).
Validation date: 2026-11-09.
Validation host: Linux x86-64 / WSL2, operator UID/GID 1001, Rust/Cargo 1.99.1,
Docker Engine 29.1.3 with local socket, Compose v2 and just. This is an existing
development host, not a newly provisioned clean host.

## Commands and scope

- `cargo test --locked -p factory-runtime --test deployment`: missing backend is
  rejected by both low-level executable routes; explicit fake HTTP fixture checks
  readiness, token secrecy and SIGTERM.
- `cargo test --locked -p factory-runtime --test product`: all 18 passed, including
  browser assets, CLI/MCP/ACP identity, exact planning approval/report and rejection
  of a second service using the same workspace with different storage/port.
- `cargo test --locked -p factory-runtime --test execution`: all seven passed after
  explicitly opting the existing fake recovery fixture into development mode.
- `just --list` and `just --dry-run run request-26 'Build greeting'`: normal
  operations select the Rust product client, with explicit service/demo/helper names.
- Disposable process smoke via `FACTORY_BIN=.../target/debug/software-factory`:
  `just init`, missing-backend `just start` rejection, `just demo-start` readiness
  HTTP 200, `just run`, duplicate request ID, `just status`, `just runs`. Apostrophes
  and literal dollar signs round-trip safely. This is explicitly fake protocol
  coverage, not provider or protection acceptance.

`bash scripts/check-rust.sh` passed formatting, all-target typechecking, Clippy
with warnings denied and **81 tests**; 11 host/provider opt-ins were skipped.
The one-second-budget human-wait test failed once under simultaneous image
compilation; its isolated rerun and the final full suite passed.

Fresh-image Compose validation passed using:

```bash
docker build --build-arg FACTORY_UID=1001 --build-arg FACTORY_GID=1001 \
  -f containers/rust-runtime.Dockerfile -t software-factory-controller:issue-26 .
python3 scripts/accept-rust-production.py \
  --image software-factory-controller:issue-26 --output .checks/issue-26-compose.json
```

An additional disposable copy of that helper exercised the documented retained-image
`docker run --rm --pull never --network host --read-only` client with the target
mounted read-only, token/origin environment and `--entrypoint software-factory`:
`doctor` and `list` both succeeded against the random published host port.
These execute the image's clients without host Cargo or Python product code.
Python only orchestrated the acceptance fixture.

The final [machine evidence](acceptance/issue-26-compose.json) records controller
`sha256:4972a36ce4f800d4b837ed75b224a871dc6e9addb5594aa858347a2782943d64`, worker
`sha256:63ed0cb5491c228b88b1a17fd7ba6528155203c11c77d61b7001521026242903`, source base
`b4e871c98b2036742a7cc6a838615c97f751b2d2` and tested source-file SHA-256 hashes.
The test restored a pending approval with the same hash, approved exactly, completed
to `ready_for_review`, replayed events, checked catalog admission, hard-recreated
the controller during work with the independent broker surviving, and confirmed
cancellation/worker cleanup. This is actual Docker isolation and Compose recovery
with a **simulated provider**, not real-model evidence. Existing #19 Docker and
#28 live native acceptance records remain historical evidence and were not edited.

## Authorized real-provider acceptance

After the user explicitly authorized real provider calls in this chat, both live
runs passed on the same development host and source
`2c8ced1bdc25e458eca61a525fdb0badf2168e57`. No PR publication was authorized or
performed by these fixtures. This is new issue-26 evidence, separate from the
historical #19/#28 records.

Compose used the retained controller image above and worker
`sha256:5f5c3b7fb6c78332c22d2e501b54543410842da511170c149f52e9391c2ab579`:

```bash
python3 scripts/accept-rust-production.py --live --model gpt-5.6-sol \
  --step-timeout-seconds 600 --run-timeout-seconds 3600 \
  --image software-factory-controller:issue-26 \
  --output .checks/issue-26-compose-live.json
```

[Compose live evidence](acceptance/issue-26-compose-live.json) records success,
Codex 0.154.0 / gpt-5.6-sol / low effort, exact planning approval, restore of a
pending approval, a completed `ready_for_review` report, controller hard recreation
with its independent broker surviving, event replay and cancellation/worker cleanup.
Host reboot was not exercised. The controller used the retained Rust image;
image clients were validated separately by the smoke check above. Host Python
drove the live fixture through the HTTP API; it was not the product runtime.

All **seven native opt-ins passed** in a disposable delegated user-systemd scope,
using the already staged standalone Codex 0.154.0 and companion, Bubblewrap 0.9.0,
`gpt-5.6-luna` / low effort and the explicitly configured `on-request` account
policy. The broker continued denying escalation requests. Docker was deliberately
unreachable throughout the native run:

```bash
systemd-run --user --scope --unit=factory-issue26-native-acceptance \
  -p Delegate=yes bash .checks/issue-26-native-live.sh
# The script creates private JSON with its own delegated cgroup path, then runs:
DOCKER_HOST=unix:///tmp/issue26-docker-unavailable.sock \
  FACTORY_NATIVE_TEST_CONFIG="$native_config" FACTORY_NATIVE_TEST_MODEL=gpt-5.6-luna \
  cargo test --locked -p factory-runtime --test native_worker \
  -- --ignored --nocapture --test-threads=1
```

[Native live evidence](acceptance/issue-26-native-live.json) retains the exact
setup script, test names, source/file hashes and provider/bubblewrap hashes.
Three tests use the real provider: read-only planning, builder protection probes,
and planning → stale-approval rejection → exact approval → reopened engine →
implementation → verification/review → final report. The remaining four exercise
actual OS permission/network denials, descendant timeout/cancellation cleanup,
and simulated-protocol question/restart recovery. Those four do not claim real
model behavior. Target Python verifies the disposable application; the native
Rust engine runs without host Python as a product runtime or Docker execution.

## Standards

The initial review identified missing read-only protection for the new workspace
service lock in Docker builders. It is now protected in both execution backends.
Follow-up review: **0 outstanding Standards findings**.

## Spec

The initial review identified that same lock issue and a hidden host Cargo
requirement in Compose client examples. Both were corrected. Follow-up review:
**0 outstanding implementation findings**. Authorized real-provider checks now
pass; clean-host installation and client validation remain an operational gate.

Standards: 0 outstanding findings. Spec: 0 implementation findings; clean-host
operational acceptance remains incomplete.

## Remaining release gates

This change does not certify a clean-host install or production cutover. Follow
both [quickstarts](startup.md) on newly provisioned supported hosts, retaining
source SHA, image digests, exact prerequisite versions/commands, health, browser
and actual BB/Codex connection evidence. The newly authorized real-provider and
Docker-free enforcement workflows above pass on the existing development host;
these do not establish fresh-host installation or production cutover.

Production cutover (#27) and complete legacy recovery retirement remain separate
operational milestones.

## Follow-up recorded 2026-11-12: fresh-host attempt

The [current production record](issue-27-rust-acceptance.md#clean-host-result-still-blocked)
and [machine evidence](acceptance/issue-27-rust-production.json) identify a new
Ubuntu Noble VM with empty initial Docker cache and no host Rust toolchain.
Native installed-binary and retained-image Compose startup, health, CLI catalog
and authenticated browser access passed. This also found and corrected the
justfile's incompatibility with Ubuntu's packaged just 1.21.

Full real-provider workflows **did not pass**: the nested Codex sandbox failed
to perform a mount under the host's confinement policy. The user explicitly
declined the proposed AppArmor change and kept this gate blocked. No sandbox or
global security setting was disabled. Do not treat the successful startup checks,
the working WSL production deployment or historical VM evidence as satisfying
this still-open clean-host workflow requirement.
