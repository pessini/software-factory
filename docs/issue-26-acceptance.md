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

## Standards

The initial review identified missing read-only protection for the new workspace
service lock in Docker builders. It is now protected in both execution backends.
Follow-up review: **0 outstanding Standards findings**.

## Spec

The initial review identified that same lock issue and a hidden host Cargo
requirement in Compose client examples. Both were corrected. Follow-up review:
**0 outstanding implementation findings**; **1 operational acceptance gate**
remains: new clean-host and explicitly authorized real-provider validation.

Standards: 0 outstanding findings. Spec: 0 implementation findings; clean-host/live
operational acceptance remains incomplete.

## Remaining release gates

This change does not certify a clean-host install or production cutover. Follow
both [quickstarts](startup.md) on newly provisioned supported hosts, retaining
source SHA, image digests, exact prerequisite versions/commands, health, browser
and actual BB/Codex connection evidence. Run a real planning → exact approval →
implementation → checks/reviews → report workflow only with explicit provider
account authorization. Validate Docker-free execution separately on a host with
persistent namespace/cgroup enforcement and a pinned standalone provider.

No new billable provider acceptance has run in this task without that explicit
authorization. Production cutover (#27) and complete legacy recovery retirement
remain separate operational milestones.
