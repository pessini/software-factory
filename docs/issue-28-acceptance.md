# Issue 28 native execution acceptance

Validated on 2026-11-07 on Linux x86-64 / WSL2, kernel
`6.18.33.2-microsoft-standard-WSL2`, as a non-root operator with user-systemd
cgroup v2 delegation, Bubblewrap 0.9.0 and Rust/Cargo 1.99.1.

## Automated checks

- `bash scripts/check-rust.sh`: formatting, all-target typechecking, Clippy with
  warnings denied and **80 passed** Rust tests. Eleven host/provider opt-ins are
  excluded from that normal run.
- All **seven native opt-ins passed** in disposable delegated systemd scopes,
  using `DOCKER_HOST=unix:///tmp/issue28-docker-unavailable.sock`. The configured
  Rust engine and native verification path completed without Docker access.
- Real pinned Codex tests cover a read-only planner session; builder application
  writes while Git, instructions, protected files, ancestor rename/delete,
  symlink targets, credentials and tool networking remain denied; and a complete
  planning → stale-approval rejection → exact approval → reopened engine →
  implementation → verification/review → final report journey.
- Real OS verification probes exercise write, rename, delete, symlink and network
  denial. Timeout/cancellation probes include descendants that call `setsid()`;
  results are accepted only after cgroup population is confirmed zero.
- Two **simulated App Server protocol** fixtures use actual native processes and
  cgroups to test a user question arriving before the turn-start response, saved
  answers after reconnect, and killing the controller during an active turn.
  Restart preserves the exact turn identity and dispatch count of one. These
  fixtures make no real-model or provider-enforcement claim.
- The existing actual Docker read-only/receipt/cleanup test and Docker
  timeout/large-output/cleanup test both passed after the transport extraction.
  Existing ordinary workflow, ownership, recovery and cross-interface tests also
  pass. The two separate real-provider Docker opt-ins were not rerun.

The live native model was explicitly `gpt-5.6-luna` with `low` effort and the
account's reviewed `on-request` approval policy. The broker denied escalation
requests. Target Python commands verify the disposable application; Factory's
product runtime remains Rust/ADK without Python/LangGraph.

The provider was staged from the already installed worker image before testing;
Docker was deliberately unreachable during the native acceptance run. Both
standalone provider files are mounted individually and verified by SHA-256:

| File | SHA-256 |
| --- | --- |
| Codex 0.154.0 | `3188814c35471432d4123203e0eb38e5bddc60226e3d7ddf0e59e649ea140022` |
| Matching code-mode host | `0c57be435e73b70d9106c850d751cd259a7f04da958a453d7ef59090d82b70f1` |
| Bubblewrap 0.9.0 | `e318903862396f96de3df57264e0158682b952fd3fb53ac23d876413e7b30f71` |

## Standards

The review identified executing a changed provider before verifying saved hashes,
including reconfiguration from the original hashless JSON, plus duplicated receipt
initialization. Both were fixed. Public configuration regressions prove changed
bytes are rejected before execution; receipt initialization is shared. No
outstanding findings remained in the follow-up review.

## Spec

The review identified unsafe runtime-root exposure, rejection of early provider
questions, and missing native restart/question acceptance. Runtime roots now
exclude host home/socket/special-filesystem trees; early questions are queued
until the turn identity is recorded; native supervision fixtures cover both
recovery cases. No outstanding findings remained in the follow-up review.

Standards: 0 outstanding findings. Spec: 0 outstanding findings.

See [native execution](native-execution.md) for setup inputs, supported policies,
private metadata mount targets, explicit write allowances, operator-visible
identities and permission records, recovery behavior, and protection differences
from Docker. macOS, Windows processes, other architectures, Claude and hosts
without the required Linux enforcement capabilities are not supported.
