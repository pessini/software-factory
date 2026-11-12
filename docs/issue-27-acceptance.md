# Issue 27 production adoption evidence

**Follow-up recorded 2026-11-12:** the
[current Rust production record](issue-27-rust-acceptance.md) supersedes the
documentation-only setup described below. Full Rust verification and a real Rust
task now pass, the scope defect is fixed and the original glossary is archived.
The clean-host provider gate remains blocked by host confinement, by explicit
user decision. The following sections preserve the initial adoption evidence.

Date: 2026-11-10 (America/Sao_Paulo). **A named WSL production workspace is now
operating through Rust.** Production workflow, whole-state restore, controller
restart, CLI/MCP/ACP/browser reconnect and cancellation checks passed. This is
the user's selected ongoing Software Factory workspace, outside `.checks`, not a
disposable acceptance fixture. Issue 27 is not declared fully complete: the
current-candidate clean-host acceptance tracked under #26 remains unverified.
The user independently confirmed the desktop Factory tool returned
`ready_for_review` for this same production run.

The [machine record](acceptance/issue-27-production.json) contains exact hashes,
identities, receipts and limitations. The [cutover procedure](production-cutover.md)
defines the general inventory, recovery, freeze and rollback requirements.

## Production inventory and dispositions

| Item | Observed production state |
| --- | --- |
| Host/operator | WSL2 Linux x86-64, kernel `6.18.33.2-microsoft-standard-WSL2`, UID/GID 1001 |
| Target | `/home/lpessini/dev/software-factory-production`, dedicated Git checkout selected by the user |
| Release source | `e50adb1e3994ec0e05439134ac2bc520b751710e`, also current GitHub main at revalidation |
| Target policy commit | `5516c2b998db4e27b8b39b3582cd9a66c4d4f770` on `codex/production-recovery-docs` |
| Private deployment root | `/home/lpessini/.local/share/software-factory-production`, mode 0700 |
| Active Rust storage | `data-on-request` under that root, separate from all historical state |
| Controller | Compose project `factory-production`, service `controller`, loopback `127.0.0.1:4317`, non-root, restart `unless-stopped` |
| Prior production controllers/runs | None for this newly created deployment; preflight found no running Docker containers and no listener at 4317 |
| Legacy production runs, approvals, workers | Zero in this named workspace: it was created during this operation and has no inherited legacy database |
| Legacy disposition | No production legacy environment is needed for this workspace; no historical `.checks` state or environments were deleted |

This inventory covers the selected deployment. Missing default directories are
not proof that other hosts/custom storage have no runs. Historical disposable
acceptance state remains under the source checkout's ignored `.checks` directory;
it is excluded from the production inventory and retained. Full LangGraph
retirement across unspecified hosts is not asserted.

An initial Rust planning attempt, `pln_8e867d3dd837`, stopped on a provider-policy
mismatch before recording a provider turn. Explicit `plan-cancel` reached
`cancelled`; all three saved receipts confirmed stopped and no workers remained.
Its original `data` directory and complete snapshot are retained. The accepted
backend was configured in **fresh** `data-on-request` storage; no saved backend
was edited under an existing run. It explicitly uses the managed account's
`on-request` policy, as earlier live acceptance did. Brokers continue denying
all escalation requests. No fake-worker fallback was enabled.

## Immutable release and revalidation

The controller was built from current main with the existing pinned Dockerfile,
Rust/Cargo 1.99.1 and operator UID/GID 1001. The deployed image ID is
`sha256:d81871252788edbf7bb2eac7f03ec4d7cfe74e53f333c9d302b39ed97cb5c91d`.
Its broker uses that same pinned ID. The retained worker ID is
`sha256:5f5c3b7fb6c78332c22d2e501b54543410842da511170c149f52e9391c2ab579`,
with Codex 0.154.0. The copied native client SHA-256 is
`6a306aed6a065c3ac6567155e18d1df0b24424e966985c48a12f9ac432c03312`.

Both image IDs are saved in backend configuration. A private `docker image save`
archive and SHA-256 file are retained under `release/`; its archive checksum is
`bb7647916c6c810ce17e3fd46deca3b644def6c4100b30a0b8dfb80781a6d873`.
The controller-extracted client, copied Compose file, policy/backend hashes and
prior client configurations are retained separately from the state snapshots.
Private token and provider login contents are never included in public evidence.

Relative to the issue-26 live source `2c8ced1`, changes include Python application,
packaging/test/CI removal, Rust-only startup/ACP fixes and Docker acceptance fixture
relocation. Current all-target checks and the real production journey below
revalidate the changed Rust entry points and production backend. Historical
[#19 clean-host/reboot/rollback](issue-19-acceptance.md) and
[#26 live execution](issue-26-acceptance.md) remain evidence for their pinned
artifacts; they are not relabelled as executions of this candidate.

`bash scripts/check-rust.sh` passed formatting, all-target typechecking, Clippy
with warnings denied and **81 tests**; **11 opt-in tests** were ignored. The initial
sandboxed invocation failed to bind the deployment-test listener; the rerun with
local-network permission passed. After production started on 4317, final validation
also exposed the existing recovery test's hardcoded default-port collision. The
isolated test reproduced `Address already in use`; its fixture now binds
`127.0.0.1:0`, matching the other service tests. This test-only change does not
alter the deployed application/image. Production checks used real Docker and real
ChatGPT-authenticated provider calls, `gpt-5.6-sol` / low effort, with saved 600s
step and 3600s run budgets. This existing WSL development host is not a clean host.

## Backup, restore and rollback

Before controller startup, the complete configured checkout/data snapshot was
saved as `backups/pre-cutover-2026-11-10`. The safely cancelled initial-policy state
was saved as `backups/initial-policy-cancelled-2026-11-10`. With the successful
plan parked awaiting approval, the controller was stopped and
`backups/awaiting-approval-2026-11-10` was created using
`scripts/factory-snapshot.py`. The helper verified no surviving owned workers,
locked service/workspace/execution state, copied the entire data and checkout,
validated SQLite copies and wrote file/mode/link hashes.

Both original directories were moved aside and retained privately as
`retained-pre-restore-data` and `retained-pre-restore-workspace`. Restore into the
absent **original paths** succeeded. After controller recreation, session ID,
planning run ID, revision, exact plan hash, snapshot and `planned` status matched
the pre-backup record. The completed implementation was subsequently snapshotted
offline as `backups/completed-workflow-2026-11-10`; manifest hashes are published
in the machine record. No stale receipts were restored over active workers.

There was no prior production release for this workspace. Rollback to the prior
operational state means disabling this new service and its launchers; it does
not mean starting Python. State recovery uses the verified whole snapshot and
retained image IDs. With clients frozen and all workers reconciled, stop the
service, preserve failed directories, restore the matching snapshot at its
original data/workspace paths and restart the retained release. Keep the matching
`production.env`, token and client registration backups with the deployment.
The original `pre-cutover` snapshot refers to `data`, while the successful-plan
and completed snapshots refer to `data-on-request`; never mix those configurations.
An old-binary downgrade was not exercised here; #19's old-release rollback remains
historical evidence.

## Actual client adoption and workflow

| Entry point | Old → new configuration and result |
| --- | --- |
| CLI | No installed Factory command → `~/.local/bin/software-factory` links to the fixed production launcher; native Rust binary, target, origin, private token source and client home are explicit; `doctor` and saved status passed |
| Terminal Codex | No Factory registration for this target → trusted `control/.codex/config.toml`, absolute Rust MCP launcher, fixed target, read-only outer sandbox and explicit managed `on-request` approval policy; actual Codex 0.167.0 MCP calls read plan then completed run in separate processes |
| ChatGPT desktop | Same project-scoped MCP configuration in the production control folder; user independently confirmed the Factory status tool returned `ready_for_review` for the exact production run |
| BB ACP | BB absent → Homebrew cask `bb` 0.49.0, provider `acp-software-factory`, absolute Rust ACP launcher, dedicated project `proj_4aayf3hzfu`; real BB model discovery and saved-plan reading passed; thread `thr_dq4dcqbwer` attached the completed run after restart |
| Browser | No service → authenticated ChatGPT in-app browser at the loopback origin; clean post-token-exchange URL, selected plan, completed report and browser reconnect visually verified |

The native launcher reads the private token file itself, so GUI/stdio clients
need no token embedded in registration JSON/TOML. BB and CLI share persistent
private `clients` presentation storage. The Codex helper's generated `never`
approval setting was explicitly changed in this deployment to `on-request` to
match enterprise requirements; read-only sandboxing remains enabled. Global
Codex settings outside the added trusted control path were preserved.

The real production task repaired obsolete Rust recovery links in
`docs/run-recovery.md`, retaining the historical body byte-for-byte. The real
Planner produced session `pln_dfaec6690948`, revision 1, exact hash
`a66ab451c78cb20af2e53678cbdc4e49dbe08caedacac75f4921d3e60a2a2715`.
The user reviewed the described revision/hash and explicitly approved it in this
chat. Approval created implementation
`run-af57388e073e4940b1986798c66d8540a97b1aaedada8a88fe9837491240a26b`.
Planning, Builder, read-only quality verification and independent Reviewer
completed; the report is `ready_for_review`, event cursor 19. All corresponding
worker receipts confirm stopped.

The approved snapshot also contained a glossary. Its automatic implementation
obligation generated `CONTEXT.md` in addition to the requested introductory edit.
The report discloses both files. This is a limitation of the scope check: the
Reviewer accepted the required glossary alongside the intro-only criterion.
Both remain uncommitted in the production checkout for operator review; only
the recovery-introduction correction is copied into this issue branch. The
workflow's report is evidence of execution, not a claim that this additional
file met the narrower introductory-only request. No worker commit, push, PR
publication or target deployment occurred.

The completed run and report were snapshotted, then the actual controller was
stopped/recreated using retained images and stable paths. Run ID, terminal status,
result/report and event cursor matched after restart. A fresh Codex CLI process
read `ready_for_review`/`done` through MCP; BB attached the same run, and the
browser reloaded the saved plan and opened its report. An attempted in-flight
restart probe safely declined before mutation because the Builder had already
completed. No new in-flight restart or host-reboot result is claimed.

A separate real planning request, `pln_44969fdee050`, was cancelled only after
provider turn `01a1c569-6304-7ee1-8a47-143ea61b9829` was confirmed. Cancellation
first returned `stopping`, then reached `cancelled`; its receipt confirms stopped
and the Docker owner-label query found zero running owned workers/brokers.
Its state remains in production storage, not just an external fixture report.

## Operate this deployment

```bash
software-factory doctor
software-factory list
software-factory status run-af57388e073e4940b1986798c66d8540a97b1aaedada8a88fe9837491240a26b
factory-codex
bb project show proj_4aayf3hzfu
```

`factory-codex` enters the trusted control folder and launches terminal Codex.
For ChatGPT desktop, open
`/home/lpessini/.local/share/software-factory-production/control` as the project;
use its Factory tools rather than editing the target through the outer assistant.
BB's **Software Factory Production** project uses the existing checkout and
**Software Factory (Rust production)** provider. The browser's saved plan is
`http://127.0.0.1:4317/planning/pln_dfaec6690948`; fresh browser sessions need the
documented local token exchange. Do not publish token-bearing links.

Service operations use the retained configuration, without builds or pulls:

```bash
production_root=/home/lpessini/.local/share/software-factory-production
docker compose --project-name factory-production \
  --env-file "$production_root/production.env" \
  -f "$production_root/release/compose.yaml" ps
# For a planned restart, freeze client mutations and reconcile workers first.
docker compose --project-name factory-production \
  --env-file "$production_root/production.env" \
  -f "$production_root/release/compose.yaml" stop controller
docker compose --project-name factory-production \
  --env-file "$production_root/production.env" \
  -f "$production_root/release/compose.yaml" up -d --no-build --pull never --wait
```

The configured target policy and worker verify this documentation task. Before
new Rust implementation work, provision a reviewed Rust-capable worker and full
repository quality commands, preserving existing recovery state and using the
documented backend-change boundary. A successful documentation check does not
certify arbitrary code changes. WSL/Docker availability controls service startup;
no independent WSL boot/service-manager guarantee is added by this operation.

## Remaining acceptance

- Run both #26 quickstarts and actual clients on clean supported hosts using the
  current candidate. The closed GitHub issue and older VM result do not supply
  this missing evidence.
- Resolve the additional glossary artifact during production output review;
  do not describe this narrow task as changing exactly one file.
- No production legacy recovery dependency exists in this inventory's scope.
  Retain historical acceptance environments/state; deleting them requires their
  own documented disposition and is outside this cutover.

## Standards

Independent review against the branch's original `main` baseline: **0 findings**.
The review checked private-state handling, artifact identities, inventory scope,
backup/recovery ownership and accurate milestone wording.

## Spec

Independent review: **0 actionable implementation findings**. Actual production
adoption is evidenced; current-candidate clean-host acceptance remains partial.
The additional glossary artifact and task-scope limitation are explicitly recorded.
