# Production Rust work and remaining clean-host gate

Recorded 2026-11-12. **The named WSL workspace now has a Rust-capable worker and
has completed a real Rust implementation task.** The scope-control regression
is fixed, and the earlier unrequested glossary output has been archived outside
the active checkout. The remaining acceptance gate is clean-host provider
execution under #26. The user explicitly chose to leave that gate blocked rather
than change AppArmor policy.

The [machine record](acceptance/issue-27-rust-production.json) contains exact
artifacts, approval, checks, receipts and backup hashes. The
[initial adoption record](issue-27-acceptance.md) remains historical evidence for
the earlier documentation workflow and clients.

## Current deployment

- Workspace: `/home/lpessini/dev/software-factory-production`.
- Active private storage:
  `/home/lpessini/.local/share/software-factory-production/data-rust`.
- Controller: `sha256:ed6c33083ec47b2fa16f6e1970060a9188b7df0dd49e71391b28ef61926b683e`,
  built from `fcc1b556446bcb7f67cf6a7e4df219cb47d833cd`.
- Rust worker: `sha256:cf9c10df8901a0f14fe1a23b4cc0396c2c9867d36be36f7efbe74e8fec12fd69`.
- Provider: pinned Codex 0.154.0, gpt-5.6-sol / low effort, explicit `on-request`
  account policy; broker escalation requests remain denied.

The [Rust project worker](rust-project-worker.md) runs formatting checks,
all-target typechecking, Clippy with warnings denied and the full ordinary test
suite. Its real image acceptance passed with **84 tests and 11 ignored host/provider
opt-ins**, under the existing 2 GiB/2 CPU/256 PID limits, no network and a read-only
checkout/image. Mutable Cargo/build/test state stays in the separate per-run
quality home. The deployed controller image has no Python or uv; test-fixture
Python belongs to the optional worker toolchain.

The previous documentation-only deployment was stopped and completely
snapshotted before its backend changed. Its original `data-on-request` storage,
images and checkout snapshot remain available. A fresh `data-rust` backend was
configured, then the same CLI/MCP/ACP service origin and token were retained.
No existing run's saved backend or receipts were rewritten.

## Actual Rust workflow

The task corrected the misleading `Durable fake run saved` event message to
`Durable run saved` and added an assertion to the existing public planning API
integration test. The approved files were exactly:

```text
rust/factory-runtime/src/runtime.rs
rust/factory-runtime/tests/planning.rs
```

Session `pln_e35d44bad148`, revision 1, had exact approved hash
`ee9c8f3054e13dae93826f2cd7df79f1423df13b0ad14077ca52205a85d900a8`.
The user explicitly approved it. Implementation
`run-7de9def3590549137dae0e5757c3288fdaaa3a049f40e757e4271897a39d13c5`
completed baseline checks, planning, Builder, full offline Rust verification,
focused request/code/security reviews and Judge. Its result is `ready_for_review`,
with **84 ordinary tests passed**, 11 opt-ins ignored and zero final findings.
The actual diff contains only the two approved files. No `CONTEXT.md` was created.

The new public-response assertion was also checked against the old wording and
failed with `Durable fake run saved`; it passed after adopting the verified fix.
The source output is committed in the issue branch. The production checkout keeps
its output uncommitted for normal human review. The worker did not commit, push,
publish or deploy its change. The live controller remains the recorded artifact;
editing this repository's Rust source does not hot-reload the service binary.

New plans require an exact `write_scope` file list. Reference glossary/ADR content
cannot expand it. The engine checks the complete actual diff after every Builder
turn and before handoff; an extra file blocks even if checks and a model Reviewer
would approve it. Public planning/approval and workflow integration regressions
cover that failure. Historical approvals preserve their original hashes and
behavior. The initial glossary output is archived privately under
`archived-glossary-output`, with its original report and whole snapshot retained.

After completion, the controller was stopped, a complete offline snapshot saved
as `backups/completed-rust-workflow-2026-11-12`, and the controller recreated.
The same run ID, terminal status, report and event cursor survived. Controller and
worker image archives/checksums and source history are retained under `release/`.
A fresh Codex CLI process also read this same `ready_for_review` run through the
actual MCP adapter after recreation. BB's previously verified registration remains
installed; its desktop server was closed during the final reconnect attempt.

```bash
software-factory doctor
software-factory status run-7de9def3590549137dae0e5757c3288fdaaa3a049f40e757e4271897a39d13c5
factory-codex
```

The saved browser session is
`http://127.0.0.1:4317/planning/pln_e35d44bad148`.
The previous deployment's run IDs belong to retained old storage; they are not
automatically imported into this fresh backend.

## Clean-host result: still blocked

A fresh checksum-verified Ubuntu Noble VM was provisioned with an initially
empty Docker image cache, Docker 29.1.3, Compose 2.40.3 and packaged just 1.21.0.
No host Cargo/rustc was installed. Retained controller/worker artifacts and Git
source were transferred and verified. The native installed binary and Compose
controller both reached health readiness, and CLI provider-catalog checks passed.
Authenticated browser UIs connected to the correct separate target workspaces.

The first native quickstart attempt exposed a real recipe compatibility bug:
just 1.21 does not have `home_directory`. The recipes now use compatible environment
functions, verified with just 1.21 and 1.59. Native `just configure-docker`/
`just start` and retained-image Compose startup then passed. The quickstart source
was `09f0e78`; its Rust runtime build inputs are unchanged from the recorded
controller source.

Both real-provider planning attempts stopped for recovery before implementation:
the nested Codex filesystem sandbox failed with
`bwrap: Failed to make / slave: Permission denied` on the AppArmor-enabled host.
Health/catalog availability therefore did not establish a usable provider
workflow. A named AppArmor profile was proposed, but the user selected
**“Leave the clean-host gate blocked; keep production work going.”** No profile
was loaded, no security setting was relaxed, and no confinement bypass was added
to the application. The VM is no longer running; its private disk/diagnostics are
retained. These failed attempts are not counted as clean-host workflow passes.

#26's current-candidate clean-host acceptance and therefore full #27 closure
remain open. Revisit the host confinement prerequisite and repeat the affected
fresh-host real-provider/client checks only when that work is authorized.
