#!/usr/bin/env python3
"""Small Docker control adapter for broker quality/recovery regressions."""

import json
import os
import sys
import time
from pathlib import Path

root = Path(__file__).parent
path = root / "container.json"
args = sys.argv[1:]
command = args[1]
if command == "create":
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
