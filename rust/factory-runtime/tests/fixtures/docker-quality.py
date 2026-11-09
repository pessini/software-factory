#!/usr/bin/env python3
"""Small Docker control adapter for broker quality/recovery regressions."""

import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

root = Path(__file__).parent
path = root / "container.json"
args = sys.argv[1:]
command = args[1]
if command == "create":
    if (root / "mode").read_text() == "run-expired":
        # Deliberately exceed the old one-second setup budget. The expiry test
        # must target a running worker, even on a slow CI host.
        time.sleep(1.5)
    labels = dict(args[i + 1].split("=", 1) for i, arg in enumerate(args) if arg == "--label")
    container = {
        "Id": "a" * 64,
        "Image": "sha256:fixture",
        "Config": {"Labels": labels},
        "State": {"Running": False, "Pid": 0, "Status": "created", "ExitCode": 0},
    }
    path.write_text(json.dumps(container))
    print(container["Id"])
elif command == "ls":
    if path.exists():
        print("a" * 64)
elif command == "inspect":
    print("[" + path.read_text() + "]")
elif command == "start":
    container = json.loads(path.read_text())
    container["State"].update(Running=True, Pid=123, Status="running")
    path.write_text(json.dumps(container))
    mode = (root / "mode").read_text()
    if mode == "run-expired":
        # Expire the saved run only after the exact worker identity is running.
        # This exercises deadline cleanup rather than uncertain creation.
        with sqlite3.connect(root / "factory-rust.sqlite") as connection:
            run_id, raw = connection.execute(
                "SELECT run_id, document FROM factory_slice_runs"
            ).fetchone()
            document = json.loads(raw)
            document["deadline_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
            connection.execute(
                "UPDATE factory_slice_runs SET document = ? WHERE run_id = ?",
                (json.dumps(document), run_id),
            )
    if mode != "output":
        print("before timeout", flush=True)
        time.sleep(30)
    else:
        for _ in range(256):
            os.write(1, b"x" * 8192)
        os.write(1, "last: é☃".encode())
    container["State"].update(Running=False, Pid=0, Status="exited")
    path.write_text(json.dumps(container))
elif command == "kill":
    if (root / "mode").read_text() != "cleanup-fails":
        container = json.loads(path.read_text())
        container["State"].update(Running=False, Pid=0, Status="exited", ExitCode=137)
        path.write_text(json.dumps(container))
elif command == "logs":
    if (root / "mode").read_text() == "output":
        for _ in range(256):
            os.write(1, b"x" * 8192)
        os.write(1, "last: é☃".encode())
    else:
        print("before timeout")
else:
    raise AssertionError(args)
