#!/usr/bin/env python3
"""Actual Compose controller + protected Docker workers; simulated provider.

Uses isolated test-owned paths/project. Does not change a deployed service.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import mkdtemp

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="software-factory-controller:issue-19")
    parser.add_argument("--output", type=Path, default=ROOT / ".checks/issue-19-production.json")
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location(
        "workers", ROOT / "scripts/accept-rust-workers.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = Path(mkdtemp(prefix="issue-19-", dir=ROOT / ".checks"))
    fixture = module.Fixture(root, "journey", "factory-test-worker:dev", configure=False)
    fixture.config["limits"]["total_run_timeout_seconds"] = 300
    policy = fixture.workspace / ".factory/factory.yaml"
    policy.parent.mkdir()
    policy.write_text(json.dumps(fixture.config))
    module.execute(["git", "add", "."], cwd=fixture.workspace)
    module.execute(["git", "commit", "-qm", "Policy"], cwd=fixture.workspace)
    token = "test-only-production-token-" + root.name
    token_file = root / "http-token"
    token_file.write_text(token)
    token_file.chmod(0o600)
    fixture.data.chmod(0o700)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    environment = {
        **os.environ,
        "FACTORY_UID": str(os.getuid()),
        "FACTORY_GID": str(os.getgid()),
        "FACTORY_DOCKER_GID": str(Path("/var/run/docker.sock").stat().st_gid),
        "FACTORY_DATA_DIR": str(fixture.data),
        "FACTORY_WORKSPACE": str(fixture.workspace),
        "FACTORY_TOKEN_FILE": str(token_file),
        "FACTORY_PORT": str(port),
        "FACTORY_CONTROLLER_IMAGE": args.image,
    }
    project = root.name.replace("_", "-")
    evidence = {"version": 1, "kind": "real_compose_docker_simulated_provider", "passed": False}

    def compose(*argv, check=True):
        return module.execute(
            ["docker", "compose", "-f", str(ROOT / "compose.yaml"), "-p", project, *argv],
            env=environment,
            check=check,
        )

    def api(operation, **payload):
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/control/{operation}",
            data=json.dumps(payload).encode(),
            headers={"authorization": f"Bearer {token}", "content-type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=90) as response:
            return json.load(response)

    def boot():
        compose("up", "-d", "--no-build", "--wait", "--wait-timeout", "60")
        assert api("plan_list") == []

    def wait(operation, expected, **payload):
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            value = api(operation, **payload)
            if value["status"] == expected:
                return value
            assert value["status"] not in {"failed", "blocked", "recovery_needed"}, value
            time.sleep(0.2)
        raise AssertionError(f"did not reach {expected}")

    def snapshot(action):
        module.execute(
            [
                sys.executable,
                str(ROOT / "scripts/factory-snapshot.py"),
                action,
                "--data",
                str(fixture.data),
                "--workspace",
                str(fixture.workspace),
                "--snapshot",
                str(root / "snapshot"),
            ]
        )

    def event_id(run_id, cursor):
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/runs/{run_id}/events",
            headers={"authorization": f"Bearer {token}", "last-event-id": str(cursor)},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            for line in response:
                if line.startswith(b"id:"):
                    return int(line.split(b":", 1)[1])
        raise AssertionError("no saved SSE event")

    def wait_for_builder(run_id):
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            for receipt in fixture.receipts():
                if (
                    receipt["request"]["run_id"] == run_id
                    and receipt["request"]["execution"].get("step") == "build"
                    and receipt["execution_id"]
                    and not receipt["stopped"]
                ):
                    return
            status = api("status", run_id=run_id)
            assert status["status"] not in {"failed", "blocked", "recovery_needed"}, status
            time.sleep(0.05)
        raise AssertionError("no in-flight builder turn")

    try:
        backend = json.loads((fixture.root / "docker.json").read_text())
        backend["seccomp_profile"] = "/opt/factory/worker-seccomp.json"
        backend_file = fixture.data / "docker-input.json"
        backend_file.write_text(json.dumps(backend))
        compose("run", "--rm", "--no-deps", "controller", "configure-docker", str(backend_file))
        boot()

        def catalog_request(_):
            try:
                api("models")
                return "ok"
            except urllib.error.HTTPError as error:
                assert error.code == 409
                return "busy"

        # Hold the external simulated provider so competing requests definitely
        # reach admission before the first slot can be released.
        gate = fixture.workspace / ".factory-test-catalog-gate"
        gate.write_text("hold")
        with ThreadPoolExecutor(4) as pool:
            first = pool.submit(catalog_request, 0)
            try:
                deadline = time.monotonic() + 15
                while not list(fixture.data.rglob("catalog-gate-ready")):
                    assert not first.done(), "catalog did not wait at the test gate"
                    assert time.monotonic() < deadline, "catalog gate did not become ready"
                    time.sleep(0.05)
                others = [pool.submit(catalog_request, i) for i in range(1, 4)]
                catalog_results = [future.result(timeout=10) for future in others]
            finally:
                gate.unlink()
            catalog_results.append(first.result(timeout=15))
        assert catalog_results.count("ok") == 1 and catalog_results.count("busy") == 3, (
            catalog_results
        )
        started = api("plan_start", request="Update greeting", request_id="production")
        session = started["session_id"]
        waiting = wait("plan_status", "waiting_for_input", session_id=session)
        compose("stop")
        compose("up", "-d", "--no-build", "--force-recreate", "--wait")
        assert api("plan_status", session_id=session)["pending"] == waiting["pending"]
        api(
            "plan_answer",
            session_id=session,
            interaction_id=waiting["pending"]["interaction_id"],
            answers={"recipient": "world"},
        )
        planned = wait("plan_status", "planned", session_id=session)
        compose("stop")
        snapshot("backup")
        # Restore the completed exact plan, still pending approval, into absent
        # directories at its original mount paths. Never regenerate its revision.
        fixture.data.rename(fixture.root / "pre-restore-data")
        fixture.workspace.rename(fixture.root / "pre-restore-workspace")
        snapshot("restore")
        compose("up", "-d", "--no-build", "--force-recreate", "--wait")
        restored = api("plan_status", session_id=session)
        assert restored["status"] == "planned"
        assert restored["revision"] == planned["revision"]
        assert restored["plan_hash"] == planned["plan_hash"]
        approved = api("plan_approve", session_id=session, plan_hash=planned["plan_hash"])
        run_id = approved["implementation"]["run_id"]
        assert event_id(run_id, 0) == 1  # Disconnect without cancelling progress.
        # Pin the crash to an actual provider turn, rather than racing the create
        # intent window (which legitimately requires operator reconciliation).
        wait_for_builder(run_id)
        compose("kill", "-s", "SIGKILL", "controller")
        compose("up", "-d", "--no-build", "--force-recreate", "--wait")
        result = wait("status", "ready_for_review", run_id=run_id)
        result = json.loads(
            compose(
                "exec",
                "-T",
                "controller",
                "factory-runtime",
                str(fixture.data),
                str(fixture.workspace),
                "status",
                run_id,
            ).stdout
        )
        assert "Hello, world!" in (fixture.workspace / "greeting.txt").read_text()
        assert "ready_for_review" in api("plan_report", session_id=session)["report"]
        operations = result["workflow"]["operations"]
        assert all(op["dispatch_count"] == 1 for op in operations.values())
        assert event_id(run_id, 1) == 2  # Reconnect replays the saved next event.
        # The prior manual run deliberately leaves its verified edit for review.
        # Accept it in this disposable repository before starting independent work.
        module.execute(["git", "add", "greeting.txt"], cwd=fixture.workspace)
        module.execute(
            ["git", "commit", "-qm", "Accept verified fixture output"], cwd=fixture.workspace
        )
        cancel_run = api(
            "start", request="Update greeting [cancel]", request_id="production-cancel"
        )
        cancel_id = cancel_run["run_id"]
        wait_for_builder(cancel_id)
        assert api("cancel", run_id=cancel_id)["status"] == "stopping"
        assert wait("status", "cancelled", run_id=cancel_id)["status"] == "cancelled"
        inspections = []
        for receipt in fixture.receipts():
            if (
                not receipt["container_id"]
                or receipt["request"]["execution"].get("step") == "catalog"
            ):
                continue
            worker = json.loads(
                module.execute(["docker", "inspect", receipt["container_id"]]).stdout
            )[0]
            host = worker["HostConfig"]
            assert worker["Config"]["User"] == f"{os.getuid()}:{os.getgid()}"
            assert host["ReadonlyRootfs"] and "ALL" in host["CapDrop"]
            assert host["Memory"] > 0 and host["PidsLimit"] > 0
            assert not worker["State"]["Running"] and worker["State"]["Pid"] == 0
            for mount in worker["Mounts"]:
                assert mount["Destination"] != "/var/run/docker.sock"
                assert mount["Source"] != str(fixture.data)
                if mount["Destination"] == receipt["request"]["evidence_directory"]:
                    assert not mount["RW"]
            inspections.append(
                {"id": receipt["container_id"], "read_only_root": True, "stopped": True}
            )
        logs = compose("logs", "--no-color").stdout
        assert token not in logs
        controller_id = compose("ps", "-q", "controller").stdout.strip()
        controller = json.loads(module.execute(["docker", "inspect", controller_id]).stdout)[0]
        assert controller["State"]["Health"]["Status"] == "healthy"
        assert controller["HostConfig"]["PortBindings"]["4317/tcp"][0]["HostIp"] == "127.0.0.1"
        evidence.update(
            {
                "passed": True,
                "planning_session": session,
                "run_id": run_id,
                "image_id": controller["Image"],
                "status": result["status"],
                "operations": len(operations),
                "workers": inspections,
                "restore_pending_approval": True,
                "restored_plan_hash": restored["plan_hash"],
                "catalog_admission": catalog_results,
                "sse_replay": True,
                "cancelled_run": cancel_id,
                "hard_recreation": True,
                "root": str(root),
                "source_base": module.execute(
                    ["git", "rev-parse", "HEAD"], cwd=ROOT
                ).stdout.strip(),
                "source_hashes": {
                    str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in [
                        ROOT / "compose.yaml",
                        ROOT / "containers/rust-runtime.Dockerfile",
                        ROOT / "containers/controller-entrypoint.sh",
                        ROOT / "containers/worker.Dockerfile",
                        ROOT / "scripts/factory-snapshot.py",
                        ROOT / "scripts/accept-rust-production.py",
                        ROOT / "tests/fixtures/fake_app_server.py",
                        *sorted((ROOT / "rust/factory-runtime/src").rglob("*.rs")),
                    ]
                },
                "docker_version": module.execute(
                    ["docker", "version", "--format", "{{.Server.Version}}"]
                ).stdout.strip(),
                "live_provider": "not exercised",
                "host_reboot": "not exercised",
            }
        )
        print(json.dumps({"passed": True, "output": str(args.output)}))
    finally:
        compose("down", "--remove-orphans", check=False)
        fixture.cleanup_containers()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(evidence, indent=2) + "\n")


if __name__ == "__main__":
    main()
