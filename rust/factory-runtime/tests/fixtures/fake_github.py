#!/usr/bin/env python3
"""A controlled gh executable for publication process-tree cancellation."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import unquote

control = Path(os.environ["FACTORY_GITHUB_CONTROL"])
workspace = Path(os.environ["FACTORY_GITHUB_WORKSPACE"])
path = next(arg for arg in sys.argv if arg.startswith("repos/"))
if "/git/matching-refs/heads/" in path:
    branch = unquote(path.split("/git/matching-refs/heads/", 1)[1])
    head = (
        os.environ["FACTORY_GITHUB_BASE"]
        if branch == "main"
        else subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=workspace, text=True).strip()
    )
    print(json.dumps([[{"ref": "refs/heads/" + branch, "object": {"sha": head}}]]))
elif "/pulls?" in path:
    print("[[]]")
elif path.endswith("/pulls") and "POST" in sys.argv:
    json.load(sys.stdin)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    control.write_text(json.dumps({"parent": os.getpid(), "child": child.pid}))
    time.sleep(600)
else:
    raise AssertionError(f"unexpected controlled GitHub request: {path}")
