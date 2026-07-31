"""Crash-safe local audit store for resource operator controls."""

from __future__ import annotations

import os
import threading
from pathlib import Path

from mote.contracts.model.failover import OperatorTransition
from mote.runtime.telemetry.logging import log_class

MODEL_OPERATOR_AUDIT_FILENAME = "model-operator-audit.jsonl"


class OperatorAuditIntegrityError(RuntimeError):
    pass


class OperatorControlError(RuntimeError):
    pass


class OperatorAuditRequiredError(OperatorControlError):
    pass


class OperatorRevisionConflict(OperatorControlError):
    pass


class OperatorDrainIncompleteError(OperatorControlError):
    pass


@log_class(level="DEBUG", exclude={"path"})
class LocalModelOperatorAuditStore:
    """Append-only JSONL control audit with fsync-before-state-change semantics."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def append(self, transition: OperatorTransition) -> None:
        payload = transition.model_dump_json().encode("utf-8") + b"\n"
        with self._lock:
            existed = self._path.exists()
            self._path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(
                self._path,
                os.O_APPEND | os.O_CREAT | os.O_WRONLY,
                0o600,
            )
            try:
                written = 0
                while written < len(payload):
                    count = os.write(descriptor, payload[written:])
                    if count <= 0:
                        raise OSError("operator audit append made no progress")
                    written += count
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            if not existed:
                directory = os.open(self._path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)

    def records(self) -> tuple[OperatorTransition, ...]:
        if not self._path.exists():
            return ()
        records: list[OperatorTransition] = []
        with self._lock:
            try:
                with self._path.open("rb") as stream:
                    for line_number, line in enumerate(stream, start=1):
                        if not line.endswith(b"\n"):
                            raise OperatorAuditIntegrityError(f"operator audit line {line_number} is incomplete")
                        try:
                            records.append(OperatorTransition.model_validate_json(line))
                        except ValueError as exc:
                            raise OperatorAuditIntegrityError(f"operator audit line {line_number} is invalid") from exc
            except OSError as exc:
                raise OperatorAuditIntegrityError("operator audit cannot be read") from exc
        return tuple(records)


def model_operator_audit_path(workspace_root: Path) -> Path:
    return workspace_root / ".runtime" / MODEL_OPERATOR_AUDIT_FILENAME


__all__ = [
    "LocalModelOperatorAuditStore",
    "MODEL_OPERATOR_AUDIT_FILENAME",
    "OperatorAuditIntegrityError",
    "OperatorAuditRequiredError",
    "OperatorControlError",
    "OperatorDrainIncompleteError",
    "OperatorRevisionConflict",
    "model_operator_audit_path",
]
