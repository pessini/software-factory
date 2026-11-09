# Software Factory

A Rust/ADK controller for durable planning, implementation, protected workers,
review evidence, and authorized PR delivery. The installed `software-factory`
command, browser UI, BB ACP adapter, and Codex MCP adapter share the same Rust
service and saved identities. Controller and client hosts need no Python, uv, or
LangGraph. Target repositories and optional worker toolchains choose their own tools.

Start with the [native and Compose quickstarts](docs/startup.md), including the
[dependency matrix](docs/startup.md#dependencies), provider/backend setup and
client connection steps. Native controller packaging works with either protected
Docker workers or the supported Linux native subprocess backend. Compose runs
from retained controller/worker images without host Rust/Cargo. Neither product
path requires host Python.

```sh
just --list                      # discover services, client commands and helpers
just build                       # pinned Rust/Cargo 1.99.1
# Set stable FACTORY_WORKSPACE, private FACTORY_DATA_DIR and client token;
# initialize/review target policy and configure a real execution backend first.
just start                       # foreground native service; Ctrl+C preserves work
# In another terminal with the same workspace/token:
just plan plan-001 'Describe the change'
just runs
```

For installed binaries, executable collisions, BB/Codex registrations and the CLI
inventory, see the [native product guide](docs/native-product.md). Clients connect
to the independently started service at `http://127.0.0.1:4317` by default;
explicit non-loopback connections require HTTPS.

The [Rust workflow](docs/rust-workflow.md),
[shared control contract](docs/rust-interfaces.md), and
[production topology](docs/production-deployment.md) describe execution and storage.
Exact approval, workspace ownership, durable recovery, cancellation cleanup, and
protected worker boundaries apply across every interface. Approval of a plan
grants no publication or deployment authority.

The native entry points do not decode LangGraph state. Keep old runs in their
[separate Python recovery environment](docs/interface-migration.md).
Release acceptance, current cutover readiness, actual production cutover (#27),
and complete legacy retirement are distinct milestones. Startup documentation
does not declare production cutover or recovery retirement.

## Development

```sh
bash scripts/check-rust.sh
```

Rust protocol/behavior checks do not install the Python application. The separate
legacy CI job runs `bash scripts/check.sh` until recovery retirement is evidenced.
See [issue 25 validation](docs/issue-25-acceptance.md) for scope and test evidence.

Optional Linux native subprocess execution and its enforced policy requirements are described in [native execution](docs/native-execution.md). Docker remains selectable.
