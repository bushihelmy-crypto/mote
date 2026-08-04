"""Fenced compaction and purge owner for terminal ModelCall evidence."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path

from filelock import FileLock

from mote.contracts.model.checkpoint import ModelCheckpointPolicy
from mote.contracts.model.failover import ModelCallState
from mote.runtime.models.failover.model_journal import LocalModelCallJournal
from mote.runtime.persistence.atomic import atomic_write

MODEL_CALL_TOMBSTONE_SCHEMA = "mote.model-call-tombstone/v1"


class ModelCheckpointMaintenanceAction(StrEnum):
    COMPACT_TERMINAL = "compact_terminal"
    PURGE_TOMBSTONE = "purge_tombstone"


class ModelCheckpointMaintenanceDisposition(StrEnum):
    APPLIED = "applied"
    NOT_ELIGIBLE = "not_eligible"
    FENCED = "fenced"
    ABSENT = "absent"


@dataclass(frozen=True, slots=True)
class ModelCheckpointMaintenanceCommand:
    command_id: str
    action: ModelCheckpointMaintenanceAction
    model_call_id: str
    authority_id: str
    fencing_token: int
    now: datetime

    def __post_init__(self) -> None:
        if not self.command_id or not self.model_call_id or not self.authority_id or self.fencing_token < 1:
            raise ValueError("Model checkpoint maintenance identity is invalid")
        if self.now.tzinfo is None or self.now.utcoffset() is None:
            raise ValueError("Model checkpoint maintenance instant must be timezone-aware")


@dataclass(frozen=True, slots=True)
class ModelCheckpointMaintenanceReceipt:
    command_id: str
    model_call_id: str
    action: ModelCheckpointMaintenanceAction
    disposition: ModelCheckpointMaintenanceDisposition
    evidence_path: str = ""


class ModelCheckpointMaintenance:
    def __init__(
        self,
        journal_root: Path,
        *,
        policy: ModelCheckpointPolicy,
        fencing_token: int,
    ) -> None:
        if fencing_token < 1:
            raise ValueError("Model checkpoint maintenance fence must be positive")
        self._root = Path(journal_root)
        self._policy = policy
        self._fencing_token = fencing_token
        self._journal = LocalModelCallJournal(self._root, policy=policy)
        self._tombstones = self._root / "tombstones"
        self._lock = FileLock(str(self._root / ".maintenance.lock"))

    def execute(self, command: ModelCheckpointMaintenanceCommand) -> ModelCheckpointMaintenanceReceipt:
        if command.fencing_token != self._fencing_token:
            return self._receipt(command, ModelCheckpointMaintenanceDisposition.FENCED)
        with self._lock:
            if command.action is ModelCheckpointMaintenanceAction.COMPACT_TERMINAL:
                return self._compact(command)
            return self._purge(command)

    def _compact(self, command: ModelCheckpointMaintenanceCommand) -> ModelCheckpointMaintenanceReceipt:
        path = self._journal.path_for(command.model_call_id)
        if not path.exists():
            return self._receipt(command, ModelCheckpointMaintenanceDisposition.ABSENT)
        recovery = self._journal.recover(command.model_call_id)
        terminal = recovery.terminal
        if terminal is None or recovery.state in {
            ModelCallState.PLANNED,
            ModelCallState.RUNNING,
            ModelCallState.IN_DOUBT,
        }:
            return self._receipt(command, ModelCheckpointMaintenanceDisposition.NOT_ELIGIBLE)
        terminal_at = terminal.occurred_at
        if command.now < terminal_at + timedelta(days=self._policy.terminal_retention_days):
            return self._receipt(command, ModelCheckpointMaintenanceDisposition.NOT_ELIGIBLE)
        tombstone = {
            "schema": MODEL_CALL_TOMBSTONE_SCHEMA,
            "model_call_id": recovery.model_call_id,
            "state": recovery.state.value,
            "attempts_started": recovery.attempts_started,
            "attempts_finished": recovery.attempts_finished,
            "wire_attempts": terminal.wire_attempts,
            "cost_usd": str(terminal.cost_usd),
            "terminal_at": terminal_at.isoformat(),
            "purge_after": (terminal_at + timedelta(days=self._policy.tombstone_retention_days)).isoformat(),
        }
        target = self._tombstone_path(command.model_call_id)
        atomic_write(target, json.dumps(tombstone, sort_keys=True, separators=(",", ":")).encode(), mode=0o600)
        if json.loads(target.read_bytes()) != tombstone:
            raise RuntimeError("ModelCall tombstone read-back failed")
        path.unlink()
        self._fsync(path.parent)
        return self._receipt(command, ModelCheckpointMaintenanceDisposition.APPLIED, target)

    def _purge(self, command: ModelCheckpointMaintenanceCommand) -> ModelCheckpointMaintenanceReceipt:
        path = self._tombstone_path(command.model_call_id)
        if not path.exists():
            return self._receipt(command, ModelCheckpointMaintenanceDisposition.ABSENT)
        try:
            raw = json.loads(path.read_bytes())
            purge_after = datetime.fromisoformat(raw["purge_after"])
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("ModelCall tombstone is corrupt or unsupported") from exc
        if (
            type(raw) is not dict
            or raw.get("schema") != MODEL_CALL_TOMBSTONE_SCHEMA
            or raw.get("model_call_id") != command.model_call_id
            or purge_after.tzinfo is None
        ):
            raise ValueError("ModelCall tombstone identity or schema is invalid")
        if command.now < purge_after:
            return self._receipt(command, ModelCheckpointMaintenanceDisposition.NOT_ELIGIBLE)
        path.unlink()
        self._fsync(path.parent)
        return self._receipt(command, ModelCheckpointMaintenanceDisposition.APPLIED)

    def _tombstone_path(self, model_call_id: str) -> Path:
        digest_path = self._journal.path_for(model_call_id)
        return self._tombstones / f"{digest_path.stem}.json"

    @staticmethod
    def _receipt(
        command: ModelCheckpointMaintenanceCommand,
        disposition: ModelCheckpointMaintenanceDisposition,
        path: Path | None = None,
    ) -> ModelCheckpointMaintenanceReceipt:
        return ModelCheckpointMaintenanceReceipt(
            command.command_id,
            command.model_call_id,
            command.action,
            disposition,
            "" if path is None else str(path),
        )

    @staticmethod
    def _fsync(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


__all__ = [
    "MODEL_CALL_TOMBSTONE_SCHEMA",
    "ModelCheckpointMaintenance",
    "ModelCheckpointMaintenanceAction",
    "ModelCheckpointMaintenanceCommand",
    "ModelCheckpointMaintenanceDisposition",
    "ModelCheckpointMaintenanceReceipt",
]
