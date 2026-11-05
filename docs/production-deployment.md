# Single-host Rust deployment (#19)

This is the production packaging candidate for one Linux Docker host, one
controller and one dedicated target checkout. SQLite is local to that host.
The [release evidence](issue-19-acceptance.md) determines cutover readiness.
Keep the Python installation and old-run recovery environment until every release
gate passes. ADK never opens or interprets LangGraph checkpoint tables.

## Install and configure

Install Docker Engine with Compose v2, Git, Python 3, jq and OpenSSL. Use a local
daemon (`/var/run/docker.sock`), not a remote Docker context. Prepare a dedicated
checkout with its `.git` directory inside it; do not use external linked worktrees
for this initial topology. Give the checkout and private data directory the same
non-root UID/GID. The data directory must be outside the checkout and mode 0700.
Do not share either directory with another controller, including a native service.

From the Factory repository:

```bash
cp containers/production.env.example .env.production
# Edit the file: absolute, stable paths; actual UID/GID and Docker socket GID.
id -u
id -g
stat -c '%g' /var/run/docker.sock
set -a
. ./.env.production
set +a
install -d -m 700 "$FACTORY_DATA_DIR"
# Clone the target into $FACTORY_WORKSPACE and add its reviewed .factory/factory.yaml.
# Ensure every parent directory is traversable by the configured UID/GID.
umask 077
openssl rand -hex 32 > "$FACTORY_TOKEN_FILE"
docker build -f containers/worker.Dockerfile -t software-factory-worker:0.154.0 .
docker compose --env-file .env.production build
```

Write an initial backend configuration into the private data directory. The
provider login must exist on the **daemon host** and be readable by the worker
UID. It is mounted read-only only into the provider home, denied to model tools.
Do not store the HTTP token, backend configuration or provider login in the target.

```bash
jq -n --arg workspace "$FACTORY_WORKSPACE" --arg data "$FACTORY_DATA_DIR" \
  --arg auth /home/operator/.codex/auth.json \
  --argjson uid "$FACTORY_UID" --argjson gid "$FACTORY_GID" \
  '{executable:"docker",image:"software-factory-worker:0.154.0",
    daemon_workspace:$workspace,daemon_data_dir:$data,
    workspace_volume:null,data_volume:null,daemon_auth_file:$auth,
    uid:$uid,gid:$gid,seccomp_profile:"/opt/factory/worker-seccomp.json",
    approval_policy:"never"}' > "$FACTORY_DATA_DIR/docker-input.json"
docker compose --env-file .env.production run --rm --no-deps controller \
  configure-docker "$FACTORY_DATA_DIR/docker-input.json"
docker compose --env-file .env.production up -d --no-build --wait
docker compose --env-file .env.production ps
```

`configure-docker` saves the immutable worker image ID and seccomp hash. Service
startup fails if the saved Docker backend is absent; production never silently
falls back to the fake worker. Preserve the selected worker image on the daemon.
The build pins Rust 1.99.1, the ADK Git revision, Cargo.lock, Docker CLI 29.1.3,
Codex 0.154.0, uv 0.12.12 and worker Python 3.14.7. Base images are digest-pinned.
OS security packages come from Debian's repository; retain the built image by
digest for exact rollback rather than assuming a later rebuild has identical bytes.

## Clients and identity

Compose binds `0.0.0.0:4317` inside the container and publishes only
`127.0.0.1:4317` on the host (or the configured host port). The explicit
`FACTORY_HTTP_DOCKER_LOOPBACK=1` assertion permits this topology with a local HTTP
origin. Never set that assertion when exposing the port beyond loopback.

Use the existing browser UI at `http://127.0.0.1:4317/?token=YOUR_TOKEN`; exchange
redirects to a clean URL and sets the HttpOnly/SameSite cookie. Read the token
locally from its private file; a configured token is never printed at startup.
CLI/BB ACP/Codex MCP adapters use `FACTORY_CONTROL_URL=http://127.0.0.1:4317` and
`FACTORY_HTTP_TOKEN` from that file. See [shared interfaces](rust-interfaces.md).
Clients on this host must identify the workspace by its same absolute path.

The bearer grants controller authority, including commands that can mutate the
target. `FACTORY_CALLER_ID` is one operator's audit identity, not multi-user RBAC.
Wider deployment requires an authenticated TLS reverse proxy, access policy and
network controls, an explicit HTTPS `FACTORY_HTTP_ORIGIN`, exact
`FACTORY_HTTP_HOST`, and operator-managed token/identity. Remove the loopback
assertion and review the proxy forwarding/Host rules. Do not publish a plain HTTP
port publicly or treat this shared bearer as per-user authorization.

## Storage and worker authority

Compose mounts data and workspace at the **same absolute paths** as the daemon
host. Persist the entire data directory: `factory-rust.sqlite` and its WAL/SHM,
`factory-controls.sqlite` and its WAL/SHM, ADK checkpoints/sessions/queue tables,
artifacts, evidence/manifests, worker records, broker locks/logs, provider homes,
continuation IDs and Docker/workspace identity files. Persist the entire checkout,
including `.git`, ignored dependencies, ownership claims and protected metadata.
Do not discard or edit locks, claims or receipts to force progress.

Only the controller gets the Docker socket, via its supplemental socket GID.
This is host-level authority even with a non-root controller UID. Treat the
controller image, reviewed quality programs, configuration and operator as trusted;
do not use an untrusted shared Docker host. The controller runs with dropped
capabilities, no-new-privileges, a read-only root and bounded tmpfs.

Coding workers get no socket or controller storage root. Their evidence and
provider subdirectories are scoped; evidence and protected repository files are
read-only. The established [worker policy](rust-workflow.md) supplies separate PID
namespaces, non-root IDs, dropped capabilities, seccomp, read-only roots, 2 CPUs,
2 GiB memory and 256 PIDs per worker; quality commands have no network. The
controller defaults to one supervised run (maximum supported setting 16), 2 CPUs,
2 GiB and 256 PIDs. Catalog inspection has one additional cross-process admission
slot: concurrent requests return HTTP 409 Busy and may be retried after the active
inspection finishes. Interrupted catalog workers are confirmed stopped before a
replacement is created. Only one writer owns this workspace. Review per-run time/repair
budgets in `.factory/factory.yaml`; these remain durable run inputs.

PR publication needs GitHub CLI credentials supplied only to the controller.
The image includes `gh` and Git, but Compose does not grant publication credentials
by default. For authorized delivery, add an operator-owned read-only credential
mount via a reviewed override and configure `GH_CONFIG_DIR` or `GH_TOKEN`; never
add these mounts/environment variables to workers. Retain the saved delivery
policy and the [existing publication rules](pr-delivery.md).

## Health, shutdown and recovery

`GET /health/live` checks HTTP responsiveness. `GET /health/ready` checks committed
runtime storage access after startup intent recovery. Both require the configured
Host but no token and return only `{"status":"ok"}` or unavailable. Readiness does
not certify provider availability or clear `recovery_needed` work.
Use authenticated status/list operations for recovery diagnostics. Startup output
contains the origin/version; background failures log a run ID without raw provider
errors. Detailed receipts and broker logs remain private and can contain requests
or provider output: do not publish them as diagnostic bundles.

Startup recovers durable planning/handoff intents before opening HTTP. Each
resumed run takes its execution lock and reconciles the exact operation receipt,
container identity and provider turn before dispatch. Uncertain outcomes remain
`recovery_needed`, retain ownership and need explicit operator inspection/resume.
Do not create a fresh request ID to recover the same work. Client disconnects do
not cancel execution; SSE supports saved event cursors. Cancellation first saves
`stopping`; only confirmed worker cleanup permits terminal cancellation.

SIGTERM and Ctrl+C stop new supervision, drain HTTP and current state transitions
for at most 25 seconds (worker transition drain: 20 seconds), then exit. Compose
allows 35 seconds. Shutdown preserves unfinished runs rather than cancelling
them. Independent worker containers can survive controller death. On restart the
saved reconciliation path inspects them; never start a second controller to help.

## Consistent backup and restore

Use an offline maintenance window. Stop new client mutations, wait for active
operations to finish or cancel them and confirm cleanup, then stop the controller.
Do not run standalone CLI/brokers against this data during maintenance. A parked
approval/planning session is eligible when all its worker operations have stopped.
The snapshot helper also refuses a live service/execution lock or any running
worker with this controller's owner label. Keep the Docker daemon available for
that check. It does not stop unrelated containers.

```bash
docker compose --env-file .env.production stop
python3 scripts/factory-snapshot.py backup \
  --data "$FACTORY_DATA_DIR" --workspace "$FACTORY_WORKSPACE" \
  --snapshot /absolute/private-backups/factory-2026-11-05
docker compose --env-file .env.production up -d --no-build --wait
```

This copies **all** databases, WAL/SHM, evidence/receipts and checkout data together,
checks SQLite integrity on disposable copies and records file/mode/link hashes.
Never copy just a live main SQLite file. A failed/partial copy has no usable
manifest. Snapshot contents include private provider state: restrict access,
encrypt off-host copies and retain the corresponding immutable controller/worker
images, token/login configuration and this environment file separately.

To restore, stop all clients/controllers and verify no surviving operations. Keep
the failed directories for inspection, moving them aside so **both original paths
are absent**. Run as the original UID/GID (restoring as root requires ownership
correction before startup). Retain the original absolute mount paths, worker image,
seccomp profile and provider auth. Restoring under different paths is unsupported:
workspace IDs, claims, receipts and daemon owner labels bind to the originals.

```bash
python3 scripts/factory-snapshot.py restore \
  --data "$FACTORY_DATA_DIR" --workspace "$FACTORY_WORKSPACE" \
  --snapshot /absolute/private-backups/factory-2026-11-05
docker compose --env-file .env.production up -d --no-build --wait
```

Restore refuses existing destinations and mismatched hashes. An interrupted
restore leaves incomplete directories: keep the service stopped, remove only
those known incomplete copies and retry from the intact snapshot. Never restore
old receipts while newer workers still exist. Inspect saved status/evidence before
answering the original interaction or resuming an eligible run.

## Upgrade, rollback and old runs

Record controller image digest, worker image ID, main SHA, configuration and the
release evidence. Drain active operations and take the consistent snapshot above
before replacing the controller. Run acceptance on a separate copy first.
This release supports Factory schema/envelope 1, control schema 1 and saved
`factory-fake-v1`, `factory-sdlc-v1`, `factory-grill-v1` graphs; newer versions
fail closed. There is no automatic downgrade or LangGraph checkpoint conversion.
Keep old images/provider homes and do not silently replace the saved backend
image/profile. Changing that backend requires a separately reviewed migration or
a new data directory while retaining the old recovery deployment.

For a compatible controller-only upgrade, change `FACTORY_CONTROLLER_IMAGE` to
the tested digest and `up -d --no-build --force-recreate --wait`. Inspect pending
interactions and claims before accepting new work. To roll back, stop clients and
the replacement; reconcile all surviving workers first. Use the prior image with
unchanged compatible storage, or restore the **whole pre-upgrade data + checkout
snapshot** if schema/state compatibility is uncertain. Never downgrade only the
SQLite file or combine an old database with a newer checkout/receipts.

Existing Python runs must finish/drain in their original Python environment or
retain that interpreter, dependency lock, original storage/checkpoints, provider
homes, protected-worker runtime and checkout for recovery. Do not point ADK at
Python state. Planning archive import is a deliberate, bounded operation described
in [interface migration](interface-migration.md), not implementation-run migration.
Keep both release paths available until old-run recovery and rollback are verified.

## Repeatable release acceptance

Run `bash scripts/check-rust.sh` and `bash scripts/check.sh`, then build the
controller for the test operator UID/GID, build `containers/test-worker.Dockerfile`
as `factory-test-worker:dev`, and run `python3 scripts/accept-rust-production.py`.
The latter runs real Compose and Docker with an explicitly simulated provider,
restores a pending planning session, completes exact approval through report,
recreates the controller during work, checks dispatch counts and inspects actual
image permissions/mounts. It never targets an existing deployment. CI retains its
JSON evidence alongside the earlier durable-container and protected-worker tests.
Use a non-root test operator with local Docker access.

Before cutover, fetch current `origin/main`, record its SHA and compare
`a9358d80fde74bf4ebfa5f7e967f2c98d8a515f0..origin/main`. Reconcile every new behavioral
change into the prerequisite parity inventory, rebase/retest the candidate and
record the tested source/image hashes. A previously closed migration issue alone
does not certify a changed main. Complete the remaining live-provider, clean-host,
browser, actual reboot and rollback gates in [issue 19 evidence](issue-19-acceptance.md)
before removing the default Python/LangGraph path or declaring production cutover.
