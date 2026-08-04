"""Offline inventory and activation evidence for ModelCall checkpoint sources."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from mote.contracts.model.model_journal import ModelCallJournalRecord
from mote.runtime.models.failover.model_journal import validate_model_call_record_stream
from mote.runtime.persistence.atomic import atomic_write

MODEL_CHECKPOINT_MIGRATION_SCHEMA = "mote.model-checkpoint-migration/v1"
_RECORD = TypeAdapter(ModelCallJournalRecord)


class ModelCheckpointSourceKind(StrEnum):
    MODEL_CALL_JSONL = "model_call_jsonl"
    SESSION_PROJECTION_JSONL = "session_projection_jsonl"
    LEGACY_RUN_JSONL = "legacy_run_jsonl"
    INFERENCE_SQLITE = "inference_sqlite"


class ModelCheckpointMigrationDisposition(StrEnum):
    CANONICAL = "canonical"
    RETIRE_PROJECTION = "retire_projection"
    LEGACY = "legacy"
    BLOCKED_CORRUPT = "blocked_corrupt"
    BLOCKED_UNSUPPORTED = "blocked_unsupported"
    BLOCKED_IDENTITY_MISMATCH = "blocked_identity_mismatch"


@dataclass(frozen=True, slots=True)
class ModelCheckpointSourceEvidence:
    relative_path: str
    kind: ModelCheckpointSourceKind
    digest: str
    size: int
    disposition: ModelCheckpointMigrationDisposition
    identities: tuple[str, ...] = ()
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ModelCheckpointInventory:
    workspace_identity: str
    sources: tuple[ModelCheckpointSourceEvidence, ...]

    @property
    def blocked(self) -> tuple[ModelCheckpointSourceEvidence, ...]:
        return tuple(source for source in self.sources if source.disposition.value.startswith("blocked_"))


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _relative(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise ValueError("Model checkpoint source escaped inventory root") from exc


def _jsonl_evidence(root: Path, path: Path, kind: ModelCheckpointSourceKind) -> ModelCheckpointSourceEvidence:
    data = path.read_bytes()
    identities: list[str] = []
    disposition = (
        ModelCheckpointMigrationDisposition.CANONICAL
        if kind is ModelCheckpointSourceKind.MODEL_CALL_JSONL
        else (
            ModelCheckpointMigrationDisposition.RETIRE_PROJECTION
            if kind is ModelCheckpointSourceKind.SESSION_PROJECTION_JSONL
            else ModelCheckpointMigrationDisposition.LEGACY
        )
    )
    if data and not data.endswith(b"\n"):
        disposition = ModelCheckpointMigrationDisposition.BLOCKED_CORRUPT
    elif kind is ModelCheckpointSourceKind.MODEL_CALL_JSONL:
        try:
            records = tuple(_RECORD.validate_json(line) for line in data.splitlines())
            if records:
                identities = [records[0].model_call_id]
                if any(record.model_call_id != identities[0] for record in records):
                    disposition = ModelCheckpointMigrationDisposition.BLOCKED_IDENTITY_MISMATCH
                else:
                    validate_model_call_record_stream(records)
            else:
                disposition = ModelCheckpointMigrationDisposition.BLOCKED_CORRUPT
        except ValidationError as exc:
            error_types = {str(error.get("type", "")) for error in exc.errors()}
            disposition = (
                ModelCheckpointMigrationDisposition.BLOCKED_UNSUPPORTED
                if "literal_error" in error_types or "union_tag_invalid" in error_types
                else ModelCheckpointMigrationDisposition.BLOCKED_CORRUPT
            )
        except (TypeError, ValueError):
            disposition = ModelCheckpointMigrationDisposition.BLOCKED_CORRUPT
    return ModelCheckpointSourceEvidence(
        _relative(root, path),
        kind,
        _digest(data),
        len(data),
        disposition,
        tuple(identities),
    )


def _sqlite_evidence(root: Path, path: Path) -> ModelCheckpointSourceEvidence:
    data = path.read_bytes()
    disposition = ModelCheckpointMigrationDisposition.RETIRE_PROJECTION
    detail = "strict read-only inventory"
    try:
        connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
        try:
            result = connection.execute("PRAGMA quick_check").fetchone()
            if result != ("ok",):
                disposition = ModelCheckpointMigrationDisposition.BLOCKED_CORRUPT
                detail = "sqlite quick_check failed"
        finally:
            connection.close()
    except sqlite3.Error:
        disposition = ModelCheckpointMigrationDisposition.BLOCKED_CORRUPT
        detail = "sqlite open failed"
    return ModelCheckpointSourceEvidence(
        _relative(root, path),
        ModelCheckpointSourceKind.INFERENCE_SQLITE,
        _digest(data),
        len(data),
        disposition,
        detail=detail,
    )


def inventory_model_checkpoint_sources(root: Path) -> ModelCheckpointInventory:
    """Inventory every recognized Model recovery source without modifying it."""

    root = root.resolve()
    sources: list[ModelCheckpointSourceEvidence] = []
    if not root.exists():
        return ModelCheckpointInventory(_digest(str(root).encode()), ())
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        if path.name == "model-session-projections.jsonl":
            sources.append(_jsonl_evidence(root, path, ModelCheckpointSourceKind.SESSION_PROJECTION_JSONL))
        elif path.name == "run-journal.jsonl":
            sources.append(_jsonl_evidence(root, path, ModelCheckpointSourceKind.LEGACY_RUN_JSONL))
        elif path.suffix == ".jsonl" and path.parent.name == "model-calls":
            sources.append(_jsonl_evidence(root, path, ModelCheckpointSourceKind.MODEL_CALL_JSONL))
        elif path.suffix in {".sqlite", ".sqlite3", ".db"} and "inference" in path.as_posix().lower():
            sources.append(_sqlite_evidence(root, path))
    identity_material = json.dumps(
        [(source.relative_path, source.digest, source.disposition.value) for source in sources],
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return ModelCheckpointInventory(_digest(identity_material), tuple(sources))


def write_model_checkpoint_activation_manifest(
    inventory: ModelCheckpointInventory,
    destination: Path,
) -> str:
    """Commit secret-free activation evidence only when the inventory is safe."""

    if inventory.blocked:
        raise RuntimeError("blocked Model checkpoint evidence forbids activation")
    payload = {
        "schema": MODEL_CHECKPOINT_MIGRATION_SCHEMA,
        "workspace_identity": inventory.workspace_identity,
        "source_count": len(inventory.sources),
        "sources": [
            {
                "path": source.relative_path,
                "kind": source.kind.value,
                "digest": source.digest,
                "size": source.size,
                "disposition": source.disposition.value,
                "identities": list(source.identities),
            }
            for source in inventory.sources
        ],
        "legacy_production_reader": "retired",
        "evidence_retention_days": 180,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    atomic_write(destination, encoded, mode=0o600)
    if json.loads(destination.read_bytes()) != payload:
        raise RuntimeError("Model checkpoint activation manifest read-back failed")
    return _digest(encoded)


__all__ = [
    "MODEL_CHECKPOINT_MIGRATION_SCHEMA",
    "ModelCheckpointInventory",
    "ModelCheckpointMigrationDisposition",
    "ModelCheckpointSourceEvidence",
    "ModelCheckpointSourceKind",
    "inventory_model_checkpoint_sources",
    "write_model_checkpoint_activation_manifest",
]
