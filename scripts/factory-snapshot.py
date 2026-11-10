#!/usr/bin/env python3
"""Offline, whole-deployment snapshot. Stop clients/controller before invoking.

Copies every SQLite WAL/SHM alongside databases, artifacts, receipts, provider
homes, claims and checkout Git data. Never restores over a populated directory.
Snapshots are private trusted operator artifacts, not an interchange format.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory


def inventory(root: Path) -> dict[str, dict[str, str | int]]:
    entries = {}
    for path in sorted(root.rglob("*")):
        mode = path.lstat().st_mode
        value: dict[str, str | int] = {"mode": stat.S_IMODE(mode)}
        if stat.S_ISLNK(mode):
            value["link"] = os.readlink(path)
        elif stat.S_ISREG(mode):
            digest = hashlib.sha256()
            with path.open("rb") as source:
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(block)
            value["sha256"] = digest.hexdigest()
        elif not stat.S_ISDIR(mode):
            raise ValueError("snapshot cannot contain sockets/devices/FIFOs")
        entries[str(path.relative_to(root))] = value
    return entries


def validate_databases(root: Path) -> None:
    for path in root.rglob("*.sqlite"):
        # SQLite can update SHM even through a read-only connection. Validate a
        # disposable copy so neither checking nor restoring mutates the snapshot.
        with TemporaryDirectory(prefix="factory-integrity-") as temporary:
            target = Path(temporary) / path.name
            for suffix in ["", "-wal", "-shm"]:
                source = Path(str(path) + suffix)
                if source.exists():
                    shutil.copy2(source, Path(str(target) + suffix))
            with sqlite3.connect(target.as_uri() + "?mode=ro", uri=True) as connection:
                if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise ValueError("SQLite integrity check failed")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["backup", "restore"])
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--docker", default="docker")
    args = parser.parse_args()
    os.umask(0o077)
    paths = [args.data, args.workspace, args.snapshot]
    if any(not path.is_absolute() or path.is_symlink() for path in paths):
        parser.error("use absolute, non-symlink directory paths")
    data, workspace, snapshot = (path.resolve() for path in paths)
    if any(
        a == b or a in b.parents or b in a.parents
        for i, a in enumerate([data, workspace, snapshot])
        for b in [data, workspace, snapshot][i + 1 :]
    ):
        parser.error("data, workspace and snapshot must be separate directories")
    # The stopped controller cannot race a new worker create. Refuse backup or
    # restore if an exact owner still has any running worker (including paused).
    owner = hashlib.sha256(os.fsencode(data)).hexdigest()
    containers = subprocess.run(
        [args.docker, "ps", "-q", "--filter", f"label=software-factory.controller={owner}"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if containers.stdout.strip():
        parser.error("surviving workers must be reconciled/stopped before snapshot maintenance")
    with ExitStack() as stack:
        if data.exists():
            lock = stack.enter_context((data / "control-service.lock").open("a+b"))
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if workspace.exists():
            lock = stack.enter_context((workspace / ".factory-rust.service.lock").open("a+b"))
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            lock = stack.enter_context((workspace / ".factory-rust.lock").open("a+b"))
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.action == "backup":
            if not data.is_dir() or not workspace.is_dir():
                parser.error("backup requires existing data and workspace directories")
            if (
                (workspace / ".git").is_file()
                or (workspace / ".git").is_symlink()
                or any((workspace / ".git/worktrees").glob("*"))
            ):
                parser.error(
                    "use a dedicated checkout; archive external linked worktrees separately"
                )
            snapshot.mkdir(mode=0o700)
            # A partial copy has no manifest and therefore cannot be restored.
            for name, source in [("data", data), ("workspace", workspace)]:
                shutil.copytree(source, snapshot / name, symlinks=True)
            validate_databases(snapshot / "data")
            manifest = {
                "version": 1,
                "data": str(data),
                "workspace": str(workspace),
                "entries": {name: inventory(snapshot / name) for name in ["data", "workspace"]},
            }
            (snapshot / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        else:
            # No files, including locks, may be replaced under another process.
            if data.exists() or workspace.exists():
                parser.error("restore requires absent data and workspace directories")
            manifest = json.loads((snapshot / "manifest.json").read_text())
            if (manifest["version"], manifest["data"], manifest["workspace"]) != (
                1,
                str(data),
                str(workspace),
            ):
                parser.error("restore requires version 1 and the original absolute mount paths")
            for name in ["data", "workspace"]:
                if inventory(snapshot / name) != manifest["entries"][name]:
                    parser.error("snapshot content/mode hash mismatch")
            validate_databases(snapshot / "data")
            for name, destination in [("data", data), ("workspace", workspace)]:
                shutil.copytree(snapshot / name, destination, symlinks=True)
        print(json.dumps({"action": args.action, "complete": True, "version": 1}))


if __name__ == "__main__":
    main()
