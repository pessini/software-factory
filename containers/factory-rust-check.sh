#!/usr/bin/env bash
# Reviewed quality argv for this Rust repository. All mutable state stays in the
# per-run quality home; the target checkout and image are read-only during checks.
set -euo pipefail

if [[ "${HOME:-}" != /factory/provider ]]; then
  echo 'factory-rust-check requires the isolated worker home /factory/provider' >&2
  exit 2
fi
if [[ "${1:-all}" != all ]]; then
  echo 'usage: factory-rust-check [all]' >&2
  exit 2
fi

quality_home="$HOME/rust-quality"
mkdir -p "$quality_home"
if [[ ! -f "$quality_home/ready" ]]; then
  # Interrupted preparation is retried from immutable image bytes. No target
  # files, controller receipts or evidence directories are involved.
  rm -rf "$quality_home/cargo" "$quality_home/target"
  cp -R /opt/factory-rust-cache/cargo "$quality_home/cargo"
  cp -R /opt/factory-rust-cache/target "$quality_home/target"
  chmod -R u+rwX "$quality_home/cargo" "$quality_home/target"
  touch "$quality_home/ready"
fi
export CARGO_HOME="$quality_home/cargo"
export CARGO_TARGET_DIR="$quality_home/target"
export TMPDIR="$quality_home/tmp"
mkdir -p "$TMPDIR"
export CARGO_BUILD_JOBS=1 CARGO_INCREMENTAL=0
export CARGO_PROFILE_DEV_DEBUG=0 CARGO_PROFILE_TEST_DEBUG=0
export RUSTUP_HOME=/usr/local/rustup
export RUSTUP_TOOLCHAIN=1.99.1

cargo --version
rustc --version
cargo fmt --all -- --check
cargo check --workspace --all-targets --locked --offline
cargo clippy --workspace --all-targets --locked --offline -- -D warnings
cargo test --workspace --locked --offline
