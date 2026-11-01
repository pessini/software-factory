# Development commands. Install tools with Homebrew: brew install just rust jq
# Run `just` to list commands. Long-running commands stay in the foreground;
# stop them with Ctrl+C. Saved runs survive restarting the process.
#
# The native service serves the existing planning UI and owns background runs.
# Open the token link it prints. Configure the protected Docker backend before
# requesting provider work; see docs/rust-interfaces.md.
#
# Choose a target checkout and persistent storage outside that checkout:
#   FACTORY_WORKSPACE=/absolute/checkout just start
#   FACTORY_DATA_DIR=/absolute/data FACTORY_WORKSPACE=/absolute/checkout just start
# Defaults below use this repository and persistent per-user Rust slice storage.
# Keep these paths unchanged when reconnecting to saved runs.

set shell := ["bash", "-euo", "pipefail", "-c"]

export FACTORY_WORKSPACE := env("FACTORY_WORKSPACE", justfile_directory())
export FACTORY_DATA_DIR := env("FACTORY_DATA_DIR", env("XDG_DATA_HOME", home_directory() / ".local/share") / "swe-factory/rust-slice")

# List available commands without starting a service.
default:
    @just --list

# Compile the standalone native binary: target/release/factory-runtime.
build:
    cargo build --locked --release -p factory-runtime

# Start the native Rust service and planning UI; builds automatically.
start:
    @printf '%s\n' 'Starting the Rust service and planning UI; Ctrl+C to stop.'
    cargo run --locked --quiet -p factory-runtime -- "$FACTORY_DATA_DIR" "$FACTORY_WORKSPACE" serve

# Example: just run greeting-1 'Build a greeting' demo-session
# Reusing the same request ID/input returns saved state; new work needs a new ID.
# jq encodes the request safely; no Python process is used.
# Create a fake Rust run; use the supervisor or explicitly resume it.
run request_id request session_id="default":
    cargo run --locked --quiet -p factory-runtime -- "$FACTORY_DATA_DIR" "$FACTORY_WORKSPACE" start \
      "$(jq -cn --arg request_id {{ quote(request_id) }} --arg session_id {{ quote(session_id) }} --arg request {{ quote(request) }} '{request_id: $request_id, session_id: $session_id, request: $request, timeout_seconds: 3600, max_dispatches: 3}')"

# List saved Rust runs, including their status and pending interactions.
runs:
    cargo run --locked --quiet -p factory-runtime -- "$FACTORY_DATA_DIR" "$FACTORY_WORKSPACE" list

# Inspect one run; copy its interaction_id to answer the saved approval.
status run_id:
    cargo run --locked --quiet -p factory-runtime -- "$FACTORY_DATA_DIR" "$FACTORY_WORKSPACE" status {{ quote(run_id) }}

# Approve or decline the fake worker: just answer RUN_ID INTERACTION_ID true/false.
answer run_id interaction_id approved="true":
    cargo run --locked --quiet -p factory-runtime -- "$FACTORY_DATA_DIR" "$FACTORY_WORKSPACE" answer {{ quote(run_id) }} {{ quote(interaction_id) }} {{ quote(approved) }}

# Advance a run without the supervisor, or explicitly retry recovery_needed work.
resume run_id:
    cargo run --locked --quiet -p factory-runtime -- "$FACTORY_DATA_DIR" "$FACTORY_WORKSPACE" resume {{ quote(run_id) }}

# Save cancellation and stop the selected run. Stopping the service is Ctrl+C.
cancel run_id:
    cargo run --locked --quiet -p factory-runtime -- "$FACTORY_DATA_DIR" "$FACTORY_WORKSPACE" cancel {{ quote(run_id) }}

# Run Rust formatting, typechecking, Clippy and tests.
check:
    bash scripts/check-rust.sh

# Optional Docker path for the same Rust slice; native `start` needs no Docker.
container-build:
    docker build -f containers/rust-runtime.Dockerfile -t factory-rust-slice:dev .
