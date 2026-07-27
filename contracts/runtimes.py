"""Stable value contracts for managed stateful interactive runtimes."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import FrozenSet, Optional


def _validate_projection_identifier(value: str, *, field_name: str) -> None:
    if type(value) is not str or not value or len(value) > 256 or any(ord(character) < 32 for character in value):
        raise ValueError(f"runtime projection {field_name} is invalid")


class RuntimeState(StrEnum):
    DECLARED = "declared"
    STARTING = "starting"
    READY = "ready"
    BUSY = "busy"
    HANDED_OFF = "handed_off"
    RESTORING = "restoring"
    DEGRADED = "degraded"
    CLOSING = "closing"
    CLOSED = "closed"
    FAILED = "failed"


class CheckpointFidelity(StrEnum):
    NONE = "none"
    LOGICAL = "logical"
    PARTIAL = "partial"
    FULL = "full"


class RuntimeAccessMode(StrEnum):
    READ = "read"
    WRITE = "write"


class RuntimeDurabilityState(StrEnum):
    NOT_CONFIGURED = "not_configured"
    CURRENT = "current"
    LAGGING = "lagging"


@dataclass(frozen=True, slots=True)
class RuntimeRef:
    """Stable machine identity plus a session-local readable alias."""

    runtime_id: str
    kind: str
    alias: str = "default"

    def __post_init__(self) -> None:
        if not self.runtime_id or not self.kind or not self.alias:
            raise ValueError("runtime_id, kind and alias must be non-empty")
        if ":" in self.kind or ":" in self.alias:
            raise ValueError("runtime kind and alias must not contain ':'")

    @property
    def readable(self) -> str:
        return f"{self.kind}:{self.alias}"

    def __str__(self) -> str:
        return self.readable


@dataclass(frozen=True, slots=True)
class RuntimeCapabilities:
    checkpoint_fidelity: CheckpointFidelity = CheckpointFidelity.NONE
    handoff_modes: FrozenSet[str] = field(default_factory=frozenset)
    surface_kinds: FrozenSet[str] = field(default_factory=frozenset)
    multi_instance: bool = False


@dataclass(frozen=True, slots=True)
class RuntimeDescriptor:
    ref: RuntimeRef
    state: RuntimeState
    epoch: int
    revision: int
    capabilities: RuntimeCapabilities
    durability: RuntimeDurabilityState = RuntimeDurabilityState.NOT_CONFIGURED
    recoverable_revision: int | None = None
    durability_detail: str = ""


@dataclass(frozen=True, slots=True)
class RuntimeHealth:
    healthy: bool
    status: str = "ready"
    detail: str = ""
    durability: RuntimeDurabilityState = RuntimeDurabilityState.NOT_CONFIGURED
    current_revision: int | None = None
    recoverable_revision: int | None = None
    durability_detail: str = ""


@dataclass(frozen=True, slots=True)
class RuntimeCheckpoint:
    runtime_id: str
    kind: str
    epoch: int
    revision: int
    codec: str
    schema_version: int
    payload_ref: str
    digest: str = ""
    sensitivity: str = "private"
    fidelity: CheckpointFidelity = CheckpointFidelity.NONE
    alias: str = "default"


@dataclass(frozen=True, slots=True)
class RuntimeProjectionIntent:
    """Versioned request to derive a projection from a Runtime checkpoint.

    The checkpoint is the sole source of domain state. Options are deliberately
    small immutable strings: an intent never embeds exported bytes, filesystem
    paths or Artifact CAS references. A projector resolves ``projector`` and
    ``schema_version`` and materializes any downstream publication itself.
    """

    intent_id: str
    projector: str
    schema_version: int
    options: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        _validate_projection_identifier(self.intent_id, field_name="intent_id")
        _validate_projection_identifier(self.projector, field_name="projector")
        if type(self.schema_version) is not int or self.schema_version < 1:
            raise ValueError("runtime projection schema_version must be a positive integer")
        options = tuple(tuple(item) for item in self.options)
        keys: list[str] = []
        for item in options:
            if len(item) != 2:
                raise ValueError("runtime projection options must be key/value pairs")
            key, value = item
            _validate_projection_identifier(key, field_name="option key")
            if type(value) is not str or len(value) > 4096 or any(ord(character) < 32 for character in value):
                raise ValueError("runtime projection option value is invalid")
            keys.append(key)
        if len(keys) != len(set(keys)):
            raise ValueError("runtime projection option keys must be unique")
        object.__setattr__(self, "options", options)


@dataclass(frozen=True, slots=True)
class RuntimeCommitFact:
    """One durable Runtime checkpoint and the projections derived from it."""

    commit_id: str
    checkpoint: RuntimeCheckpoint
    projections: tuple[RuntimeProjectionIntent, ...] = ()
    reason: str = ""

    def __post_init__(self) -> None:
        _validate_projection_identifier(self.commit_id, field_name="commit_id")
        if not isinstance(self.checkpoint, RuntimeCheckpoint):
            raise TypeError("runtime commit fact requires a RuntimeCheckpoint")
        projections = tuple(self.projections)
        if any(not isinstance(item, RuntimeProjectionIntent) for item in projections):
            raise TypeError("runtime commit projections must be RuntimeProjectionIntent values")
        intent_ids = [item.intent_id for item in projections]
        if len(intent_ids) != len(set(intent_ids)):
            raise ValueError("runtime commit projection intent IDs must be unique")
        if type(self.reason) is not str or any(ord(character) < 32 for character in self.reason):
            raise ValueError("runtime commit reason is invalid")
        object.__setattr__(self, "projections", projections)


@dataclass(frozen=True, slots=True)
class RuntimeProjectionRequest:
    """One replayable unit of projection work derived from a commit fact."""

    commit_id: str
    checkpoint: RuntimeCheckpoint
    intent: RuntimeProjectionIntent
    attempts: int = 0

    def __post_init__(self) -> None:
        _validate_projection_identifier(self.commit_id, field_name="commit_id")
        if not isinstance(self.checkpoint, RuntimeCheckpoint):
            raise TypeError("runtime projection request requires a RuntimeCheckpoint")
        if not isinstance(self.intent, RuntimeProjectionIntent):
            raise TypeError("runtime projection request requires a RuntimeProjectionIntent")
        if type(self.attempts) is not int or self.attempts < 0:
            raise ValueError("runtime projection attempts must be non-negative")

    @property
    def key(self) -> tuple[str, str]:
        return self.commit_id, self.intent.intent_id


@dataclass(frozen=True, slots=True)
class RuntimeProjectionAck:
    """Durable acknowledgement that one projection request was accepted."""

    commit_id: str
    intent_id: str
    status: str = "completed"
    error: str = ""
    attempts: int = 0

    def __post_init__(self) -> None:
        _validate_projection_identifier(self.commit_id, field_name="commit_id")
        _validate_projection_identifier(self.intent_id, field_name="intent_id")
        if self.status not in {"completed", "retry_scheduled", "dead_letter"}:
            raise ValueError("runtime projection acknowledgement status is invalid")
        if type(self.error) is not str or len(self.error) > 4096:
            raise ValueError("runtime projection acknowledgement error is invalid")
        if self.status != "completed" and not self.error:
            raise ValueError("runtime projection failure outcome requires an error")
        if type(self.attempts) is not int or self.attempts < 0:
            raise ValueError("runtime projection attempts must be non-negative")

    @property
    def key(self) -> tuple[str, str]:
        return self.commit_id, self.intent_id


@dataclass(frozen=True, slots=True)
class RuntimeProjectionFailure:
    """One projection request that could not be materialized or published."""

    commit_id: str
    intent_id: str
    error: str
    retryable: bool = False
    attempts: int = 1

    def __post_init__(self) -> None:
        _validate_projection_identifier(self.commit_id, field_name="commit_id")
        _validate_projection_identifier(self.intent_id, field_name="intent_id")
        if type(self.error) is not str or not self.error or len(self.error) > 4096:
            raise ValueError("runtime projection failure error is invalid")
        if type(self.retryable) is not bool:
            raise TypeError("runtime projection retryable flag must be boolean")
        if type(self.attempts) is not int or self.attempts < 1:
            raise ValueError("runtime projection attempts must be positive")


@dataclass(frozen=True, slots=True)
class RuntimeProjectionReconcileResult:
    """Batch result; one failed projection never blocks independent requests."""

    completed: tuple[RuntimeProjectionAck, ...] = ()
    failed: tuple[RuntimeProjectionFailure, ...] = ()
    dead_lettered: tuple[RuntimeProjectionFailure, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "completed", tuple(self.completed))
        object.__setattr__(self, "failed", tuple(self.failed))
        object.__setattr__(self, "dead_lettered", tuple(self.dead_lettered))


@dataclass(frozen=True, slots=True)
class RuntimeOperationIntent:
    """Write-ahead record for one deterministic, opt-in Runtime mutation."""

    operation_id: str
    runtime_id: str
    kind: str
    alias: str
    epoch: int
    base_revision: int
    target_revision: int
    codec: str
    schema_version: int
    payload: str
    base_checkpoint: RuntimeCheckpoint
    projections: tuple[RuntimeProjectionIntent, ...] = ()

    def __post_init__(self) -> None:
        for field_name, value in (
            ("operation_id", self.operation_id),
            ("runtime_id", self.runtime_id),
            ("kind", self.kind),
            ("alias", self.alias),
            ("codec", self.codec),
        ):
            _validate_projection_identifier(value, field_name=field_name)
        if type(self.epoch) is not int or self.epoch < 1:
            raise ValueError("runtime operation epoch must be a positive integer")
        if type(self.base_revision) is not int or self.base_revision < 0:
            raise ValueError("runtime operation base_revision is invalid")
        if self.target_revision != self.base_revision + 1:
            raise ValueError("runtime operation target_revision must follow its base")
        if type(self.schema_version) is not int or self.schema_version < 1:
            raise ValueError("runtime operation schema_version must be a positive integer")
        if type(self.payload) is not str or not self.payload or len(self.payload) > 4_194_304:
            raise ValueError("runtime operation payload is invalid")
        if not isinstance(self.base_checkpoint, RuntimeCheckpoint):
            raise TypeError("runtime operation requires a base checkpoint")
        checkpoint = self.base_checkpoint
        if (
            checkpoint.runtime_id != self.runtime_id
            or checkpoint.kind != self.kind
            or checkpoint.alias != self.alias
            or checkpoint.epoch != self.epoch
            or checkpoint.revision != self.base_revision
        ):
            raise ValueError("runtime operation base checkpoint identity is inconsistent")
        projections = tuple(self.projections)
        if any(not isinstance(item, RuntimeProjectionIntent) for item in projections):
            raise TypeError("runtime operation projections must be RuntimeProjectionIntent values")
        object.__setattr__(self, "projections", projections)

    def fingerprint(self) -> str:
        """Stable business identity, independent of mutable Runtime revision."""
        canonical = json.dumps(
            {
                "runtime_id": self.runtime_id,
                "kind": self.kind,
                "alias": self.alias,
                "codec": self.codec,
                "schema_version": self.schema_version,
                "payload": self.payload,
                "projections": [
                    {
                        "intent_id": item.intent_id,
                        "projector": item.projector,
                        "schema_version": item.schema_version,
                        "options": list(item.options),
                    }
                    for item in self.projections
                ],
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()


@dataclass(frozen=True, slots=True)
class RuntimeOperationReceipt:
    """Durable completion proving one operation must not be applied again."""

    operation_id: str
    fingerprint: str
    runtime_id: str
    kind: str
    alias: str
    epoch: int
    revision: int
    changed: bool
    commit_id: str = ""

    def __post_init__(self) -> None:
        for field_name, value in (
            ("operation_id", self.operation_id),
            ("runtime_id", self.runtime_id),
            ("kind", self.kind),
            ("alias", self.alias),
        ):
            _validate_projection_identifier(value, field_name=field_name)
        if (
            type(self.fingerprint) is not str
            or len(self.fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in self.fingerprint)
        ):
            raise ValueError("runtime operation fingerprint is invalid")
        if type(self.epoch) is not int or self.epoch < 1:
            raise ValueError("runtime operation receipt epoch is invalid")
        if type(self.revision) is not int or self.revision < 0:
            raise ValueError("runtime operation receipt revision is invalid")
        if type(self.changed) is not bool:
            raise TypeError("runtime operation receipt changed flag must be boolean")
        if self.commit_id:
            _validate_projection_identifier(self.commit_id, field_name="commit_id")

    @classmethod
    def from_intent(
        cls,
        intent: RuntimeOperationIntent,
        *,
        revision: int | None = None,
        changed: bool = True,
        commit_id: str = "",
    ) -> "RuntimeOperationReceipt":
        return cls(
            operation_id=intent.operation_id,
            fingerprint=intent.fingerprint(),
            runtime_id=intent.runtime_id,
            kind=intent.kind,
            alias=intent.alias,
            epoch=intent.epoch,
            revision=intent.target_revision if revision is None else revision,
            changed=changed,
            commit_id=commit_id,
        )


@dataclass(frozen=True, slots=True)
class RuntimeOperationRecovery:
    """Base checkpoint and unapplied WAL entries required to restore a Runtime."""

    checkpoint: RuntimeCheckpoint | None = None
    operations: tuple[RuntimeOperationIntent, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "operations", tuple(self.operations))


@dataclass(frozen=True, slots=True)
class DriverStartResult:
    restored: bool = False
    detail: str = ""


@dataclass(frozen=True, slots=True)
class DriverCheckpoint:
    codec: str
    schema_version: int
    payload_ref: str
    digest: str = ""
    sensitivity: str = "private"
    fidelity: Optional[CheckpointFidelity] = None


__all__ = [
    "CheckpointFidelity",
    "DriverCheckpoint",
    "DriverStartResult",
    "RuntimeAccessMode",
    "RuntimeCapabilities",
    "RuntimeCheckpoint",
    "RuntimeCommitFact",
    "RuntimeDescriptor",
    "RuntimeDurabilityState",
    "RuntimeHealth",
    "RuntimeOperationIntent",
    "RuntimeOperationReceipt",
    "RuntimeOperationRecovery",
    "RuntimeProjectionAck",
    "RuntimeProjectionFailure",
    "RuntimeProjectionIntent",
    "RuntimeProjectionReconcileResult",
    "RuntimeProjectionRequest",
    "RuntimeRef",
    "RuntimeState",
]
