# Issue 25 native product entry points

Validated on 2026-11-07 with Rust/Cargo 1.99.1 and Docker Engine 29.1.3.

- `cargo install --path rust/factory-runtime --locked --offline --debug --bin software-factory --root /tmp/factory-issue-25-install` installed the native command successfully. It prints `Rust/ADK` with an otherwise empty PATH.
- `bash scripts/check-rust.sh` passed formatting, typechecking, Clippy with warnings denied, and all 74 Rust tests (including 17 native public-interface tests). Four existing opt-in Docker/provider tests remain ignored by normal checks.
- Native product integration tests exercise executable invocations, authenticated HTTP, ACP/MCP JSON-RPC stdio, durable chat reload, ownership rejection, immutable active settings, shared panel selections, exact plan approval, completed implementation/report, reconnect, and terminal attachment. Clients use `/nonexistent` as PATH.
- `docker build -f containers/rust-runtime.Dockerfile -t software-factory:issue-25 .` built both executables with embedded assets without copying Python source into the build.
- `bash scripts/accept-native-entrypoints.sh software-factory:issue-25` passed in the resulting image: Python/Python3/uv are absent, and CLI, authenticated browser assets, ACP and MCP use the same Rust service and planning identity with the service URL unset. The harness removes its temporary containers and volumes. CI runs this harness before the existing deployment checks.
- Python recovery lint, formatting and strict typechecking pass independently. The isolated legacy suite finished with 377 passed, 22 skipped and one failure: `test_cancellation_racing_spawn_stops_the_new_child`. The same test fails identically on untouched baseline `9055dd6`, with `bb.stopped == []` rather than the expected child ID. Python runtime/test source is unchanged by this branch; this is a pre-existing legacy recovery issue, not a native entry-point regression.

The image smoke test explicitly opts into the fake backend to exercise protocols.
The completed cross-client implementation journey uses a scripted worker at the
existing service boundary. These are entry-point/behavior checks, not new live
provider or production-cutover acceptance claims. Existing protected Docker,
worker cleanup, authorization, durable recovery and delivery regression tests
remain in the Rust suite. #16–#19 gates are not reopened; #26/#27 retain deployment
UX and actual production cutover.

See [native product documentation](native-product.md) for the exhaustive command
inventory, deliberate compatibility changes, installation/upgrade and isolated
LangGraph recovery instructions.

## Two-axis code review

Standards review found no documented-standard breaches or baseline smells. It
identified correctness issues in macOS client/workspace-root aliases and panel selections
without a thread reference. These were fixed and covered through ACP/panel tests.

Spec review identified four compatibility issues: BB transport wrappers, ordinary
plan-prefixed requests, valid settings-session names mistaken for legacy state,
and nullable MCP arguments. All four were fixed with public-interface regressions.
