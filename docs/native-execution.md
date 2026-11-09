# Native subprocess execution (#28)

The Rust engine owns the same planning, exact plan approval, implementation,
checks, reviews, receipts and recovery graph with either execution backend.
`configure-docker` retains the existing Docker backend. `configure-native`
explicitly selects Linux subprocess execution. CLI, browser, MCP and ACP clients
continue to call the engine; an existing host chat does not advance the graph.
`worker_connection.model_connection: native` names the Codex protocol and does
**not** select the execution backend.

## Capability and protection matrix

| Combination | Native support / enforcement |
| --- | --- |
| Linux x86-64, non-root, unprivileged user/PID/mount/network namespaces, Bubblewrap and writable delegated cgroup v2 with `cgroup.kill` | Native Codex; configuration must pass actual namespace, cleanup and read-only/builder provider-tool probes |
| WSL2 Linux with the same features and user systemd delegation | Same requirements; Windows filesystem permissions alone are insufficient |
| macOS, Windows processes, Linux without delegation/namespaces, other architectures | Refused; no unrestricted fallback |
| Codex 0.154.0, matching `codex-code-mode-host`, reviewed system/toolchain directories | Pinned by binary SHA-256; App Server version, model, effort and effective profile validated |
| Claude / other subprocess providers | Not implemented; require their own versioned protocol, effective-policy reporting, sandbox tests, durable identity and ambiguous-dispatch recovery adapter |
| Docker | Retained, including daemon broker and existing mount/isolation/recovery behavior |

Native execution shares the host kernel. It does not claim Docker's resource,
seccomp, filesystem-image or credential-isolation properties. Bubblewrap supplies
a private filesystem and PID namespace with capabilities dropped. Only reviewed
runtime directories, selected public CA/resolver files, the target workspace,
run evidence and that role's provider home are mounted. Host home directories,
controller databases/receipts and host sockets are absent. Environment inheritance
is cleared. The trusted App Server has provider-network access; its **model tools**
use the pinned provider's named filesystem/network policy with network disabled.
Verification commands run with an entirely separate network namespace, including
when Docker is unavailable. All provider escalation/tool approval requests are
denied, including with an account requiring the explicit `on-request` policy.

Planning and reviews see a read-only workspace. Builders receive a read-only
workspace plus writable binds for reviewed existing application paths. Paths
containing Git metadata, instructions, ignored files or protected policy are
split into smaller writable leaves; protected parents remain read-only. This
also prevents rename/delete of protected directories and creation of missing
protected names. Symlink destinations obey their own mount permissions; writable
files with preexisting hard links are refused. The workspace root is a private read-only directory containing read-only binds
of the real entries; empty `.codex` and `.agents` mount targets required by the
pinned provider exist only in that private view. No host instruction/policy
directories are created. Create application directories before configuration and list them in
`writable_paths`. Replacing an individually writable file through a read-only
parent (an atomic-save rename) is unavailable; use a writable application
directory without protected descendants when that behavior is needed.

Runtime directories are operator-reviewed read-only dependencies under `/usr`
or a specific `/opt` toolchain directory. Host home, `/run`, `/proc`, `/dev` and
`/sys` trees cannot be requested as runtime roots. The verified provider and its
companion are mounted as individual files, so selecting a provider under a home
directory does not expose that directory. Roots overlapping controller data,
workspace or configured credentials are rejected. Credential contents are never
copied into receipts.

## Reviewed configuration and diagnostics

Prepare a private persistent data directory outside the target checkout. Install
Bubblewrap, the standalone Linux Codex 0.154.0 binary **and its adjacent matching
`codex-code-mode-host`**, and target verification tools. Supply a stable delegated
cgroup v2 directory owned by the operator. Its lifecycle must outlive transient
clients and controller reconnects. Do not point production storage at a temporary
acceptance-test scope. See [startup](startup.md#docker-free-controller-and-native-execution) for the
Docker-free `just` quickstart and the controller/backend dependency matrix.

Example operator JSON, outside the target repository:

```json
{
  "executable": "/opt/factory/codex-0.154.0/codex",
  "bubblewrap": "/usr/bin/bwrap",
  "cgroup_root": "/sys/fs/cgroup/user.slice/USER_SERVICE/factory-execution",
  "runtime_roots": ["/usr"],
  "writable_paths": ["src", "tests"],
  "auth_file": "/home/operator/.codex/auth.json",
  "approval_policy": "never"
}
```

Use actual canonical paths. `src` and `tests` must already exist. On usr-merged
systems the sandbox recreates `/bin`, `/sbin`, `/lib` and `/lib64` links into
`/usr`; other layouts need corresponding reviewed runtime directories. Host
virtual environments and home-based login shells are not implicitly exposed.
The pinned provider requires directory-shaped writable roots. Its named builder
profile covers the workspace while the outer namespace enforces the narrower
reviewed writable directories/files; both layers are recorded in effective policy.
Checks use explicit argv and `/usr/bin:/bin`, with `HOME=/tmp`, a private temporary
cache and Python bytecode disabled. Use absolute argv for other reviewed tools.

```bash
software-factory doctor-native /absolute/native.json --storage /absolute/data -w /absolute/checkout
software-factory configure-native /absolute/native.json --storage /absolute/data -w /absolute/checkout
software-factory web --storage /absolute/data -w /absolute/checkout
```

The low-level `factory-runtime DATA WORKSPACE doctor-native/configure-native JSON`
commands perform the same checks. Diagnostics reject missing/incompatible provider
files, changed binaries, unsupported policies, unavailable namespaces and missing
cgroup delegation. Configuration hashes the provider, companion and Bubblewrap;
subsequent launches verify those hashes. Both backend files in one data directory
are an error. Changing backend or configuration requires separate storage; deleting
a configuration file cannot authorize an existing native run to use a fake worker.

## Saved authority and recovery

Run status includes `execution_backend` with backend/provider/version/policy and
reviewed configuration. Each private `worker-records/*.json` receipt includes
`backend_identity`, `effective_policy`, the provider-reported `provider_policy`,
model/effort, thread ID, turn ID and immutable request identity. Effective policy
records writable/protected paths, filesystem rules, network, approvals and check
environment. Session continuation requires the same effective policy and agent
settings; otherwise a new provider thread is created. No existing thread's policy
is silently widened. Provider substitution or an unexpected active profile blocks
execution before a turn is submitted.

The independent broker holds stdio and the operation lock after a CLI/MCP/ACP
client disconnects. Native children enter the operation cgroup in an
async-signal-safe pre-exec hook, before running any provider/tool code. Receipt
intent precedes spawn and turn submission. Brokers retain saved questions/answers,
wall deadlines and bounded output. Cancellation uses `cgroup.kill` and confirms
`populated 0`; process IDs are never reused as proof of cleanup. Empty operation
cgroups remain as cleanup evidence while the delegated parent lives.

If a broker dies, an exact saved result can be reconciled, but a turn without a
confirmed result is ambiguous and is never automatically resubmitted. Missing
cgroups or unconfirmed cleanup retain recovery and workspace ownership. Do not
remove delegation, live claims, receipts, provider homes or operation cgroups to
force recovery. Verification timeout is a failed check only after confirmed tree
cleanup; cancellation and run expiry retain their distinct outcomes.

## Acceptance checks

Ordinary `cargo test --workspace --locked` covers identity fencing and existing
Rust workflow/interface/recovery behavior. Host tests are explicit opt-ins because
they require real namespaces, delegation and (for specialist tests) provider login:

```bash
FACTORY_NATIVE_TEST_CONFIG=/absolute/native.json \
  cargo test --locked -p factory-runtime --test native_worker -- --ignored --nocapture
```

The config is read for disposable Git workspaces and private data directories;
it must use a real delegated cgroup outside those temporary directories. The
builder and end-to-end fixtures set their own application/glossary write allowances. Tests cover
actual read-only syscall/network denials and cleanup receipts; a live pinned
Codex planner; builder writes plus protected rename/delete/symlink, credentials
and network denial; and a complete Rust planning → stale-approval rejection →
exact approval → reopened engine → implementation → verification/review → report.
Two simulated-protocol fixtures additionally exercise a question sent before the
turn-start response, persisted answers/reconnect, and killing the controller during
an active turn. They use actual native processes and cgroups, with no model or
provider-enforcement claim. Timeout/cancellation tests include descendants that
create a new session. `FACTORY_NATIVE_TEST_MODEL` can select an explicit available
test model. Python in
these fixtures is a **target verification tool**, never the Factory runtime.
See the existing Docker opt-in tests in `tests/docker_worker.rs` as well.

Recorded host validation is in [issue 28 acceptance](issue-28-acceptance.md).
