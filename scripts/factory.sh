#!/usr/bin/env bash
# Source-checkout launcher; FACTORY_BIN may point just recipes at an installed binary.
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
exec cargo run --manifest-path "$root/Cargo.toml" --locked --quiet \
  -p factory-runtime --bin software-factory -- "$@"
