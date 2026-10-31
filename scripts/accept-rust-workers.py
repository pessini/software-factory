#!/usr/bin/env python3
"""Process/volume acceptance for the Rust controller and native Docker broker.

The default image is an explicit protocol simulation, not live provider evidence.
Creates only uniquely named fixture directories and removes only their containers.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import time
from contextlib import suppress
from pathlib import Path
from tempfile import mkdtemp
from typing import Any

REPOSITORY = Path(__file__).resolve().parents[1]


def execute(
    argv: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv, cwd=cwd, env=env, text=True, capture_output=True, timeout=900, check=check
    )


class Fixture:
    def __init__(self, root: Path, name: str, image: str, live: bool = False) -> None:
        self.root = root / name
        self.workspace = self.root / "workspace"
        self.data = self.root / "data"
        self.workspace.mkdir(parents=True)
        self.data.mkdir()
        for argv in (
            ["init", "-q"],
            ["config", "user.name", "Factory acceptance"],
            ["config", "user.email", "factory@example.invalid"],
        ):
            execute(["git", *argv], cwd=self.workspace)
        (self.workspace / "greeting.txt").write_text("Hello\n")
        execute(["git", "add", "greeting.txt"], cwd=self.workspace)
        execute(["git", "commit", "-qm", "Initial greeting"], cwd=self.workspace)
        self.executable = str(root / "factory-runtime")
        backend = {
            "executable": "docker",
            "image": image,
            "daemon_workspace": str(self.workspace),
            "daemon_data_dir": str(self.data),
            "workspace_volume": None,
            "data_volume": None,
            "daemon_auth_file": None,
            "uid": os.getuid(),
            "gid": os.getgid(),
            "seccomp_profile": str(REPOSITORY / "containers/worker-seccomp.json"),
            "approval_policy": "on-request" if live else "never",
        }
        if live:
            backend["daemon_auth_file"] = str(
                Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "auth.json"
            )
        path = self.root / "docker.json"
        path.write_text(json.dumps(backend))
        self.call("configure-docker", str(path))
        agent = {
            "provider": "codex",
            "model": "gpt-5.6-luna" if live else "factory-test-model",
            "reasoning_level": "low",
            "permission_mode": "auto",
        }
        self.config = {
            "version": 2,
            "workflow": "simple-sdlc",
            "review_mode": "single",
            "worker_connection": {
                "runtime": "codex",
                "model_connection": "native",
                "codex_version": "0.154.0",
            },
            "agents": {
                "planner": agent,
                "builder": {**agent, "permission_mode": "accept-edits"},
                "reviewer": agent,
            },
            "quality": {
                "commands": [
                    {
                        "name": "greeting",
                        "argv": [
                            "python3",
                            "-B",
                            "-c",
                            "assert 'BROKEN' not in open('greeting.txt').read()",
                        ],
                        "timeout_seconds": 30,
                        "repeat_on_recovery": True,
                    }
                ]
            },
            "limits": {
                "step_timeout_seconds": 120 if live else 30,
                "total_run_timeout_seconds": 600 if live else 60,
            },
        }

    def argv(self, *args: str) -> list[str]:
        return [self.executable, str(self.data), str(self.workspace), *args]

    def call(self, *args: str) -> dict[str, Any]:
        result = execute(self.argv(*args))
        return json.loads(result.stdout)

    def start(self, request: str = "Update greeting", timeout: int = 60) -> dict[str, Any]:
        return self.call(
            "start",
            json.dumps(
                {
                    "request_id": "request",
                    "session_id": "session",
                    "request": request,
                    "timeout_seconds": timeout,
                    "max_dispatches": 3,
                    "implementation": {
                        "config": self.config,
                        "required_criteria": [],
                        "approved_plan": None,
                    },
                }
            ),
        )

    def receipts(self) -> list[dict[str, Any]]:
        return [json.loads(p.read_text()) for p in (self.data / "worker-records").glob("*.json")]

    def until_terminal(self, run_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            value = self.call("resume", run_id)
            if value["status"] in {"ready_for_review", "failed", "blocked", "cancelled"}:
                return value
            assert value["status"] != "recovery_needed", value.get("error")
            time.sleep(0.1)
        raise AssertionError("run did not finish within the acceptance bound")

    def wait_for_build(self, child: subprocess.Popen[str]) -> dict[str, Any]:
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            for receipt in self.receipts():
                kind = receipt["request"]["execution"]
                if (
                    kind["kind"] == "specialist"
                    and kind["step"] == "build"
                    and receipt["execution_id"]
                ):
                    return receipt
            assert child.poll() is None, child.communicate()
            time.sleep(0.05)
        raise AssertionError("coding turn did not start")

    def evidence(self, result: dict[str, Any]) -> dict[str, Any]:
        receipts = self.receipts()
        workers = [
            r
            for r in receipts
            if r["request"]["execution"]["kind"] == "specialist"
            and r["request"]["execution"]["step"] != "catalog"
        ]
        for receipt in receipts:
            if receipt["container_id"] and receipt["request"]["execution"].get("step") != "catalog":
                values = json.loads(execute(["docker", "inspect", receipt["container_id"]]).stdout)
                assert not values[0]["State"]["Running"] and values[0]["State"]["Pid"] == 0
        return {
            "status": result["status"],
            "run_id": result["run_id"],
            "root": str(self.root),
            "operations": result["workflow"]["operations"],
            "workers": [
                {
                    k: r[k]
                    for k in (
                        "operation_id",
                        "container_id",
                        "provider_thread_id",
                        "execution_id",
                        "stopped",
                        "error",
                        "image_id",
                        "reported",
                    )
                }
                for r in workers
            ],
        }

    def cancel_publication(self, run_id: str) -> dict[str, Any]:
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        gh = bin_dir / "gh"
        shutil.copy2(REPOSITORY / "tests/fixtures/fake_github.py", gh)
        gh.chmod(0o755)
        control = self.root / "publication-processes.json"
        base = execute(["git", "rev-parse", "HEAD"], cwd=self.workspace).stdout.strip()
        environment = {
            **os.environ,
            "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
            "FACTORY_GITHUB_CONTROL": str(control),
            "FACTORY_GITHUB_WORKSPACE": str(self.workspace),
            "FACTORY_GITHUB_BASE": base,
        }
        policy = self.root / "delivery.json"
        policy.write_text(
            json.dumps(
                {"mode": "pull_request", "repository": "test/fixture", "title": "Update greeting"}
            )
        )
        active = subprocess.Popen(
            self.argv("deliver", run_id, str(policy)),
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        processes = {}
        try:
            bound = time.monotonic() + 20
            while not control.exists():
                assert active.poll() is None, active.communicate()
                assert time.monotonic() < bound, "controlled PR creation did not start"
                time.sleep(0.05)
            processes = json.loads(control.read_text())
            self.call("cancel-delivery", run_id)
            output, error = active.communicate(timeout=5)
            assert active.returncode == 0, error
            result = json.loads(output)
            assert result["workflow"]["delivery"]["status"] == "recovery_needed"
            bound = time.monotonic() + 5
            while any(not self.process_stopped(pid) for pid in processes.values()):
                assert time.monotonic() < bound, (
                    "publication command descendant survived cancellation"
                )
                time.sleep(0.05)
            assert (self.workspace / ".factory-rust.claim").exists(), (
                "uncertain publication released ownership"
            )
            return result
        finally:
            if active.poll() is None:
                active.kill()
                active.communicate(timeout=5)
            for pid in processes.values():
                if not self.process_stopped(pid):
                    with suppress(ProcessLookupError):
                        os.kill(pid, signal.SIGKILL)

    @staticmethod
    def process_stopped(pid: int) -> bool:
        path = Path(f"/proc/{pid}/stat")
        return not path.exists() or path.read_text().split(") ", 1)[1].split()[0] == "Z"

    def cleanup_containers(self) -> None:
        for receipt in self.receipts():
            if receipt["container_id"]:
                # IDs came from this fixture's private receipts. Never remove a
                # foreign name, running worker, volume, image, or user directory.
                execute(["docker", "rm", receipt["container_id"]], check=False)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--output", type=Path, default=None)
    options = parser.parse_args()
    options.image = options.image or (
        "software-factory-worker:0.154.0" if options.live else "factory-test-worker:dev"
    )
    options.output = options.output or REPOSITORY / ".checks" / (
        "issue-17-live-recovery.json" if options.live else "issue-17-docker.json"
    )
    checks = REPOSITORY / ".checks"
    checks.mkdir(exist_ok=True)
    root = Path(mkdtemp(prefix="issue-17-workers-", dir=checks))
    shutil.copy2(REPOSITORY / "target/debug/factory-runtime", root / "factory-runtime")
    source_paths = sorted(
        [
            *REPOSITORY.glob("rust/factory-runtime/src/**/*.rs"),
            REPOSITORY / "Cargo.lock",
            REPOSITORY / "rust/factory-runtime/Cargo.toml",
            Path(__file__).resolve(),
            REPOSITORY / "tests/fixtures/fake_app_server.py",
            REPOSITORY / "tests/fixtures/fake_github.py",
            REPOSITORY / "containers/worker-seccomp.json",
        ]
    )
    source_hashes = {
        str(p.relative_to(REPOSITORY)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in source_paths
    }
    binary_sha256 = hashlib.sha256((root / "factory-runtime").read_bytes()).hexdigest()
    scenarios: dict[str, Any] = {}
    fixtures: list[Fixture] = []
    try:
        for scenario in (
            ("crash_in_flight", "cancel")
            if options.live
            else (
                "pass",
                "repair",
                "waiting",
                "native_waiting",
                "crash_in_flight",
                "before_dispatch",
                "after_dispatch",
                "after_result",
                "after_queue_finish",
                "tamper",
                "delivery_cancel",
                "cancel",
                "timeout",
            )
        ):
            fixture = Fixture(root, scenario, options.image, options.live)
            fixtures.append(fixture)
            run = fixture.start(
                "Update greeting [repair]"
                if scenario == "repair"
                else "Update greeting [questions]"
                if scenario == "waiting"
                else "Update greeting [native-input]"
                if scenario == "native_waiting"
                else "Update greeting [tamper]"
                if scenario == "tamper"
                else "Change greeting.txt to contain exactly Hello, world! followed by one newline. The only required deliverable is that file. No publication.",
                timeout=600 if options.live else 1 if scenario == "timeout" else 60,
            )
            run_id = run["run_id"]
            if scenario in {
                "before_dispatch",
                "after_dispatch",
                "after_result",
                "after_queue_finish",
            }:
                environment = {**os.environ, "FACTORY_SLICE_CRASH_AT": scenario}
                if scenario != "after_queue_finish":
                    environment["FACTORY_CRASH_OPERATION_CONTAINS"] = "build-1"
                crashed = execute(fixture.argv("resume", run_id), env=environment, check=False)
                assert crashed.returncode == 86, (crashed.returncode, crashed.stderr)
                result = fixture.until_terminal(run_id)
                assert result["status"] == "ready_for_review", result.get("error")
                builders = [
                    r
                    for r in fixture.receipts()
                    if r["request"]["execution"].get("step") == "build"
                ]
                assert len(builders) == 1 and builders[0]["execution_id"]
            elif scenario in {"crash_in_flight", "cancel"}:
                child = subprocess.Popen(
                    fixture.argv("resume", run_id),
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                receipt = fixture.wait_for_build(child)
                if scenario == "crash_in_flight":
                    child.kill()
                    child.communicate(timeout=10)
                    result = fixture.until_terminal(run_id)
                    assert result["status"] == "ready_for_review", result.get("error")
                    recovered = next(
                        r
                        for r in fixture.receipts()
                        if r["operation_id"] == receipt["operation_id"]
                    )
                    assert recovered["execution_id"] == receipt["execution_id"]
                    assert (
                        result["workflow"]["operations"][receipt["operation_id"]]["dispatch_count"]
                        == 1
                    )
                else:
                    fixture.call("cancel", run_id)
                    child.communicate(timeout=45)
                    result = fixture.call("resume", run_id)
                    assert result["status"] == "cancelled", result.get("error")
            elif scenario in {"waiting", "native_waiting"}:
                waiting = fixture.call("resume", run_id)
                assert waiting["status"] == "waiting_for_input", waiting.get("error")
                question = waiting["workflow"]["interaction"]
                assert fixture.call("resume", run_id)["workflow"]["interaction"] == question
                answers = json.dumps({"recipient": "world"})
                fixture.call("answer-questions", run_id, question["interaction_id"], answers)
                fixture.call("answer-questions", run_id, question["interaction_id"], answers)
                result = fixture.until_terminal(run_id)
                assert result["status"] == "ready_for_review", result.get("error")
            else:
                result = fixture.until_terminal(run_id)
                assert result["status"] == (
                    "failed" if scenario == "timeout" else "ready_for_review"
                ), result.get("error")
            if scenario == "delivery_cancel":
                result = fixture.cancel_publication(run_id)
            if scenario == "repair":
                assert result["workflow"]["test_repair_count"] == 1
                builders = [
                    r
                    for r in fixture.receipts()
                    if r["request"]["execution"].get("role") == "builder"
                ]
                assert (
                    len(builders) == 2
                    and builders[0]["provider_thread_id"] == builders[1]["provider_thread_id"]
                )
            scenarios[scenario] = fixture.evidence(result)
            print(json.dumps({"scenario": scenario, "status": result["status"]}), flush=True)
        record = {
            "kind": "live_native_provider_with_real_docker_and_processes"
            if options.live
            else "simulated_native_provider_with_real_docker_and_processes",
            "image": options.image,
            "root": str(root),
            "binary_sha256": binary_sha256,
            "source_hashes": source_hashes,
            "scenarios": scenarios,
        }
        options.output.parent.mkdir(parents=True, exist_ok=True)
        options.output.write_text(json.dumps(record, indent=2) + "\n")
    finally:
        for fixture in fixtures:
            fixture.cleanup_containers()


if __name__ == "__main__":
    main()
