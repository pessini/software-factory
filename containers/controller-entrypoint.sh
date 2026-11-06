#!/bin/sh
set -eu
umask 077
data=${FACTORY_DATA_DIR:-/data}
workspace=${FACTORY_WORKSPACE:-/workspace}
if [ -n "${FACTORY_HTTP_TOKEN_FILE:-}" ]; then
    FACTORY_HTTP_TOKEN=$(cat "$FACTORY_HTTP_TOKEN_FILE")
    export FACTORY_HTTP_TOKEN
fi
if [ "${FACTORY_REQUIRE_DOCKER:-0}" = 1 ] && { [ "${1:-serve}" = serve ] || [ "${1:-serve}" = web ]; } && [ ! -f "$data/docker-backend.json" ]; then
    echo 'Docker backend is not configured; run configure-docker before starting production.' >&2
    exit 1
fi
exec factory-runtime "$data" "$workspace" "$@"
