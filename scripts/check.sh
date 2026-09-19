#!/usr/bin/env bash
# The same credential-free checks run locally and on both CI operating systems.
set -uo pipefail
cd "$(dirname "$0")/.."
mkdir -p .checks
export FACTORY_LIVE_CONTAINER=0 FACTORY_LIVE_PROVIDER=0 FACTORY_LIVE_NATIVE=0 FACTORY_LIVE_SANDBOX=0
export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1

if ! uv sync --frozen 2>&1 | tee .checks/install.log; then
  exit 1
fi

failed=0
check() {
  local name="$1"
  shift
  if ! "$@" 2>&1 | tee ".checks/$name.log"; then
    failed=1
  fi
}

check versions uv run --frozen --no-sync python --version
check lint uv run --frozen --no-sync ruff check .
check format uv run --frozen --no-sync ruff format --check .
check types uv run --frozen --no-sync mypy src
check tests uv run --frozen --no-sync pytest -p pytest_asyncio.plugin --junitxml=.checks/tests.xml
exit "$failed"
