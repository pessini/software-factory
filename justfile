# Rust product operations. See docs/startup.md for prerequisites and backend setup.
# Services stay in the foreground; Ctrl+C preserves saved work. Clients require
# the same FACTORY_HTTP_TOKEN and workspace as the independent service.
set shell := ["bash", "-euo", "pipefail", "-c"]
export FACTORY_WORKSPACE := env_var_or_default("FACTORY_WORKSPACE", justfile_directory())
# Preserve the existing default so upgrades reconnect to saved runs.
export FACTORY_DATA_DIR := env_var_or_default("FACTORY_DATA_DIR", env_var_or_default("XDG_DATA_HOME", env_var("HOME") / ".local/share") / "swe-factory/rust-slice")
factory := env_var_or_default("FACTORY_BIN", justfile_directory() / "scripts/factory.sh")
compose := 'docker compose --env-file ' + quote(env_var_or_default("FACTORY_COMPOSE_ENV", ".env.production"))

# List commands; does not start a service.
default:
    @just --list

# Build both Rust executables using the pinned toolchain and lockfile.
build:
    cargo build --locked --release -p factory-runtime

# Create target policy; review quality argv before any work (refuses overwrite).
init:
    {{quote(factory)}} init -w "$FACTORY_WORKSPACE"

# Pin protected Docker workers from reviewed operator JSON; requires Docker.
configure-docker backend:
    {{quote(factory)}} configure-docker {{quote(backend)}} --storage "$FACTORY_DATA_DIR" -w "$FACTORY_WORKSPACE"

# Probe Linux namespaces/cgroups and pinned native Codex prerequisites.
doctor-native backend:
    {{quote(factory)}} doctor-native {{quote(backend)}} --storage "$FACTORY_DATA_DIR" -w "$FACTORY_WORKSPACE"

# Select Docker-free Linux execution; use separate storage from Docker runs.
configure-native backend:
    {{quote(factory)}} configure-native {{quote(backend)}} --storage "$FACTORY_DATA_DIR" -w "$FACTORY_WORKSPACE"

# Start native controller/UI with its saved real backend; Ctrl+C to stop.
start:
    {{quote(factory)}} web --storage "$FACTORY_DATA_DIR" -w "$FACTORY_WORKSPACE"

# Check service/workspace, target policy and real provider model catalog.
doctor:
    {{quote(factory)}} doctor -w "$FACTORY_WORKSPACE"

# Submit implementation to the running service; request IDs are idempotent.
run request_id request session_id="default":
    {{quote(factory)}} run {{quote(request)}} --request-id {{quote(request_id)}} --session {{quote(session_id)}} -w "$FACTORY_WORKSPACE"

# Submit planning; approve the exact saved hash through UI/client when ready.
plan request_id request session_id="default":
    {{quote(factory)}} plan {{quote(request)}} --request-id {{quote(request_id)}} --session {{quote(session_id)}} -w "$FACTORY_WORKSPACE"

# List saved runs through the service; never dispatches work.
runs:
    {{quote(factory)}} list -w "$FACTORY_WORKSPACE"

# Inspect a run through the service; never dispatches work.
status run_id:
    {{quote(factory)}} status {{quote(run_id)}} -w "$FACTORY_WORKSPACE"

# Save answers JSON for an exact pending interaction (copy its question IDs).
answer run_id interaction_id answers:
    {{quote(factory)}} answer {{quote(run_id)}} {{quote(interaction_id)}} {{quote(answers)}} -w "$FACTORY_WORKSPACE"

# Explicitly request saved-run reconciliation through the service.
resume run_id:
    {{quote(factory)}} resume {{quote(run_id)}} -w "$FACTORY_WORKSPACE"

# Save cancellation; the service confirms worker cleanup.
cancel run_id:
    {{quote(factory)}} cancel {{quote(run_id)}} -w "$FACTORY_WORKSPACE"

# Start a FAKE service for UI/protocol demos only; use separate demo storage.
demo-start:
    FACTORY_ALLOW_FAKE_WORKER=1 {{quote(factory)}} web --storage "$FACTORY_DATA_DIR-demo" -w "$FACTORY_WORKSPACE"

# Format/typecheck/lint and run the ordinary Rust test suite.
check:
    bash scripts/check-rust.sh

# Build Compose controller locally (Docker builder contains Cargo).
container-build:
    {{compose}} build controller

# Pin worker/broker images using reviewed JSON inside the controller container.
container-configure backend:
    {{compose}} run --rm --no-deps controller configure-docker {{quote(backend)}}

# Start Compose from retained images; never builds or pulls implicitly.
container-start:
    {{compose}} up -d --no-build --pull never --wait

# Show Compose health and container identities.
container-status:
    {{compose}} ps

# Follow controller logs (private); Ctrl+C stops following only.
container-logs:
    {{compose}} logs --follow controller

# Stop Compose controller, retaining storage, checkout, brokers and workers.
container-stop:
    {{compose}} stop controller

# Optional acceptance helper: real Compose/workers with simulated provider.
container-accept:
    python3 scripts/accept-rust-production.py
