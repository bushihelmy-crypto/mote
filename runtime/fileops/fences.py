"""Durable project recovery fences pointing to the authoritative session journal."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from mote.contracts.file.errors import RecoveryFenceError, RecoveryInDoubtError
from mote.contracts.file.identity import LockMode, ProjectIdentity
from mote.contracts.file.transactions import FileOperationKind

_FORMAT_VERSION = 2
_FENCE_KEYS = frozenset(
    {
        "format_version",
        "transaction_id",
        "session_id",
        "journal_path",
        "operation",
        "artifact_root",
        "project_mode",
        "projects",
        "resource_keys",
    }
)


@dataclass(frozen=True)
class RecoveryFence:
    format_version: int
    transaction_id: str
    session_id: str
    journal_path: str
    operation: FileOperationKind
    artifact_root: str
    project_mode: LockMode
    projects: tuple[ProjectIdentity, ...]
    resource_keys: tuple[str, ...]

    @classmethod
    def create(
        cls,
        *,
        transaction_id: str,
        session_id: str,
        journal_path: str,
        operation: FileOperationKind,
        artifact_root: str,
        project_mode: LockMode,
        projects: tuple[ProjectIdentity, ...],
        resource_keys: tuple[str, ...] = (),
    ) -> "RecoveryFence":
        return cls(
            format_version=_FORMAT_VERSION,
            transaction_id=transaction_id,
            session_id=session_id,
            journal_path=journal_path,
            operation=operation,
            artifact_root=artifact_root,
            project_mode=project_mode,
            projects=tuple(sorted(set(projects))),
            resource_keys=tuple(sorted(set(resource_keys))),
        )


class ProjectRecoveryFenceStore:
    """Maintains crash-durable scoped reservations for each project."""

    def __init__(self, lock_root: Path) -> None:
        self.root = Path(lock_root) / "recovery-fences"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == "posix":
            os.chmod(self.root, 0o700)

    def list(self, project: ProjectIdentity) -> tuple[RecoveryFence, ...]:
        directory = self._directory(project)
        if not directory.exists():
            return ()
        return tuple(self._read_path(project, path) for path in sorted(directory.glob("*.json")))

    def get(
        self,
        project: ProjectIdentity,
        transaction_id: str,
    ) -> RecoveryFence | None:
        path = self._path(project, transaction_id)
        if not path.exists():
            return None
        return self._read_path(project, path)

    def _read_path(
        self,
        project: ProjectIdentity,
        path: Path,
    ) -> RecoveryFence:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if type(payload) is not dict or set(payload) != _FENCE_KEYS:
                raise ValueError("recovery fence fields are not canonical")
            if type(payload["format_version"]) is not int:
                raise TypeError("recovery fence format version is not an integer")
            for field in (
                "transaction_id",
                "session_id",
                "journal_path",
                "operation",
                "artifact_root",
                "project_mode",
            ):
                if type(payload[field]) is not str:
                    raise TypeError(f"recovery fence {field} is not a string")
            if type(payload["resource_keys"]) is not list or any(
                type(key) is not str for key in payload["resource_keys"]
            ):
                raise TypeError("recovery fence resource scope is invalid")
            if type(payload["projects"]) is not list:
                raise TypeError("recovery fence project scope is invalid")
            projects: list[ProjectIdentity] = []
            for item in payload["projects"]:
                if type(item) is not dict or set(item) != {"key", "scheme"}:
                    raise TypeError("recovery fence project identity is invalid")
                if (
                    type(item["key"]) is not str
                    or not item["key"]
                    or type(item["scheme"]) is not str
                    or not item["scheme"]
                ):
                    raise TypeError("recovery fence project identity is invalid")
                projects.append(ProjectIdentity(key=item["key"], scheme=item["scheme"]))
            fence = RecoveryFence(
                format_version=payload["format_version"],
                transaction_id=payload["transaction_id"],
                session_id=payload["session_id"],
                journal_path=payload["journal_path"],
                operation=FileOperationKind(payload["operation"]),
                artifact_root=payload["artifact_root"],
                project_mode=LockMode(payload["project_mode"]),
                projects=tuple(projects),
                resource_keys=tuple(payload["resource_keys"]),
            )
            _validate_fence(fence)
            if project not in fence.projects:
                raise ValueError("recovery fence does not include its project scope")
            if path != self._path(project, fence.transaction_id):
                raise ValueError("recovery fence filename does not match transaction id")
            return fence
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise RecoveryInDoubtError(
                "project recovery fence is unreadable",
                project_identity=project.key,
                path=str(path),
                cause=exc,
            ) from exc

    def put(self, project: ProjectIdentity, fence: RecoveryFence) -> None:
        try:
            _validate_fence(fence)
            if project not in fence.projects:
                raise ValueError("recovery fence does not include target project")
        except (TypeError, ValueError) as exc:
            raise RecoveryFenceError(
                "cannot publish an invalid recovery fence",
                project_identity=project.key,
                cause=exc,
            ) from exc
        directory = self._directory(project)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == "posix":
            os.chmod(directory, 0o700)
        path = self._path(project, fence.transaction_id)
        payload = json.dumps(
            {
                "format_version": fence.format_version,
                "transaction_id": fence.transaction_id,
                "session_id": fence.session_id,
                "journal_path": fence.journal_path,
                "operation": fence.operation.value,
                "artifact_root": fence.artifact_root,
                "project_mode": fence.project_mode.value,
                "projects": [{"key": item.key, "scheme": item.scheme} for item in fence.projects],
                "resource_keys": fence.resource_keys,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        fd, raw_temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=directory)
        temp = Path(raw_temp)
        try:
            if os.name == "posix":
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb", closefd=True) as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temp, path)
            temp.unlink()
            self._fsync_directory(directory)
        except FileExistsError as exc:
            try:
                temp.unlink()
            except FileNotFoundError:
                pass
            raise RecoveryFenceError(
                "recovery fence transaction id is already reserved",
                project_identity=project.key,
                transaction_id=fence.transaction_id,
                cause=exc,
            ) from exc
        except Exception as exc:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                temp.unlink()
            except FileNotFoundError:
                pass
            raise RecoveryFenceError(
                "cannot durably publish project recovery fence",
                project_identity=project.key,
                cause=exc,
            ) from exc

    def clear(self, project: ProjectIdentity, transaction_id: str) -> None:
        path = self._path(project, transaction_id)
        try:
            path.unlink()
            self._fsync_directory(path.parent)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise RecoveryFenceError(
                "cannot durably clear project recovery fence",
                project_identity=project.key,
                transaction_id=transaction_id,
                cause=exc,
            ) from exc

    def _directory(self, project: ProjectIdentity) -> Path:
        return self.root / project.key

    def _path(self, project: ProjectIdentity, transaction_id: str) -> Path:
        key = hashlib.sha256(transaction_id.encode("utf-8")).hexdigest()
        return self._directory(project) / f"{key}.json"

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _validate_fence(fence: RecoveryFence) -> None:
    if type(fence.format_version) is not int or fence.format_version != _FORMAT_VERSION:
        raise ValueError("unsupported recovery fence format")
    if type(fence.transaction_id) is not str or not fence.transaction_id:
        raise ValueError("recovery fence transaction id is invalid")
    if type(fence.session_id) is not str or not fence.session_id:
        raise ValueError("recovery fence session id is invalid")
    if type(fence.journal_path) is not str or not Path(fence.journal_path).is_absolute():
        raise ValueError("recovery fence journal path is not absolute")
    if type(fence.artifact_root) is not str:
        raise TypeError("recovery fence artifact root is not a string")
    if type(fence.operation) is not FileOperationKind:
        raise TypeError("recovery fence operation is invalid")
    if type(fence.project_mode) is not LockMode:
        raise TypeError("recovery fence project mode is invalid")
    if type(fence.projects) is not tuple or not fence.projects:
        raise TypeError("recovery fence project scope is invalid")
    for project in fence.projects:
        if type(project) is not ProjectIdentity:
            raise TypeError("recovery fence project identity is invalid")
        if not project.key or not project.scheme:
            raise ValueError("recovery fence project identity is invalid")
    if tuple(sorted(set(fence.projects))) != fence.projects:
        raise ValueError("recovery fence project scope is not canonical")
    if type(fence.resource_keys) is not tuple or any(type(key) is not str or not key for key in fence.resource_keys):
        raise TypeError("recovery fence resource scope is invalid")
    if tuple(sorted(set(fence.resource_keys))) != fence.resource_keys:
        raise ValueError("recovery fence resource scope is not canonical")
    if fence.operation == FileOperationKind.MUTATION:
        if fence.project_mode != LockMode.SHARED or not fence.resource_keys:
            raise ValueError("mutation recovery fence scope is invalid")
        if not fence.artifact_root or not Path(fence.artifact_root).is_absolute():
            raise ValueError("mutation recovery fence artifact root is invalid")
        return
    if fence.operation == FileOperationKind.REWIND:
        if fence.project_mode != LockMode.EXCLUSIVE or len(fence.projects) != 1 or fence.resource_keys:
            raise ValueError("rewind recovery fence scope is invalid")
        if fence.artifact_root:
            raise ValueError("rewind recovery fence cannot carry an artifact root")
        return
    raise ValueError("unsupported recovery fence operation")


__all__ = ["ProjectRecoveryFenceStore", "RecoveryFence"]
