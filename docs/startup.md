# Start Software Factory

The supported product is the Rust/ADK `software-factory` service and its native
CLI, browser, BB ACP and Codex MCP clients. Controller packaging and specialist
execution are separate choices: a native controller can use Docker workers or
Linux native subprocesses; Compose uses protected Docker workers and daemon
brokers. Installing Rust alone does not install provider CLIs or target tools.

## Dependencies

| Component | Required on host | Supplied elsewhere / optional |
| --- | --- | --- |
| Native controller from source | Git, rustup with pinned Rust/Cargo 1.99.1, writable private storage; `just`, Bash, jq and OpenSSL for the quickstart | `FACTORY_BIN=/absolute/bin/software-factory` uses an installed compatible binary without Cargo; curl for health checks |
| Compose controller from retained images | Linux, local Docker Engine, Compose v2, Git, `just`, Bash, jq, OpenSSL; non-root operator with socket access | Controller image contains Rust executables, Git, curl, gh and Docker CLI; host Cargo/Python unnecessary |
| Protected Docker execution with either controller | Docker daemon, retained worker image, reviewed seccomp profile, non-root target ownership, provider login on daemon host | Worker contains pinned Codex 0.154.0 and matching code-mode host; its Python/uv are worker tools |
| Native subprocess execution | Linux x86-64 (including capable WSL2), Bubblewrap, delegated cgroup v2 with cgroup.kill, user/PID/mount/network namespaces, standalone Codex 0.154.0 plus adjacent matching codex-code-mode-host, provider login, target tools | No Docker; unsupported providers/platforms/policies fail closed; see [capability matrix](native-execution.md) |
| Development / release acceptance | Rust toolchain, Git; Docker for Docker opt-ins | Python 3 for existing acceptance/consistent-snapshot helpers; jq for JSON preparation |
| Historical recovery | Isolated checkout of a pre-retirement release and its original dependencies/storage | Not shipped or checked by current CI; see [recovery](interface-migration.md) |

Docker workers need Linux-compatible target tools inside their image. Native
workers need reviewed host toolchain directories and explicit verification argv.
A target using Python does not turn Python into a Factory runtime dependency.
Use the pinned provider login policy (`never`, or explicitly reviewed
`on-request`); all escalation requests remain denied. Authenticate Codex separately
before configuration. Never copy credentials into the target checkout.

## Native controller with Docker execution

Use a dedicated target Git checkout owned by your non-root operator. From the
Factory source checkout, install rustup, the pinned toolchain, Git, Bash, `just`,
Docker, jq and OpenSSL using your supported host's package manager. `just --list` only
lists recipes. Set these variables in **every** terminal used below:

```bash
export FACTORY_WORKSPACE=/absolute/target
export FACTORY_DATA_DIR=/absolute/private/factory-data
# Use a durable location outside both source and target checkouts.
install -d -m 700 "$FACTORY_DATA_DIR"
export FACTORY_HTTP_TOKEN=$(openssl rand -hex 32)
# Retain the token privately and reuse it in client terminals/restarts.
just build
just init
# Edit $FACTORY_WORKSPACE/.factory/factory.yaml: models, budgets and quality argv.
docker build -f containers/worker.Dockerfile -t software-factory-worker:0.154.0 .
umask 077
jq -n --arg workspace "$FACTORY_WORKSPACE" --arg data "$FACTORY_DATA_DIR" \
  --arg auth "$HOME/.codex/auth.json" \
  --arg profile "$PWD/containers/worker-seccomp.json" \
  --argjson uid "$(id -u)" --argjson gid "$(id -g)" \
  '{executable:"docker",image:"software-factory-worker:0.154.0",
    daemon_workspace:$workspace,daemon_data_dir:$data,
    workspace_volume:null,data_volume:null,daemon_auth_file:$auth,
    uid:$uid,gid:$gid,seccomp_profile:$profile,approval_policy:"never"}' \
  > "$FACTORY_DATA_DIR/docker-input.json"
just configure-docker "$FACTORY_DATA_DIR/docker-input.json"
just start
```

`just start` runs the native controller in the foreground. It needs a real saved
backend; missing/ambiguous backend configuration fails before HTTP readiness.
The native Docker configuration above uses host brokers: stopping the controller
leaves them available for reconciliation. Use the daemon-broker configuration in
[production deployment](production-deployment.md) when brokers must survive
controller-container replacement. Docker Desktop hosts must share the exact
absolute workspace/data/login paths with their daemon. The accepted Compose
production topology uses Linux and a local socket.

To use an installed executable instead of Cargo in all native recipes:

```bash
cargo install --path rust/factory-runtime --locked --bin software-factory
export FACTORY_BIN=$(command -v software-factory)
software-factory --version  # must include Rust/ADK
```

Keep the chosen release binary and source SHA. See [installation and executable
collisions](native-product.md) before replacing an old Python launcher. No host
Python is needed for either native-controller path.

## Docker-free controller and native execution

Use the same target policy, private storage and client steps, but a **separate**
`FACTORY_DATA_DIR` and the reviewed JSON from [native execution](native-execution.md).
Install standalone pinned Codex and its companion, Bubblewrap and target tools
first. Provision persistent user-systemd cgroup delegation with `cgroup.kill`;
the delegated root must remain present after controller/client disconnects.
Availability depends on host policy; merely creating a directory under /sys is
insufficient. Configuration actively probes namespaces, read-only/builder provider
permissions and cleanup and refuses unsupported hosts.

```bash
export FACTORY_DATA_DIR=/absolute/private/factory-native
install -d -m 700 "$FACTORY_DATA_DIR"
# Write /absolute/private/native.json with actual provider/cgroup/runtime paths.
just doctor-native /absolute/private/native.json
just configure-native /absolute/private/native.json
just start
```

The JSON selects native execution explicitly; `configure-docker` selects Docker.
Both backend files in one storage directory are rejected. Do not switch existing
runs by deleting a backend file. `worker_connection.model_connection: native`
selects the Codex protocol, not an execution backend. Native Bubblewrap/cgroup
protection differs from Docker images/seccomp/credential isolation; review the
[protection differences and supported combinations](native-execution.md).

## Compose controller

Follow [production deployment](production-deployment.md) for complete paths,
UID/GID/socket GID, token, image retention and backend JSON commands. The sequence
is: copy `containers/production.env.example` to `.env.production`, edit and export
its values, create private directories/token and a reviewed target policy, retain
or build worker/controller images, then:

```bash
# Only when building locally; requires Docker, not host Cargo:
just container-build
# docker-input.json must be inside the mounted data directory and use
# /opt/factory/worker-seccomp.json plus the required daemon broker fields.
just container-configure "$FACTORY_DATA_DIR/docker-input.json"
just container-start
just container-status
just container-logs
# Ctrl+C stops following logs. To stop the controller:
just container-stop
```

Set `FACTORY_COMPOSE_ENV=/absolute/private/production.env` to use another env file.
The recipes and direct `docker compose --env-file ...` commands are equivalent.
For retained prebuilt images, verify trusted archive checksums, `docker load` the
controller and worker archives, and set `FACTORY_CONTROLLER_IMAGE` to the retained
tag/digest; skip `container-build`. Startup uses `--no-build --pull never`: no
host Rust/Cargo, source compilation or implicit image replacement. Build-time
UID/GID must match the deployment operator. Runtime commands need this repository's
Compose/recipe files, even when their images came from an artifact archive.

## Connect and submit work

For a native controller, in a second terminal export the **same**
`FACTORY_WORKSPACE`, token and optional installed `FACTORY_BIN`. For Compose,
read the token privately with
`export FACTORY_HTTP_TOKEN=$(cat "$FACTORY_TOKEN_FILE")` after exporting its env
file. Set `FACTORY_CONTROL_URL=http://127.0.0.1:4317` (use the selected host port).

```bash
curl --fail http://127.0.0.1:4317/health/ready
just doctor
just plan plan-001 'Describe the change'
# Open http://127.0.0.1:4317/?token=YOUR_TOKEN locally.
# Answer questions and approve the exact current plan hash in the browser.
just runs
just status RUN_ID
# Direct implementation, when a separate planning session is unnecessary:
just run work-001 'Implement the reviewed request'
```

The `just doctor/plan/run/runs/status` examples above use a host Rust executable
(or Cargo by default). On a retained-image Linux Compose host **without Cargo**,
run the image's client with host networking so the selected published port and
Host header match. No Docker socket or data directory is mounted into this client:

```bash
# Export .env.production values first, then the token and selected service URL.
export FACTORY_HTTP_TOKEN=$(cat "$FACTORY_TOKEN_FILE")
export FACTORY_CONTROL_URL="http://127.0.0.1:${FACTORY_PORT:-4317}"
factory_client() {
  docker run --rm --pull never --network host --read-only \
    --mount "type=bind,src=$FACTORY_WORKSPACE,dst=$FACTORY_WORKSPACE,readonly" \
    -e FACTORY_HTTP_TOKEN -e FACTORY_CONTROL_URL \
    --entrypoint software-factory "$FACTORY_CONTROLLER_IMAGE" \
    "$@" -w "$FACTORY_WORKSPACE"
}
factory_client doctor
factory_client plan 'Describe the change' --request-id plan-001
factory_client list
factory_client status RUN_ID
```

Browser access uses the published host port. BB/Codex stdio clients need a
compatible native executable on their own host, or an operator-reviewed launcher
using the image's `acp`/`mcp` adapters without host Cargo/Python. MCP can use the
image-client pattern above with `docker run -i`. ACP also needs persistent writable
client storage for its presentation database; use this pattern in the BB launcher:

```bash
# Create once on the client host, outside the target and controller data directories.
export FACTORY_CLIENT_HOME="${XDG_STATE_HOME:-$HOME/.local/state}/software-factory/clients"
install -d -m 700 "$FACTORY_CLIENT_HOME"
factory_acp() {
  docker run --rm -i --pull never --network host --read-only \
    --mount "type=bind,src=$FACTORY_WORKSPACE,dst=$FACTORY_WORKSPACE,readonly" \
    --mount "type=bind,src=$FACTORY_CLIENT_HOME,dst=/factory/client-state" \
    -e FACTORY_CLIENT_HOME=/factory/client-state \
    -e FACTORY_HTTP_TOKEN -e FACTORY_CONTROL_URL \
    --entrypoint software-factory "$FACTORY_CONTROLLER_IMAGE" \
    acp -w "$FACTORY_WORKSPACE"
}
```

The client-state directory must be owned by the image's configured UID/GID. Reuse
it across launcher invocations so saved BB chats survive container removal. Keep
the workspace, service URL and token available to the launcher. These Linux
host-network clients are separate from the persistent Compose controller.

The configured token is never printed. Browser token exchange redirects to a
clean URL. Keep bearer links private. Health certifies storage/startup recovery,
not provider availability or workflow completion; `doctor` exercises the catalog.
Inspect the saved outcome through `ready_for_review` and verify the report.
Reuse request IDs only with identical inputs. `answer` takes the exact interaction
ID and answers JSON; `resume` explicitly requests reconciliation; `cancel` saves
cancellation and waits for service-owned cleanup. None starts another controller.

Install the native CLI on client hosts, then register BB ACP and Codex MCP using
the absolute executable, fixed workspace, origin and token as documented in
[native clients](native-product.md#bb-acp-settings-and-codex-mcp) and
[Factory Controls](../bb-plugin-factory-controls/README.md). Clients do not launch
a daemon. Provider credentials are for workers; the HTTP bearer is for clients.

## Lifecycle and release milestones

Keep canonical workspace, complete private data directory, backend identities,
provider homes and token location stable across restarts. The old per-user default
`~/.local/share/swe-factory/rust-slice` remains for compatibility; explicit per-target
paths are recommended. Native Ctrl+C and Compose stop drain transitions and retain
unfinished work. Restart the same command/env file; inspect saved interactions and
`recovery_needed` receipts before explicit resume. Never generate a new request
ID to retry uncertain work. Do not delete locks/claims/receipts to force progress.

A lifetime workspace service lock fences concurrent native/Compose controllers,
even with different data directories/ports. The data service lock and per-run
execution locks/claims remain separate. Stop one packaging mode before starting
the other; uncertain surviving worker operations still need reconciliation.
Use the [offline whole-workspace/data backup and rollback procedure](production-deployment.md)
before upgrades. The existing snapshot helper requires Python and Docker and is
for Docker deployments; native-only operators must quiesce controller/brokers and
confirm empty cgroups before taking a consistent offline filesystem snapshot.
Do not point the Docker snapshot helper at active native execution.

Implementation/release acceptance (#16–#19, #25, #28) is recorded historical
evidence. Readiness for cutover requires current source/image/provider checks.
Actual production cutover (#27) requires separate operational evidence. This guide does not declare cutover.
The Python runtime has been removed; recover old records only through an isolated
[historical release](interface-migration.md). [Issue 26 validation](issue-26-acceptance.md)
records checks and any uncompleted clean-host/provider gates.

`just demo-start` explicitly enables fake workers with separate `-demo` storage;
it is only a UI/protocol demonstration. Acceptance helpers are named `check` or
`container-accept` and have separate tool dependencies. Normal product recipes
never choose a fake backend automatically.
