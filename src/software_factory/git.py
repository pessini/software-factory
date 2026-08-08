from __future__ import annotations

import asyncio
import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class WorktreeEntry:
    kind: Literal["file", "symlink", "missing"]
    content: bytes | None = None
    object_id: str | None = None
    link_target: str | None = None
    mode: int | None = None


@dataclass(frozen=True)
class IndexEntry:
    mode: str
    object_id: str


@dataclass(frozen=True)
class WorkspaceSnapshot:
    worktree: dict[str, WorktreeEntry]
    index: dict[str, IndexEntry | None]
    head: str
    head_ref: str | None
    refs: dict[str, str]
    includes_ignored: bool = False


class GitError(RuntimeError):
    pass


async def git(workspace: Path, *args: str, check: bool = True) -> bytes:
    process = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=workspace,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if check and process.returncode:
        raise GitError(stderr.decode(errors="replace").strip() or "git command failed")
    return stdout


async def repository_root(workspace: Path) -> Path:
    output = await git(workspace, "rev-parse", "--show-toplevel")
    return Path(output.decode().strip()).resolve()


async def changed_files(workspace: Path) -> list[str]:
    tracked = await git(workspace, "diff", "--name-only", "-z", "HEAD")
    staged = await git(workspace, "diff", "--cached", "--name-only", "-z", "HEAD")
    untracked = await git(workspace, "ls-files", "--others", "--exclude-standard", "-z")
    names = {_decode_path(item) for item in tracked.split(b"\0") if item}
    names.update(_decode_path(item) for item in staged.split(b"\0") if item)
    names.update(_decode_path(item) for item in untracked.split(b"\0") if item)
    return sorted(path for path in names if not path.startswith(".factory/runs/"))


async def ignored_files(workspace: Path) -> list[str]:
    output = await git(
        workspace,
        "ls-files",
        "--others",
        "--ignored",
        "--exclude-standard",
        "-z",
    )
    return sorted(
        _decode_path(item)
        for item in output.split(b"\0")
        if item and not _decode_path(item).startswith(".factory/runs/")
    )


async def workspace_fingerprint(workspace: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for relative in await changed_files(workspace):
        path = workspace / relative
        if path.is_file():
            result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        else:
            result[relative] = "missing"
    return result


async def capture_snapshot(
    workspace: Path,
    *,
    include_ignored: bool = False,
    store_ignored: bool = True,
) -> WorkspaceSnapshot:
    worktree: dict[str, WorktreeEntry] = {}
    index: dict[str, IndexEntry | None] = {}
    changed = set(await changed_files(workspace))
    paths = set(changed)
    ignored = set(await ignored_files(workspace)) if include_ignored else set()
    paths.update(ignored)
    for relative in sorted(paths - ignored):
        worktree[relative] = _capture_worktree_entry(workspace, relative)
        index[relative] = await _index_entry(workspace, relative)
    ignored_entries = await _capture_ignored_entries(workspace, sorted(ignored), store_ignored)
    worktree.update(ignored_entries)
    index.update(dict.fromkeys(ignored_entries))
    head, head_ref, refs = await repository_metadata(workspace)
    return WorkspaceSnapshot(
        worktree=worktree,
        index=index,
        head=head,
        head_ref=head_ref,
        refs=refs,
        includes_ignored=include_ignored,
    )


async def restore_snapshot(workspace: Path, snapshot: WorkspaceSnapshot) -> None:
    await restore_repository_metadata(workspace, snapshot)
    current = set(await changed_files(workspace))
    if snapshot.includes_ignored:
        current.update(await ignored_files(workspace))
    original = set(snapshot.worktree)
    await restore_paths(workspace, sorted(current - original))
    for relative in sorted(original):
        await git(workspace, "update-index", "--force-remove", "--", relative)
        index_entry = snapshot.index[relative]
        if index_entry is not None:
            await git(
                workspace,
                "update-index",
                "--add",
                "--cacheinfo",
                index_entry.mode,
                index_entry.object_id,
                relative,
            )
        await _restore_snapshot_entry(workspace, relative, snapshot.worktree[relative])


async def repository_metadata(workspace: Path) -> tuple[str, str | None, dict[str, str]]:
    head_output = await git(workspace, "rev-parse", "--verify", "HEAD", check=False)
    head = head_output.decode("ascii", errors="replace").strip()
    ref_output = await git(workspace, "symbolic-ref", "-q", "HEAD", check=False)
    head_ref = ref_output.decode("utf-8", errors="surrogateescape").strip() or None
    refs_output = await git(
        workspace,
        "for-each-ref",
        "--format=%(refname)%00%(objectname)",
    )
    refs: dict[str, str] = {}
    for line in refs_output.splitlines():
        ref, separator, object_id = line.partition(b"\0")
        if separator:
            refs[_decode_path(ref)] = object_id.decode("ascii")
    return head, head_ref, refs


async def restore_repository_metadata(workspace: Path, snapshot: WorkspaceSnapshot) -> bool:
    current_head, current_head_ref, current_refs = await repository_metadata(workspace)
    if (
        current_head == snapshot.head
        and current_head_ref == snapshot.head_ref
        and current_refs == snapshot.refs
    ):
        return False
    for ref in sorted(set(current_refs) - set(snapshot.refs)):
        await git(workspace, "update-ref", "-d", ref)
    for ref, object_id in sorted(snapshot.refs.items()):
        if current_refs.get(ref) != object_id:
            await git(workspace, "update-ref", ref, object_id)
    if snapshot.head_ref is not None:
        await git(workspace, "symbolic-ref", "HEAD", snapshot.head_ref)
    elif snapshot.head:
        await git(workspace, "update-ref", "--no-deref", "HEAD", snapshot.head)
    if snapshot.head:
        await git(workspace, "reset", "--mixed", snapshot.head)
    return True


def capture_paths(workspace: Path, rules: list[str]) -> dict[str, WorktreeEntry]:
    snapshot: dict[str, WorktreeEntry] = {}
    for relative in _paths_under_rules(workspace, rules):
        snapshot[relative] = _capture_worktree_entry(workspace, relative)
    return snapshot


def restore_path_snapshot(
    workspace: Path, rules: list[str], snapshot: dict[str, WorktreeEntry]
) -> bool:
    current = set(_paths_under_rules(workspace, rules))
    original = set(snapshot)
    changed = current != original
    for relative in sorted(current - original, reverse=True):
        target = _workspace_path(workspace, relative)
        if target.is_file() or target.is_symlink():
            target.unlink()
    for relative, entry in sorted(snapshot.items()):
        if _capture_worktree_entry(workspace, relative) != entry:
            changed = True
            _restore_worktree_entry(workspace, relative, entry)
    return changed


def protected_changes(paths: list[str], protected: list[str]) -> list[str]:
    violations: list[str] = []
    for path in paths:
        for rule in protected:
            prefix = rule.rstrip("/")
            if path == prefix or path.startswith(f"{prefix}/"):
                violations.append(path)
                break
    return sorted(set(violations))


async def restore_paths(workspace: Path, paths: list[str]) -> None:
    for relative in paths:
        target = _workspace_path(workspace, relative)
        tracked = await git(workspace, "ls-files", "--error-unmatch", "--", relative, check=False)
        if tracked:
            await git(workspace, "restore", "--staged", "--worktree", "--", relative)
        elif target.is_file() or target.is_symlink():
            target.unlink()


def _decode_path(value: bytes) -> str:
    return value.decode("utf-8", errors="surrogateescape")


def _capture_worktree_entry(workspace: Path, relative: str) -> WorktreeEntry:
    path = _workspace_path(workspace, relative)
    if path.is_symlink():
        return WorktreeEntry("symlink", link_target=os.readlink(path))
    if path.is_file():
        return WorktreeEntry(
            "file",
            content=path.read_bytes(),
            mode=stat.S_IMODE(path.stat().st_mode),
        )
    if not path.exists():
        return WorktreeEntry("missing")
    raise GitError(f"cannot snapshot non-file repository path: {relative}")


async def _capture_ignored_entries(
    workspace: Path, relatives: list[str], write_objects: bool
) -> dict[str, WorktreeEntry]:
    entries: dict[str, WorktreeEntry] = {}
    regular: list[str] = []
    for relative in relatives:
        path = _workspace_path(workspace, relative)
        if path.is_symlink():
            entries[relative] = WorktreeEntry("symlink", link_target=os.readlink(path))
        elif path.is_file():
            regular.append(relative)
        elif not path.exists():
            entries[relative] = WorktreeEntry("missing")
        else:
            raise GitError(f"cannot snapshot non-file repository path: {relative}")
    batch = [relative for relative in regular if "\n" not in relative]
    object_ids: dict[str, str] = {}
    if batch:
        arguments = ["git", "hash-object"]
        if write_objects:
            arguments.append("-w")
        arguments.append("--stdin-paths")
        process = await asyncio.create_subprocess_exec(
            *arguments,
            cwd=workspace,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        payload = "".join(f"{relative}\n" for relative in batch).encode(
            "utf-8", errors="surrogateescape"
        )
        stdout, stderr = await process.communicate(payload)
        if process.returncode:
            raise GitError(stderr.decode(errors="replace").strip() or "git hash-object failed")
        hashes = stdout.decode("ascii").splitlines()
        if len(hashes) != len(batch):
            raise GitError("git hash-object returned an unexpected number of object IDs")
        object_ids.update(zip(batch, hashes, strict=True))
    for relative in regular:
        if relative not in object_ids:
            args = ["hash-object"]
            if write_objects:
                args.append("-w")
            args.extend(("--", relative))
            object_ids[relative] = (await git(workspace, *args)).decode("ascii").strip()
        path = _workspace_path(workspace, relative)
        entries[relative] = WorktreeEntry(
            "file",
            object_id=object_ids[relative],
            mode=stat.S_IMODE(path.stat().st_mode),
        )
    return entries


async def _restore_snapshot_entry(
    workspace: Path, relative: str, entry: WorktreeEntry
) -> None:
    if entry.kind != "file" or entry.object_id is None:
        _restore_worktree_entry(workspace, relative, entry)
        return
    content = await git(workspace, "cat-file", "blob", entry.object_id)
    _restore_worktree_entry(
        workspace,
        relative,
        WorktreeEntry("file", content=content, mode=entry.mode),
    )


def _paths_under_rules(workspace: Path, rules: list[str]) -> list[str]:
    root = workspace.resolve()
    paths: set[str] = set()
    for rule in rules:
        relative = rule.rstrip("/")
        target = _workspace_path(workspace, relative)
        if target.is_symlink() or target.is_file():
            paths.add(relative)
            continue
        if not target.is_dir():
            continue
        for directory, names, filenames in os.walk(target, followlinks=False):
            directory_path = Path(directory)
            for name in [*names, *filenames]:
                candidate = directory_path / name
                if candidate.is_symlink() or candidate.is_file():
                    paths.add(candidate.relative_to(root).as_posix())
    return sorted(paths)


async def _index_entry(workspace: Path, relative: str) -> IndexEntry | None:
    output = await git(workspace, "ls-files", "--stage", "-z", "--", relative)
    if not output:
        return None
    header, _, _ = output.partition(b"\t")
    mode, object_id, stage = header.decode("ascii").split()
    if stage != "0":
        raise GitError(f"cannot snapshot conflicted index entry: {relative}")
    return IndexEntry(mode=mode, object_id=object_id)


def _workspace_path(workspace: Path, relative: str) -> Path:
    candidate = Path(relative)
    if not relative or candidate.is_absolute() or ".." in candidate.parts:
        raise GitError(f"refusing repository path outside workspace: {relative}")
    root = workspace.resolve()
    target = root / candidate
    if not target.parent.resolve().is_relative_to(root):
        raise GitError(f"refusing repository path through symlinked parent: {relative}")
    return target


def _restore_worktree_entry(
    workspace: Path, relative: str, entry: WorktreeEntry
) -> None:
    target = _workspace_path(workspace, relative)
    if target.is_symlink() or target.is_file():
        target.unlink()
    elif target.exists():
        raise GitError(f"cannot restore file over directory: {relative}")
    if entry.kind == "missing":
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    if entry.kind == "symlink":
        if entry.link_target is None:
            raise GitError(f"missing symlink target in snapshot: {relative}")
        target.symlink_to(entry.link_target)
        return
    if entry.content is None or entry.mode is None:
        raise GitError(f"incomplete file snapshot: {relative}")
    target.write_bytes(entry.content)
    target.chmod(entry.mode)
