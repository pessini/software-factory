# Issue 18 acceptance

The release remains blocked on browser verification in the execution environment. Automated credential-free coverage is separate from
live provider evidence. Production packaging/cutover remains #19.

| Gate | Evidence |
| --- | --- |
| Saved planning / submitted answers / history after restart | `tests/planning.rs`: reopen the runtime, submit duplicate/conflicting answers, finish the exact revision, preserve history on revise |
| Exact approval / concurrent and lost-response retries | Concurrent approvals return the same implementation ID; process death after saving handoff intent is recovered without dispatch on status reads; immutable approved snapshot and glossary/ADR criteria; stale approval fails |
| Existing Python saved state | Real Python-generated archive fixture retains its original spaced JSON snapshot hash; repeat import preserves linked implementation; tampering fails |
| HTTP ownership / auth / polling | Existing session list contract, missing token, wrong Host, cross-origin mutation; another workspace cannot read saved sessions |
| SSE replay / live transition / disconnect | Saved ID 1, live ID 2, reconnect after 1 replays 2; future cursor conflicts; disconnect does not cancel saved wait |
| Full planning-to-implementation | Existing simulated Worker passes Rust quality checks/review, exact greeting output and validated final report (`approved_planning_journey...`) |
| CLI / shared remote adapter | HTTP contract tests cover saved IDs, exact answer payload, CLI status, stale error propagation, workspace mismatch before mutation, HTTPS off loopback |
| Browser visual journey | Pending: browser inventory returned no enabled browser in this session |
| Protected Docker workflow integration | Passed: `scripts/accept-rust-interfaces.py`, real Docker + HTTP/CLI/ACP/MCP, two service restarts, concurrent approval, checks/review/report; [saved record](acceptance/issue-18.json). Provider is simulated |

Commands: `bash scripts/check-rust.sh` and `bash scripts/check.sh`.
The new contracts and intentional version changes are documented in
[rust-interfaces.md](rust-interfaces.md).

Review against `dac67a77815629f4e4cf2064274d4fa898133e3d` completed on both axes.
Standards: no documented-standard violations; authentication, resume CAS, and
repeated identity derivation findings resolved. Spec: prior-decision import,
ACP retry identity, saved settings, and bootstrap-failure export findings resolved.
Both reviewers verified the corrections with no remaining actionable findings.

Final Rust checks: formatting, all-target typechecking, Clippy with warnings denied,
and the full credential-free suite. Public proxy HTTPS behavior has explicit
contract coverage. The protected cross-client acceptance record includes source
and binary hashes and distinguishes its simulated provider from live evidence.

Recorded validation results:

- `bash scripts/check-rust.sh`: **52 passed, 4 opt-in tests ignored**; formatting,
  typechecking and Clippy passed.
- `bash scripts/check.sh`: **370 passed, 22 skipped**; lint, formatting and mypy
  passed. The final historical-report adapter additionally passed all **3 focused
  export regressions** (including the follow-up historical implementation case).
- `scripts/accept-rust-interfaces.py`: **passed**, real Docker with a simulated
  provider; final source/binary hashes match the saved record.
- Browser verification: **open**. No browser surface was available, including after
  requesting a Codex browser panel; the panel request was queued without an
  inspectable browser session.
