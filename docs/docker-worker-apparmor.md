# Docker workers on AppArmor hosts

Pinned Codex 0.154.0 creates a nested filesystem sandbox. On an AppArmor-enabled
Ubuntu Docker host, `docker-default` denies its namespace mount operations, causing
`bwrap: Failed to make / slave: Permission denied`. Service health and model
catalog checks do not exercise that sandbox.

For this topology, review and load the named profile on the **Docker daemon host**:

```bash
sudo install -m 644 containers/worker-apparmor /etc/apparmor.d/software-factory-worker
sudo apparmor_parser -r /etc/apparmor.d/software-factory-worker
```

The profile permits namespace mounts required by the inner sandbox. It retains
kernel-file restrictions. The outer worker still runs as a non-root UID with
all capabilities dropped, no-new-privileges, the reviewed seccomp filter,
read-only root/protected binds, and bounded memory, CPU and PIDs. This is a broader
mount permission than `docker-default`, not equivalent AppArmor confinement.
It is not a global AppArmor disablement or a privileged container. Only workers
explicitly configured with this name receive the profile; trusted controllers
and daemon brokers retain their existing Docker settings.

Add this optional property to the reviewed worker backend JSON before
`configure-docker` or `just container-configure`:

```json
{"apparmor_profile":"software-factory-worker"}
```

This fragment supplements the complete backend JSON; it is not a replacement.
The name refers to a profile already loaded on the daemon host, including when
the controller runs inside Compose. Docker fails if the selected profile is
missing; Factory never retries with AppArmor disabled. The value `unconfined`
is rejected. Omit the property to retain the daemon's existing default policy.
Omitted properties stay omitted in saved legacy configurations and receipts.

A backend is immutable once saved. Use separate private storage for a changed
profile and retain the old storage, checkout, images and recovery obligations;
do not edit a saved backend underneath existing runs. The selected name is saved
with the backend and its execution identity, but Factory does not pin the
daemon's loaded AppArmor policy bytes. Treat profile installation and updates as
operator-managed host configuration, and retain its source/checksum with release
evidence.

After reboot, verify the host AppArmor service loaded the profile and run a real
provider workflow. Readiness probes alone cannot establish execution support.
