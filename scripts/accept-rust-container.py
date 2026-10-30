"""Destructive only to uniquely named, test-owned containers and volumes."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default="factory-rust-slice:dev")
    parser.add_argument("--output", type=Path, default=Path(".checks/rust-container.json"))
    args = parser.parse_args()
    name = "factory-slice-" + uuid.uuid4().hex[:12]
    volumes = [name + "-data", name + "-workspace"]
    mounts = ["-v", volumes[0] + ":/data", "-v", volumes[1] + ":/workspace"]
    evidence: dict[str, Any] = {"schema_version": 1, "image": args.image, "cases": []}

    def docker(*argv: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["docker", *argv], capture_output=True, text=True, check=check, timeout=60
        )

    def launch() -> None:
        docker("run", "-d", "--name", name, *mounts, args.image)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if (
                docker(
                    "exec", name, "factory-runtime", "/data", "/workspace", "list", check=False
                ).returncode
                == 0
            ):
                return
            time.sleep(0.1)
        raise AssertionError("service did not become ready")

    def stop() -> None:
        docker("kill", "--signal=KILL", name)
        docker("rm", name)

    def client(*argv: str, check: bool = True) -> dict[str, Any]:
        result = docker("exec", name, "factory-runtime", "/data", "/workspace", *argv, check=check)
        if not check:
            assert result.returncode != 0, result.stdout
            return {}
        return json.loads(result.stdout)

    def once(*argv: str, crash: str | None = None) -> dict[str, Any]:
        environment = ["-e", "FACTORY_SLICE_CRASH_AT=" + crash] if crash else []
        result = docker("run", "--rm", *mounts, *environment, args.image, *argv, check=not crash)
        if crash:
            assert result.returncode == 86, (crash, result.stdout, result.stderr)
            return {}
        return json.loads(result.stdout)

    def request(key: str) -> str:
        return json.dumps(
            {
                "request_id": key,
                "session_id": "acceptance",
                "request": "Build a greeting",
                "timeout_seconds": 120,
                "max_dispatches": 3,
            }
        )

    def wait_for(run_id: str, status: str) -> dict[str, Any]:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            run = client("status", run_id)
            if run["status"] == status:
                return run
            time.sleep(0.1)
        raise AssertionError((status, run))

    try:
        for volume in volumes:
            docker("volume", "create", volume)
        launch()
        run = client("start", request("waiting-restart"))
        run_id = run["run_id"]
        waiting = wait_for(run_id, "waiting_for_input")
        stop()
        launch()
        restored = wait_for(run_id, "waiting_for_input")
        assert restored["interaction"] == waiting["interaction"]
        assert restored["deadline_at"] == waiting["deadline_at"]
        assert restored["checkpoint_thread_id"] == waiting["checkpoint_thread_id"]
        assert client("start", request("waiting-restart")) == restored
        conflict = json.loads(request("waiting-restart"))
        conflict["request"] = "different input"
        client("start", json.dumps(conflict), check=False)
        question = restored["interaction"]["interaction_id"]
        client("answer", run_id, question, "true")
        finished = wait_for(run_id, "ready_for_review")
        assert client("answer", run_id, question, "true") == finished
        client("answer", run_id, question, "false", check=False)
        with ThreadPoolExecutor(max_workers=2) as pool:
            resumes = list(pool.map(lambda _: client("resume", run_id), range(2)))
        assert resumes == [finished, finished]
        evidence["cases"].append(
            {
                "case": "kill_recreate_waiting_duplicates_terminal_guard",
                "passed": True,
                "run": finished,
            }
        )
        stop()
        run = once("start", request("concurrent-resumes"))
        waiting = once("resume", run["run_id"])
        once("answer", run["run_id"], waiting["interaction"]["interaction_id"], "true")
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _: once("resume", run["run_id"]), range(2)))
        finished = once("resume", run["run_id"])
        assert finished["status"] == "ready_for_review"
        assert finished["operation"]["dispatch_count"] == 1
        evidence["cases"].append(
            {"case": "concurrent_workspace_resumes", "passed": True, "run": finished}
        )
        for point in ["before_dispatch", "after_dispatch", "after_result", "after_queue_finish"]:
            run = once("start", request(point))
            waiting = once("resume", run["run_id"])
            question = waiting["interaction"]["interaction_id"]
            once("answer", run["run_id"], question, "true")
            once("resume", run["run_id"], crash=point)
            recovery_deadline = time.monotonic() + 20
            while True:
                finished = once("resume", run["run_id"])
                if finished["status"] == "ready_for_review":
                    break
                assert finished["status"] == "running" and time.monotonic() < recovery_deadline, (
                    finished
                )
                time.sleep(0.2)
            assert finished["operation"]["dispatch_count"] == (
                2 if point == "before_dispatch" else 1
            )
            assert once("resume", run["run_id"]) == finished
            evidence["cases"].append({"case": point, "passed": True, "run": finished})
        evidence["image_id"] = docker(
            "image", "inspect", args.image, "--format", "{{.Id}}"
        ).stdout.strip()
        evidence["docker_version"] = docker("version", "--format", "{{json .}}").stdout.strip()
        evidence["passed"] = True
    except BaseException as error:
        evidence["passed"] = False
        evidence["error"] = repr(error)
        logs = docker("logs", name, check=False)
        evidence["service_logs"] = logs.stdout + logs.stderr
        raise
    finally:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(evidence, indent=2) + "\n")
        docker("rm", "-f", name, check=False)
        for volume in volumes:
            docker("volume", "rm", volume, check=False)


if __name__ == "__main__":
    main()
