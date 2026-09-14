"""Commit the verified bytes without running hooks, filters, or changing files."""

from __future__ import annotations

import hashlib
import os
from dataclasses import asdict
from typing import Any

from software_factory.artifacts import ArtifactIntegrityError
from software_factory.delivery_commands import DeliveryCommands
from software_factory.git import WorkspaceSnapshot, capture_snapshot
from software_factory.runtime import FactoryRuntime


class VerifiedCommit:
    def __init__(self, runtime: FactoryRuntime, commands: DeliveryCommands) -> None:
        self.runtime, self.commands = runtime, commands

    async def prepare(self, state: dict[str, Any], title: str, branch: str) -> dict[str, Any]:
        runtime, cmd = self.runtime, self.commands
        path = "delivery/commit.json"
        if runtime.artifacts.path(path).exists():
            return runtime.evidence.read(path)  # type: ignore[no-any-return]
        snapshot = runtime.evidence.read(state["handoff"]["bundle"]["repository_content"])
        await runtime.evidence.require_current(snapshot)
        index = runtime.artifacts.root / "delivery-index"
        # The temporary index contains only the verified tree. The user's index is untouched.
        try:
            if index.exists():
                index.unlink()
            await cmd.git("read-tree", snapshot["head"], index=index)
            for path in state["handoff"]["bundle"]["changed_files"]:
                entry = snapshot["files"][path]
                if entry["kind"] == "missing":
                    await cmd.git("update-index", "--force-remove", "--", path, index=index)
                    continue
                if entry["kind"] == "symlink":
                    raw, mode = os.fsencode(entry["link_target"]), "120000"
                elif entry["kind"] == "file":
                    raw = (runtime.workspace / path).read_bytes()
                    mode = "100755" if entry["mode"] & 0o111 else "100644"
                else:
                    raise ValueError("delivery does not support submodules or directory entries")
                blob = (
                    (await cmd.git("hash-object", "-w", "--no-filters", "--stdin", data=raw))
                    .decode()
                    .strip()
                )
                await cmd.git("update-index", "--add", "--cacheinfo", mode, blob, path, index=index)
            tree = (await cmd.git("write-tree", index=index)).decode().strip()
            committed_index = await cmd.git("ls-files", "--stage", "-v", "-z", index=index)
            committed_index += b"\0" + await cmd.git("ls-files", "--stage", "-f", "-z", index=index)
        finally:
            index.unlink(missing_ok=True)
        await self.verify_tree(tree, snapshot)
        author = (await cmd.git("var", "GIT_AUTHOR_IDENT")).decode().strip()
        committer = (await cmd.git("var", "GIT_COMMITTER_IDENT")).decode().strip()
        content = f"tree {tree}\nparent {snapshot['head']}\nauthor {author}\ncommitter {committer}\n\n{title}\n\nFactory delivery: {runtime.run_id}\n"
        commit = (
            (await cmd.git("hash-object", "-t", "commit", "-w", "--stdin", data=content.encode()))
            .decode()
            .strip()
        )
        await runtime.evidence.require_current(snapshot)
        record = {
            "commit": commit,
            "tree": tree,
            "branch": branch,
            "snapshot": snapshot,
            "committed_index": committed_index.hex(),
        }
        runtime.artifacts.write_json("delivery/commit.json", record)
        return record

    async def verify_tree(self, tree: str, snapshot: dict[str, Any]) -> None:
        entries = (await self.commands.git("ls-tree", "-r", "-z", tree)).split(b"\0")
        actual = {}
        for entry in filter(None, entries):
            metadata, name = entry.split(b"\t", 1)
            mode, kind, object_id = metadata.split()
            if kind != b"blob":
                raise ValueError("delivery does not support submodules")
            raw = await self.commands.git("cat-file", "blob", object_id.decode())
            actual[os.fsdecode(name)] = (mode.decode(), hashlib.sha256(raw).hexdigest())
        expected = {}
        for path, item in snapshot["files"].items():
            if item["kind"] == "file":
                expected[path] = ("100755" if item["mode"] & 0o111 else "100644", item["digest"])
            elif item["kind"] == "symlink":
                expected[path] = (
                    "120000",
                    hashlib.sha256(os.fsencode(item["link_target"])).hexdigest(),
                )
            elif item["kind"] != "missing":
                raise ValueError("delivery does not support directory entries")
        if actual != expected:
            raise ArtifactIntegrityError("commit tree differs from tested bytes; delivery stopped")

    async def _current(self, record: dict[str, Any]) -> WorkspaceSnapshot:
        runtime = self.runtime
        runtime.artifacts.verify()
        runtime.evidence.verify_instructions()
        current = await capture_snapshot(runtime.workspace)
        files = {path: asdict(entry) for path, entry in current.worktree.items()}
        saved_files = {
            p: e for p, e in record["snapshot"]["files"].items() if e["kind"] != "missing"
        }
        current_files = {p: e for p, e in files.items() if e["kind"] != "missing"}
        if current_files != saved_files:
            raise ArtifactIntegrityError("stale evidence: files changed before delivery")
        return current

    async def require_installed(self, record: dict[str, Any]) -> None:
        current = await self._current(record)
        ref = f"refs/heads/{record['branch']}"
        refs = {**record["snapshot"]["refs"], ref: record["commit"]}
        if (current.head, current.head_ref, current.refs, current.index.hex()) != (
            record["commit"],
            ref,
            refs,
            record["committed_index"],
        ):
            raise ArtifactIntegrityError(
                "repository changed after the verified commit was installed"
            )

    async def install(self, record: dict[str, Any]) -> None:
        runtime, cmd = self.runtime, self.commands
        expected = record["snapshot"]
        commit, ref = record["commit"], f"refs/heads/{record['branch']}"
        current = await self._current(record)
        refs = dict(current.refs)
        own_ref = refs.pop(ref, None)
        original_refs = dict(expected["refs"])
        original_own = original_refs.pop(ref, None)
        if refs != original_refs or own_ref not in {original_own, commit}:
            raise ArtifactIntegrityError("repository references changed before delivery")
        if (current.head, current.head_ref) not in {
            (expected["head"], expected["head_ref"]),
            (commit, ref),
        }:
            raise ArtifactIntegrityError("checked-out branch changed before delivery")
        # Compare both complete index representations, including flags.
        committed_index = bytes.fromhex(record["committed_index"])
        if current.index not in {bytes.fromhex(expected["index"]), committed_index}:
            raise ArtifactIntegrityError("index changed before delivery; staged work preserved")
        await self.verify_tree(record["tree"], expected)
        await runtime.check_cancelled()
        if own_ref != commit:
            await cmd.git("update-ref", ref, commit, original_own or "")
        if current.head_ref != ref:
            await cmd.git("symbolic-ref", "HEAD", ref)
        if current.index != committed_index:
            await cmd.git("read-tree", commit)
        if await cmd.git("status", "--porcelain"):
            raise ArtifactIntegrityError("workspace changed during commit; delivery stopped")
