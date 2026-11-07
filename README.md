# Software Factory

A Rust/ADK controller for durable planning, implementation, protected workers,
review evidence, and authorized PR delivery. The installed `software-factory`
command, browser UI, BB ACP adapter, and Codex MCP adapter share the same Rust
service and saved identities. Controller and client hosts need no Python, uv, or
LangGraph. Target repositories and optional worker toolchains choose their own tools.

Build and install from a checkout with the pinned Rust toolchain:

```sh
cargo install --path rust/factory-runtime --locked --bin software-factory
software-factory --version
software-factory init -w /absolute/target
# Review .factory/factory.yaml and configure the protected Docker backend:
software-factory configure-docker /absolute/backend.json \
  --storage /absolute/private/factory-data -w /absolute/target
export FACTORY_HTTP_TOKEN='your-private-token-at-least-24-characters'
software-factory web --storage /absolute/private/factory-data -w /absolute/target
```

In another terminal, configure the same token and start work:

```sh
export FACTORY_HTTP_TOKEN='your-private-token-at-least-24-characters'
software-factory plan 'Describe the change' -w /absolute/target --request-id my-plan
software-factory plan-status PLANNING_SESSION_ID -w /absolute/target
software-factory plan-approve PLANNING_SESSION_ID EXACT_PLAN_HASH -w /absolute/target
software-factory mcp -w /absolute/target
software-factory acp -w /absolute/target
```

An unset `FACTORY_CONTROL_URL` selects `http://127.0.0.1:4317`. The service must be
started independently; clients never fall back to another engine or own run
lifetime. Explicit non-loopback connections require HTTPS. See the
[native product guide](docs/native-product.md) for installation collisions,
upgrade/rollback, client setup, and the complete CLI compatibility inventory.

The [Rust workflow](docs/rust-workflow.md),
[shared control contract](docs/rust-interfaces.md), and
[production topology](docs/production-deployment.md) describe execution and storage.
Exact approval, workspace ownership, durable recovery, cancellation cleanup, and
protected worker boundaries apply across every interface. Approval of a plan
grants no publication or deployment authority.

The native entry points do not decode LangGraph state. Keep old runs in their
[separate Python recovery environment](docs/interface-migration.md).
Deployment UX (#26) and actual production cutover (#27) are separate follow-ups.

## Development

```sh
bash scripts/check-rust.sh
```

Rust protocol/behavior checks do not install the Python application. The separate
legacy CI job runs `bash scripts/check.sh` until recovery retirement is evidenced.
See [issue 25 validation](docs/issue-25-acceptance.md) for scope and test evidence.
