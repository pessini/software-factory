# Factory contracts before migration step 1

Baseline: main `1750eb3`, including shared control (#9) and Grill planning / exact
revision handoff (#10, PR #14). Python stays supported; the Rust slice uses a
separate `factory-rust.sqlite` database and does not read LangGraph checkpoints.

| Contract | Current source and behavior | Step 1 disposition |
| --- | --- | --- |
| State / routing | `state.py`, `graph.py`, `workflow_nodes.py`; pending/running/waiting/recovery/stopping and terminal outcomes; bounded repair/review loops | Versioned subset: pending, running, waiting_for_input, recovery_needed, stopping, ready_for_review, failed, cancelled; fake graph only |
| Events | `events.py`, `persistence.py`; schema 1, run-scoped increasing sequence, UTC timestamp, node/status/attempt/message/metadata | Matching field vocabulary in an explicitly separate Rust API; persist before returning |
| Runs / operations | `persistence.py`, `engine.py`, `runtime.py`; saved start requests, immutable operation identities, dispatch intent, provider receipts, deadlines and reconciliation | Separate Factory run/operation ledger alongside ADK checkpoints and run leases |
| Settings | `config.py`, `settings.py`, `session_settings.py`; validated configuration, model/effort selections, session settings snapshots and presets | Only validated slice limits are ported; no model configuration or provider execution |
| Sessions | `persistence.py`, `session_presentation.py`, `control.py`; stable workspace/session/run linkage, history and interface bindings | Stable workspace/session binding plus file-backed ADK session; interface history deferred to #18 |
| Human input | `interactions.py`, `persistence.py`; stable interaction IDs, exact question set, nonempty answers, duplicate answer validation | One typed boolean approval, immutable identity and conflict detection; no publishing authorization |
| Planning | `contracts/planning.py`, `planning.py`, `planning_control.py`; Grill topics/questions, immutable revisions, plan SHA-256, approval bound to the exact revision and one implementation handoff | Inventory only. Existing functionality stays in Python; full port belongs to #18 |
| Control | `control.py`, `planning_control.py`; start/status/answer/resume/cancel, events/artifacts, saved launch intents and independent jobs | Experimental Rust start/status/answer/resume/cancel and independent polling process; CLI/ACP/MCP/HTTP parity deferred |
| Execution / ownership | `execution.py`, `worker_manager.py`, `ownership.py`, `worker_records.py`; protected workers, process identity, exclusive writers, uncertain-operation recovery | Injectable fake execution adapter with its own durable receipt database and OS workspace lock; protected Docker worker port belongs to #17 |
| Delivery / evidence | `evidence.py`, `delivery.py`, `delivery_reviews.py`; tested-version evidence, explicit saved publication policy | Fake result artifact only; no live providers, commits, pushes, PRs or deployment |

Rust JSON has its own schema/graph versions and is not a drop-in Python wire or
checkpoint format. A slice approval authorizes only the deterministic fake worker;
it does not represent planning revision approval or reviewer acceptance. The shipped
planning UI is not rebuilt by this milestone. Router #13 and Wayfinder #15 remain
separate.
