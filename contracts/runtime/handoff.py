"""Stable contracts for managed Runtime ownership handoff."""
from __future__ import annotations

from dataclasses import dataclass, field

from mote.contracts.runtime import RuntimeCheckpoint


@dataclass(frozen=True, slots=True)
class RuntimeHandoffIntent:
    """Durable ownership-transfer fact written before a human gets control."""

    handoff_id: str
    runtime_id: str
    kind: str
    alias: str
    epoch: int
    base_revision: int
    target_revision: int
    owner_id: str
    fencing_token: int
    mode: str
    message: str = ""
    selection: tuple[str, ...] = field(default_factory=tuple)
    base_checkpoint: RuntimeCheckpoint | None = None

    def __post_init__(self) -> None:
        for value in (
            self.handoff_id,
            self.runtime_id,
            self.kind,
            self.alias,
            self.owner_id,
            self.mode,
        ):
            if not value or any(ord(character) < 32 for character in value):
                raise ValueError("runtime handoff identity fields must be non-empty")
        if self.epoch < 1 or self.base_revision < 0:
            raise ValueError("runtime handoff epoch or revision is invalid")
        if self.target_revision != self.base_revision + 1:
            raise ValueError("runtime handoff target revision must follow its base")
        if self.fencing_token < 1:
            raise ValueError("runtime handoff fencing token must be positive")
        object.__setattr__(self, "selection", tuple(self.selection))
        checkpoint = self.base_checkpoint
        if checkpoint is not None and (
            checkpoint.runtime_id != self.runtime_id
            or checkpoint.kind != self.kind
            or checkpoint.alias != self.alias
            or checkpoint.epoch != self.epoch
            or checkpoint.revision != self.base_revision
        ):
            raise ValueError("runtime handoff base checkpoint identity is inconsistent")


@dataclass(frozen=True, slots=True)
class PendingRuntimeHandoff:
    """Replayed handoff lifecycle that has no durable terminal resolution."""

    intent: RuntimeHandoffIntent
    active: bool = False


@dataclass(frozen=True, slots=True)
class RuntimeHandoffResolution:
    """Durable terminal ownership state for one handoff epoch."""

    handoff_id: str
    status: str
    runtime_id: str
    kind: str
    alias: str
    epoch: int
    revision: int
    checkpoint: RuntimeCheckpoint | None = None

    def __post_init__(self) -> None:
        for value in (
            self.handoff_id,
            self.status,
            self.runtime_id,
            self.kind,
            self.alias,
        ):
            if not value or any(ord(character) < 32 for character in value):
                raise ValueError("runtime handoff resolution fields must be non-empty")
        if self.epoch < 1 or self.revision < 0:
            raise ValueError("runtime handoff resolution epoch or revision is invalid")


@dataclass(frozen=True, slots=True)
class RuntimeHandoffRecovery:
    """Checkpoint and stable identity selected while reclaiming ownership."""

    runtime_id: str | None = None
    epoch: int | None = None
    revision: int | None = None
    checkpoint: RuntimeCheckpoint | None = None
    recovered_handoff_ids: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "recovered_handoff_ids", tuple(self.recovered_handoff_ids))
        if self.epoch is not None and self.epoch < 1:
            raise ValueError("runtime handoff recovery epoch is invalid")
        if self.revision is not None and self.revision < 0:
            raise ValueError("runtime handoff recovery revision is invalid")


__all__ = [
    "PendingRuntimeHandoff",
    "RuntimeHandoffIntent",
    "RuntimeHandoffRecovery",
    "RuntimeHandoffResolution",
]
