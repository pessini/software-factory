# Ubuntu confinement fix and remaining provider acceptance

Recorded 2026-11-16. **The Ubuntu nested-sandbox mount failure is fixed and
verified. Authenticated real-provider clean-host acceptance remains incomplete.**
The user chose to keep the current ChatGPT/Codex login on WSL. The stale login
already present in the test VM was removed; the current login was not exported.
The subsequent VM workflows used an explicitly simulated provider.

The [machine record](acceptance/issue-26-ubuntu.json) separates real Codex sandbox
checks, simulated-provider workflows, actual client connections and production
reconnects. It deliberately sets `complete_clean_host_acceptance` to `false`.

## Candidate and host

- Source: `6c43fb0e7e0582249afc56d1e5e195a850549d3f`.
- Controller/broker image:
  `sha256:ad9101ad4570f16b3f8aaf55b794812d560566c68a866beda75f5089e6b140a6`.
- Real pinned Codex worker used for credential-free sandbox probes:
  `sha256:5f5c3b7fb6c78332c22d2e501b54543410842da511170c149f52e9391c2ab579`.
- Explicit protocol-simulation worker, built from retained local layers without
  network access:
  `sha256:383f7367dc2ee8651318ef62611a01db1f324b0dd7637c554b1f68133e6c99a6`.

Testing ran on this computer in the retained Ubuntu Noble QEMU VM, originally
provisioned on November 10 from a checksum-verified base with an empty Docker
cache. November 16 used fresh target checkouts and private storage; it was not a
new pristine-host provisioning. No host Cargo/rustc was installed in the guest.
Docker was 29.1.3, Compose 2.40.3, packaged just 1.21 and kernel 6.8.0-147.
The native-controller path used an installed Rust binary with Docker workers;
the second path used the Compose controller and daemon brokers. These checks do
not claim Docker-free native-execution acceptance.

## Failure, fix and verification

The same credential-free `codex sandbox -- /bin/true` probe failed under
`docker-default` with `bwrap: Failed to make / slave: Permission denied` and passed
under the explicit [named worker AppArmor profile](docker-worker-apparmor.md).
All other container confinement settings were identical. The user explicitly
authorized loading this profile only in the isolated VM.

The profile permits the namespace mounts required by Codex. Actual probes still
denied writes to the outer read-only root and protected bind, including an
attempt to remount that bind writable as root inside a nested user namespace.
The worker had no Docker socket or private Factory database. Representative
worker inspections also verified non-root execution, dropped capabilities,
seccomp, read-only root and the existing 2 GiB/2 CPU/256 PID limits. These are
specific protection checks, not a claim that the broader mount allowance is
equivalent to Docker's default AppArmor policy.

A second issue appeared on software-emulated hardware: valid model-catalog
responses took about 38–40 seconds, exceeding the client's former 30-second
deadline. Clients now allow 90 seconds to cover the service's existing 60-second
catalog budget and cleanup. Native and Compose CLI catalog checks then passed;
worker execution budgets were not increased by this code change.

| Check | Result |
| --- | --- |
| Real pinned Codex sandbox, without account credentials | Passed with the named profile, including after guest reboot |
| Native installed binary and retained-image Compose startup/CLI | Passed |
| Exact planning approval → implementation → quality → review | Passed on both paths with the simulated provider; only `greeting.txt` changed |
| Whole-state backup and restore of pending approvals | Passed; the exact hashes survived and were approved after restoration |
| Controller recreation and saved result recovery | Passed |
| Active implementation cancellation | Passed on both paths; workers confirmed stopped and target content preserved |
| Whole guest reboot | Profile returned in enforce mode; saved run IDs, reports and event positions matched |
| Browser after reboot | Both authenticated clean URLs connected and displayed the final reports |
| Actual terminal Codex MCP after reboot | Both returned the expected `ready_for_review` run through `factory_status` |
| Actual BB ACP after reboot | Explicit `acp-default` conversations read both expected completed runs |
| Authenticated real-provider workflow inside Ubuntu | Not completed |

BB's cold Compose model-catalog discovery timed out on the software-emulated VM.
The subsequent explicit `acp-default` conversations succeeded. This records the
actual saved-run journey without claiming every cold catalog request passed.
Codex 0.169.0 ran and authenticated on WSL; its Factory MCP requests went to the
VM over SSH. BB was 0.49.0, started with its AppImage extraction mode because
FUSE was unavailable. No account login was mounted in either simulation backend.

## Source and bounded worker checks

Rust 1.99.1 formatting, all-target typechecking, Clippy with warnings denied and
the full ordinary suite passed: **86 tests passed, 11 opt-ins ignored**. A prior
parallel run hit the existing short-budget human-wait timing test under load;
the focused rerun and final serial suite passed. Homebrew's newly installed Rust
1.100 was not used for final acceptance; validation used a temporary pinned
1.99.1 toolchain.

The exact committed source also passed `scripts/accept-rust-project-worker.sh`
in the retained Rust worker `sha256:cf9c10df8901a0f14fe1a23b4cc0396c2c9867d36be36f7efbe74e8fec12fd69`:
86 tests, 11 ignored, offline, with a read-only checkout and the existing worker
resource bounds. No provider credentials or Docker socket entered that check.
Standards review: zero actionable findings. Spec review: zero actionable findings.

## Production and remaining gate

The [named WSL production deployment](issue-27-rust-acceptance.md) was not
restarted or upgraded for these tests and remained healthy. Fresh terminal Codex
and the existing production BB conversation independently read its real Rust run
as `ready_for_review`. The new optional AppArmor setting preserves omitted-field
serialization for historical backend identities.

The first Ubuntu real-provider attempt reached a separate stale-login failure:
`Auth 401`, refresh token already used. Its failed plans were retained and
cancelled. Automatic approval rejected exporting the current account credential
without explicit authorization; the user selected **“Keep the login on WSL
only.”** No current account credential was exported, and simulated workflows are
not counted as authenticated-provider acceptance.

The remaining #26 gate is a real-provider workflow on Ubuntu with an independently
authorized usable login, followed by the affected client/recovery checks. #27
therefore remains open. Existing WSL production evidence can be reused for its
unchanged deployment, but it does not substitute for that Ubuntu result.

The VM is powered off, its disk/state/artifacts remain private under
`.checks/issue-27-clean-vm`, and its temporary loopback helpers were stopped.
The two VM-only BB providers were disabled; production's provider remains
available. Historical production state and recovery obligations were not deleted.
