# Existing records and recovery

Existing BB storage is preserved. New Factory runs use context version 2 in
Factory-owned storage. Old records retain their original version, task ownership,
checkpoint IDs, receipts, and artifacts. They are not silently moved or reowned.

Inspect an old run without opening BB or writing to its database:

```sh
software-factory legacy-inspect RUN_ID /path/to/original/bb-storage PARENT_TASK_ID \
  --workspace /path/to/original/target
```

For an active or uncertain BB-backed run, restore the original `BB_THREAD_ID`,
`BB_PROJECT_ID`, `BB_ENVIRONMENT_ID`, and `BB_THREAD_STORAGE`, keep BB available,
and use `legacy-resume` or `legacy-cancel`. These commands retain the old BB worker
authorization path. Never copy an active database into Factory storage or edit
its version to bypass missing execution evidence. Older unsupported evidence
versions must be stopped through the original path before new work starts.

After confirmed cleanup, start a new Factory run. Historical results remain in
place and inspectable. There is no active-run migration or automatic history
conversion in this release. New shared controls reject legacy records with a
reference to this procedure. Completed `accepted` results remain historical,
unverified evidence; `ready_for_review` and delivery gates are unchanged.

## Policy conversion

Version-1 repository policies still load. For a new independent run the controller
saves the original policy as `config-source.json` and creates a version-2 copy
with an explicit native worker connection. Limits, model selections, review mode,
and delivery authorization are preserved. The target policy file is unchanged.

`software-factory convert-config --workspace /path/to/target` prints the converted
copy for review. Version 2 requires `worker_connection` with `runtime: codex`,
`model_connection: native`, and `codex_version: '0.154.0'`. Unsupported connection
types fail validation. This is not a Router compatibility claim.

## Independent job recovery

Start IDs, job launch records, and worker operation receipts are in the same
Factory database. The process gate opens only after its process identity is
saved. Each worker also saves a dispatch marker before launch, then a request hash,
provider thread/turn IDs, and a receipt before returning output.

After a controller crash, inspect the saved run, then use `resume`. Factory matches
existing operations and receipts before progressing. It retains independent
worker processes during this reconciliation. A worker with uncertain dispatch is
reported as `recovery_needed`; its implementation request is not sent again.
If a start was interrupted before model settings were saved, cancel that run and
start a new request after confirmed cleanup. Do not supply a fresh request ID to
bypass a workspace reservation.

Cancellation is durable before any stop signal. A missing or mismatched container,
an unknown worker process, or an unconfirmed exit keeps ownership reserved. Retain
the storage and inspect the matching receipts. Changing interfaces grants no
permission to commit, push, merge, or deploy.
