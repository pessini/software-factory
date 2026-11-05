# Rust shared interfaces and planning (#18)

Migration step 3 adds `factory-grill-v1`, the existing planning Web UI, and a
version-1 control API to the durable Rust/ADK service. It does not replace or read
LangGraph checkpoints. #19 owns production cutover. The historical step-1
[contract inventory](rust-contract-inventory.md) remains a record of that release;
the table below describes the current interface contract.

## Run the service

Configure the target's `.factory/factory.yaml`, then configure the protected
Docker backend as described in [the workflow guide](rust-workflow.md). A service
without a Docker backend retains the step-1 fake worker, which cannot run real
Grill/implementation specialist prompts. Provider/model choices come from the
configured worker's catalog. HTTP clients cannot submit worker configurations,
workspace paths, execution policies or delivery authorization.

```sh
FACTORY_WORKSPACE=/absolute/checkout FACTORY_DATA_DIR=/absolute/private/data just start
```

`serve` now starts HTTP on `127.0.0.1:4317` and prints a JSON ready record containing
the planning URL. Open the `?token=...` link. Token exchange uses the existing
HttpOnly, SameSite=Strict cookie (Secure for HTTPS) and redirects to a URL without the token. Restart
prints a new token unless the operator supplies `FACTORY_HTTP_TOKEN` (at least 24
characters). The service owns the workspace; arbitrary client paths convey no
execution authority. Storage and configured mount paths must remain stable.

For existing clients, configure the same service origin and token:

```sh
export FACTORY_CONTROL_URL=http://127.0.0.1:4317
export FACTORY_HTTP_TOKEN='the operator-configured token'
software-factory plan-status -w /absolute/checkout
software-factory mcp -w /absolute/checkout
```

CLI, BB ACP and MCP use their existing commands/tool names. All planning and
start/status/answer/resume/cancel/models/settings operations route to Rust when
`FACTORY_CONTROL_URL` is set. Without it, the Python controller remains available
for original saved runs. ACP keeps chat presentation/history locally; Factory
run IDs and terminal outcomes remain separate from chat turns. `/attach RUN_ID`
validates ownership through the service. Creating/reloading a chat never resumes
a terminal Factory run. Disconnecting any adapter does not cancel execution.
The BB settings panel uses the service model catalog and settings snapshots.

Remote/public access, including a loopback backend behind a TLS proxy, requires
`FACTORY_HTTP_HOST` (the externally allowed host and port), `FACTORY_CALLER_ID`
(saved approval actor), `FACTORY_HTTP_TOKEN`, and an HTTPS `FACTORY_HTTP_ORIGIN`
matching that host. Set `FACTORY_HTTP_BIND` when changing the listener. One configured
bearer identity owns one workspace. Restrict access to that operator identity;
use a TLS reverse proxy for remote clients (the client adapter requires HTTPS
off loopback). The service rejects other Host headers, cross-origin mutations,
unauthenticated assets/API calls, and non-JSON writes. Clients must be configured
for the service's canonical workspace; the adapter checks it before mutations.
This is not a multi-tenant identity/role system.

## Contract inventory and intentional changes

| Surface | Preserved contract / current mapping |
| --- | --- |
| Browser | Existing `web_static/` HTML, CSS, JS and font are embedded unchanged. `GET/POST /api/sessions`, `GET /api/sessions/{id}?after_event=N`, and `POST .../answer,revise,approve,resume,cancel` retain the planning view. `GET .../report` returns validated controller-owned report content. Polling remains supported. |
| Shared control | `POST /api/control/{operation}` takes a JSON object. Operations: `start`, `status`, `list`, `answer`, `resume`, `cancel`, `models`, `settings`, `plan_start`, `plan_list`, `plan_status`, `plan_answer`, `plan_revise`, `plan_approve`, `plan_resume`, `plan_cancel`, `plan_report`. Bearer or `x-factory-token` authentication also works without browser cookies. |
| CLI | Existing `run`, `inspect`, `answer`, `resume`, `cancel`, `models`, `plan`, `plan-status`, `plan-answer`, `plan-revise`, `plan-approve`, and `mcp` use the remote boundary when configured. Native operator CLI additionally supports `control OP JSON`; existing native raw `start/implement/status/...` commands remain supported. |
| BB ACP | Existing `/plan`, `/plans`, `/plan-status`, `/plan-answer`, `/plan-revise`, `/plan-approve`, `/new`, `/attach`, `/status`, `/continue`, `/answer`, `/cancel` retain saved identity. Remote replies contain the saved service state; local chat events are not workflow checkpoints. |
| MCP | Existing `factory_start/status/answer/resume/cancel/models/settings` and `factory_plan_start/list/status/answer/revise/approve/cancel` names/arguments remain unchanged. Tool calls do not own run lifetime. |
| Settings | `settings` with `session_id` reads/changes selections; `run_id` reads the immutable saved configuration and rejects changes. Start can carry `selection`. Shared configuration/policy paths remain operator-owned. |
| State | Runtime state schema 1 gains optional planning input/state and terminal `planned`; graph versions remain separate (`factory-fake-v1`, `factory-sdlc-v1`, `factory-grill-v1`). Unknown versions fail. Planning session/control storage is separately versioned with SQLite `user_version=1`. |
| Output changes | Rust run views expose saved questions, events, delivery, model settings and `handoff.json` as the report. Python-specific jobs/child-process internals are not Rust execution authority. Config/JSON errors are HTTP 400, ownership/stale/conflict errors 409, missing records 404, missing auth 401. Older Python web ValueErrors used 409 for all validation. Native run IDs are SHA-derived rather than Python `fct_...`. |

Control payloads use the existing field names: `request`/`request_id` to start,
`session_id` for planning, `run_id` for implementation, `interaction_id` plus
`answers` to answer, `feedback` to revise, and `plan_hash` to approve. IDs must
refer to saved state owned by the configured workspace. The native raw run API
keeps its bounded ASCII request/session ID contract; external start request IDs
are hashed and accept up to 200 characters for planning.

Delivery remains under the existing native operator `deliver/fix-reviews` controls
and saved publication policy. A plan approval authorizes implementation only.
The Python engine-specific delivery CLI remains a Python recovery interface;
it refuses Rust IDs instead of silently operating on another ledger.

## Durable planning and exact approval

The Grill interviewer uses the protected read-only Planner role. The service
saves each full execution request before dispatch, reconciles existing receipts,
checks the repository around each turn, and saves adaptive round envelopes and
answers. `[discuss]` remains explanation/re-question input. Topics retain decision
dependencies; plans retain glossary terms, ADR decisions, affected/stale decisions,
document hashes and previews, assumptions, exclusions and deferred work. At most
five questions per round and 100 rounds per revision are permitted.

Every revision is a distinct durable ADK run. The graph's interrupt becomes a
saved queue wait; answers save the exact question set before wake. Work runs in a
bounded background supervisor, independently of ordinary ADK invocation/SSE.
Planning uses a per-run lock and does not reserve the implementation workspace.
Read/status endpoints never dispatch workers. A saved answer, cancellation, or
expired deadline can be recovered after restart.

New plan hashes are SHA-256 of Rust's serialized snapshot bytes (sorted JSON
object keys). Existing Python snapshots keep their original sorted, spaced JSON
bytes and hash through explicit import. A revision cannot be edited in place.
Approval compares the exact hash, verifies evidence, and saves an immutable
handoff intent with actor, timestamp, decision ID, stable request identity and
full execution settings before creating the implementation run. A crash in that
gap is repaired by the supervisor. CAS on session history arbitrates approval
versus revision and concurrent approval. Repeated approval returns one run;
a different/stale hash fails. Completion checks plus glossary/ADR obligations are
required implementation criteria. Later revisions never mutate that input.

## Events

`GET /api/runs/{run_id}/events` is SSE with numeric `id:` equal to the persisted
run sequence. Supply `Last-Event-ID: N` or `?after_event=N`. IDs are scoped to the
run; a new planning revision has a new run ID/cursor. Events are committed before
publication. The stream repeatedly tails the authoritative saved cursor, so
catch-up and live delivery use the same path without a subscribe gap. Deduplicate
by `(run_id, sequence)`. A 64-event client queue disconnects slow consumers; they
reconnect from their last consumed ID. Client disconnect only drops that stream.
Polling returns the same events with `event_position` and remains compatible.

## Explicit Python saved-session import

Stop the original web service and reconcile its controller and worker operations.
Export only terminal or saved waiting planning revisions. Active/uncertain work
and an active linked implementation must finish or be cancelled through the
original controller. No active provider process is re-owned or checkpoint decoded
by Rust. Keep the original storage for recovery and historical evidence.

```sh
# With FACTORY_CONTROL_URL unset, using the original private Python storage:
software-factory --storage /absolute/python-data export-planning \
  -w /absolute/checkout > /absolute/planning-export.json
# Stop the Rust service during this explicit operator import:
factory-runtime /absolute/rust-data /absolute/checkout import-planning \
  /absolute/planning-export.json
```

Import preserves session IDs, revision history, original snapshot hashes/bytes,
submitted rounds, pending questions, source events and terminal implementation
linkage. The entire source archive is retained under `imports/<sha256>.json`.
Imported event metadata retains original run IDs/sequences. New native run and
interaction IDs are returned by status; clients refresh status before answering.
A repeated import never overwrites new progress; changed source archives conflict.
Historical linked implementations stay historical, including their saved report
content; they are never relabelled as verified Rust execution evidence or replayed.
A repeated approval of an already implemented imported plan returns its saved link.
Revise explicitly to request new implementation work.

The offline import/service lock prevents a service from dispatching half-imported
state. The importer creates complete saved run documents before scheduling; an
interrupted import can be retried with the same archive. Back up all data volumes,
including the control database, domain/ADK database, WAL/SHM files, receipts and
evidence. Never copy an active LangGraph database into Rust storage.
