# Rust repository worker

`containers/rust-project-worker.Dockerfile` supplies this repository's Rust 1.99.1
toolchain, rustfmt, Clippy, Git, Python test-fixture tools and pinned Codex 0.154.0.
It is an optional target toolchain, not a controller/client runtime dependency.
The ordinary provider worker remains available for targets with different tools.

Build from the reviewed release source and retain the resulting immutable ID:

```bash
docker build -f containers/rust-project-worker.Dockerfile \
  -t software-factory-rust-worker:dev .
bash scripts/accept-rust-project-worker.sh software-factory-rust-worker:dev
```

Image construction downloads and compiles locked dependencies. Actual quality
execution uses `--offline`, a read-only image/checkout and a separate network
namespace with no network access. The checked-in acceptance command exercises
those constraints with no provider login, Docker socket or controller state
mounted into the container. It tests the committed source revision, not unstaged
edits, and retains no writable target cache.

Configure the real Docker backend with this image's immutable ID, preserving the
existing non-root IDs, seccomp profile, provider login and broker setup. Changing
an existing deployment's worker image uses a new private data directory after
draining, stopping and snapshotting the old deployment. Keep the original data,
images and checkout snapshot available for inspection/recovery. Do not rewrite
saved receipts or an existing run's backend identity.

Use this reviewed quality command in the target's `.factory/factory.yaml`:

```yaml
quality:
  commands:
    - name: rust-repository
      argv: [factory-rust-check, all]
      timeout_seconds: 2400
      repeat_on_recovery: true
```

It runs formatting checks, all-target typechecking, Clippy with warnings denied
and the full ordinary test suite. Host/provider opt-ins stay explicit and are
not silently counted as passes. Build output and mutable Cargo cache live in the
per-run quality home `/factory/provider/rust-quality`; they never enter the
target checkout. The immutable seed cache comes from the image. One build job,
disabled incremental compilation and stripped development/test debug information
bound compilation within the worker's existing 2 GiB memory limit. The quality
home persists through checks/recovery for that run and is included in complete
deployment backups. Plan/review/Builder homes remain separate.

The worker cannot fetch a newly added dependency during quality verification.
Review lockfile changes and rebuild/validate a new image before selecting it for
new work. No network or permission fallback is enabled for cache misses.
