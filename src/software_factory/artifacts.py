from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from io import BufferedReader
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class ArtifactIntegrityError(RuntimeError):
    pass


class ArtifactRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ArtifactManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[2] = 2
    run_id: str
    workspace: str
    files: dict[str, ArtifactRecord] = Field(default_factory=dict)


class ArtifactStore:
    """Controller-owned evidence. Children may read it only under enforced protection."""

    def __init__(
        self, workspace: Path, run_id: str, *, storage_root: Path, create: bool = True
    ) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", run_id):
            raise ValueError("invalid artifact run ID")
        self.root = storage_root.absolute() / run_id
        self.workspace = workspace.resolve()
        if self.root.resolve().is_relative_to(self.workspace):
            raise ArtifactIntegrityError("controller evidence must be outside the workspace")
        self._check_parents(self.root)
        manifest_path = self.root / "manifest.json"
        if manifest_path.exists():
            with _open_regular(manifest_path) as handle:
                self._manifest_text = handle.read().decode("utf-8")
            try:
                self._manifest = ArtifactManifest.model_validate_json(self._manifest_text)
            except ValidationError as exc:
                raise ArtifactIntegrityError(
                    "invalid or unsupported evidence manifest; start a new run"
                ) from exc
            if self._manifest.run_id != run_id or self._manifest.workspace != str(self.workspace):
                raise ArtifactIntegrityError("evidence belongs to a different run or workspace")
            self.verify()
        else:
            legacy = workspace / ".factory" / "runs" / run_id
            if not create or legacy.exists() or self.root.exists():
                raise ArtifactIntegrityError(
                    "missing version-2 evidence manifest; old runs cannot resume; start a new run"
                )
            self.root.mkdir(parents=True, mode=0o700)
            self._manifest = ArtifactManifest(run_id=run_id, workspace=str(self.workspace))
            self._save_manifest()

    def path(self, relative: str) -> Path:
        part = Path(relative)
        if (
            not relative
            or part.is_absolute()
            or ".." in part.parts
            or part.as_posix() != relative
            or relative == "manifest.json"
        ):
            raise ArtifactIntegrityError(f"invalid evidence path: {relative}")
        path = self.root / part
        self._check_parents(path)
        return path

    def verify(self) -> None:
        self._check_parents(self.root / "manifest.json")
        try:
            with _open_regular(self.root / "manifest.json") as handle:
                manifest_text = handle.read().decode("utf-8")
            if manifest_text != self._manifest_text:
                raise ArtifactIntegrityError("evidence manifest was replaced")
            expected = set(self._manifest.files) | {"manifest.json"}
            actual = {
                p.relative_to(self.root).as_posix()
                for p in self.root.rglob("*")
                if not p.is_dir() or p.is_symlink()
            }
            if actual != expected:
                raise ArtifactIntegrityError(
                    "evidence files are missing or unexpected files were added"
                )
            for relative, record in self._manifest.files.items():
                path = self.path(relative)
                with _open_regular(path) as handle:
                    digest = hashlib.file_digest(handle, "sha256").hexdigest()
                if digest != record.sha256:
                    raise ArtifactIntegrityError(
                        f"evidence content was replaced or corrupted: {relative}"
                    )
        except (OSError, UnicodeError) as exc:
            raise ArtifactIntegrityError(f"cannot verify evidence: {exc}") from exc

    def read_text(self, relative: str) -> str:
        self.verify()
        if relative not in self._manifest.files:
            raise ArtifactIntegrityError(f"unrecorded evidence: {relative}")
        with _open_regular(self.path(relative)) as handle:
            content = handle.read()
        if hashlib.sha256(content).hexdigest() != self._manifest.files[relative].sha256:
            raise ArtifactIntegrityError(f"evidence changed during read: {relative}")
        return content.decode("utf-8")

    def write_text(self, relative: str, content: str) -> Path:
        self.verify()
        destination = self.path(relative)
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _atomic_write(destination, content)
        previous = self._manifest.files.get(relative)
        self._manifest.files[relative] = ArtifactRecord(
            revision=previous.revision + 1 if previous else 1,
            sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        )
        self._save_manifest()
        return destination

    def write_json(self, relative: str, value: BaseModel | dict[str, Any] | list[Any]) -> Path:
        payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
        return self.write_text(relative, json.dumps(payload, indent=2, sort_keys=True) + "\n")

    def _save_manifest(self) -> None:
        self._manifest_text = self._manifest.model_dump_json(indent=2) + "\n"
        _atomic_write(self.root / "manifest.json", self._manifest_text)

    @staticmethod
    def _check_parents(path: Path) -> None:
        if any(parent.is_symlink() for parent in [path, *path.parents]):
            raise ArtifactIntegrityError(f"evidence path contains a symlink: {path}")


def _atomic_write(destination: Path, content: str) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _open_regular(path: Path) -> BufferedReader:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise ArtifactIntegrityError(f"cannot open evidence: {path}") from exc
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ArtifactIntegrityError(f"evidence is not a regular file: {path}")
    return open(fd, "rb")
