"""Offline forward-only Workflow reconciliation v2 to v3 migration."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from mote.orchestration.workflows.durable.reconciliation import WorkflowReconciliationStore
from mote.runtime.persistence import atomic_write

_V2_SCHEMA = "mote.workflow-reconciliation/v2"
_V3_SCHEMA = "mote.workflow-reconciliation/v3"
_FIELDS = {"schema", "effects", "deliveries", "governance_cancellations"}


@dataclass(frozen=True, slots=True)
class WorkflowMigrationInventory:
    source_digest: str
    effects: int
    deliveries: int
    governance_cancellations: int


@dataclass(frozen=True, slots=True)
class WorkflowMigrationCandidate:
    source_digest: str
    candidate_digest: str
    candidate_path: Path


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _load_v2(path: Path) -> tuple[bytes, dict[str, object]]:
    data = path.read_bytes()
    try:
        raw = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("legacy Workflow reconciliation source is corrupt") from exc
    if type(raw) is not dict or set(raw) != _FIELDS or raw.get("schema") != _V2_SCHEMA:
        raise ValueError("legacy Workflow reconciliation source is not strict v2")
    for field in ("effects", "deliveries", "governance_cancellations"):
        if type(raw[field]) is not list:
            raise ValueError(f"legacy Workflow reconciliation {field} is invalid")
    return data, raw


def inventory_v2(path: Path) -> WorkflowMigrationInventory:
    data, raw = _load_v2(path)
    effects = raw["effects"]
    deliveries = raw["deliveries"]
    governance = raw["governance_cancellations"]
    assert isinstance(effects, list) and isinstance(deliveries, list) and isinstance(governance, list)
    candidate = path.with_name(f".{path.name}.inventory-v3")
    try:
        _write_candidate(raw, candidate)
        # Invoke the v3 owner's strict decoder without activating its writer.
        WorkflowReconciliationStore(candidate, _UnavailableOwnership()).records()
    finally:
        candidate.unlink(missing_ok=True)
    return WorkflowMigrationInventory(
        _digest(data),
        len(effects),
        len(deliveries),
        len(governance),
    )


def build_v3_candidate(source: Path, candidate: Path) -> WorkflowMigrationCandidate:
    data, raw = _load_v2(source)
    encoded = _write_candidate(raw, candidate)
    WorkflowReconciliationStore(candidate, _UnavailableOwnership()).records()
    if candidate.read_bytes() != encoded:
        raise RuntimeError("Workflow migration candidate read-back failed")
    return WorkflowMigrationCandidate(_digest(data), _digest(encoded), candidate)


def activate_candidate(
    candidate: WorkflowMigrationCandidate,
    target: Path,
    evidence_path: Path,
    *,
    expected_source_digest: str,
) -> None:
    if candidate.source_digest != expected_source_digest or not target.is_file():
        raise ValueError("Workflow migration source preimage is unavailable")
    source = target.read_bytes()
    if _digest(source) != expected_source_digest:
        raise ValueError("Workflow migration source changed after inventory")
    if _digest(candidate.candidate_path.read_bytes()) != candidate.candidate_digest:
        raise ValueError("Workflow migration candidate changed after read-back")
    atomic_write(evidence_path, source)
    if _digest(evidence_path.read_bytes()) != expected_source_digest:
        raise RuntimeError("Workflow migration evidence read-back failed")
    os.replace(candidate.candidate_path, target)
    descriptor = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_candidate(raw: dict[str, object], path: Path) -> bytes:
    body = dict(raw)
    body["schema"] = _V3_SCHEMA
    body["tombstones"] = []
    # v2 has no terminal timestamp; migration records that absence explicitly.
    for collection in ("effects", "deliveries"):
        entries = body[collection]
        assert isinstance(entries, list)
        body[collection] = [
            {**entry, "terminal_at": None} if type(entry) is dict and "terminal_at" not in entry else entry
            for entry in entries
        ]
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    atomic_write(path, encoded)
    return encoded


class _UnavailableOwnership:
    """Decoder-only sentinel; migration inventory never acquires execution."""


__all__ = [
    "WorkflowMigrationCandidate",
    "WorkflowMigrationInventory",
    "activate_candidate",
    "build_v3_candidate",
    "inventory_v2",
]
