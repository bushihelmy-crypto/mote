"""Offline ServiceCall v2 stream inventory and v3 candidate cutover."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from mote.contracts.service import ServiceCallJournalRecord, ServiceCallOwnerEpoch, ServiceCancelCommand
from mote.runtime.persistence.atomic import atomic_write
from mote.runtime.service_gateway.journal import SERVICE_CALL_ROOT_MANIFEST_SCHEMA

_ADAPTER = TypeAdapter(ServiceCallJournalRecord)
_OLD_VERSIONS = {
    "service_call_planned": 2,
    "service_attempt_started": 1,
    "service_receipt_accepted": 1,
    "service_call_suspended": 1,
    "service_attempt_finished": 1,
    "service_decision_applied": 1,
    "service_call_finished": 1,
}


@dataclass(frozen=True, slots=True)
class ServiceCallMigrationInventory:
    source_digest: str
    service_call_id: str
    record_count: int


@dataclass(frozen=True, slots=True)
class ServiceCallMigrationCandidate:
    inventory: ServiceCallMigrationInventory
    candidate_path: Path
    candidate_digest: str


@dataclass(frozen=True, slots=True)
class ServiceCallRootMigrationReceipt:
    stream_count: int
    source_digest: str
    evidence_path: Path


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _decode_v2(path: Path) -> tuple[bytes, list[dict[str, object]], str]:
    data = path.read_bytes()
    records: list[dict[str, object]] = []
    identity: str | None = None
    for line_number, line in enumerate(data.splitlines(keepends=True), start=1):
        if not line.endswith(b"\n"):
            raise ValueError(f"legacy ServiceCall line {line_number} is incomplete")
        try:
            raw = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"legacy ServiceCall line {line_number} is corrupt") from exc
        if type(raw) is not dict or type(raw.get("kind")) is not str:
            raise ValueError("legacy ServiceCall record shape is invalid")
        expected = _OLD_VERSIONS.get(raw["kind"])
        if expected is None or raw.get("schema_version") != expected:
            raise ValueError("legacy ServiceCall kind or schema is unsupported")
        call_id = raw.get("service_call_id")
        if type(call_id) is not str or not call_id:
            raise ValueError("legacy ServiceCall identity is invalid")
        if identity is None:
            identity = call_id
        elif identity != call_id:
            raise ValueError("legacy ServiceCall stream contains mixed identities")
        mapped = dict(raw)
        mapped["schema_version"] = 3
        try:
            _ADAPTER.validate_json(json.dumps(mapped, separators=(",", ":")))
        except ValidationError as exc:
            raise ValueError(f"legacy ServiceCall line {line_number} cannot forward migrate") from exc
        records.append(mapped)
    if identity is None:
        raise ValueError("legacy ServiceCall stream is empty")
    return data, records, identity


def inventory_v2(path: Path) -> ServiceCallMigrationInventory:
    data, records, identity = _decode_v2(path)
    return ServiceCallMigrationInventory(_digest(data), identity, len(records))


def build_v3_candidate(source: Path, candidate: Path) -> ServiceCallMigrationCandidate:
    data, records, identity = _decode_v2(source)
    encoded = b"".join(json.dumps(record, sort_keys=True, separators=(",", ":")).encode() + b"\n" for record in records)
    atomic_write(candidate, encoded, mode=0o600)
    # Strict v3 read-back validates every mapped discriminated record.
    for line in candidate.read_bytes().splitlines():
        _ADAPTER.validate_json(line)
    inventory = ServiceCallMigrationInventory(_digest(data), identity, len(records))
    return ServiceCallMigrationCandidate(inventory, candidate, _digest(encoded))


def activate_candidate(
    candidate: ServiceCallMigrationCandidate,
    target: Path,
    evidence_path: Path,
    *,
    expected_source_digest: str,
) -> None:
    if (
        candidate.inventory.source_digest != expected_source_digest
        or _digest(target.read_bytes()) != expected_source_digest
    ):
        raise ValueError("ServiceCall migration source changed after inventory")
    if _digest(candidate.candidate_path.read_bytes()) != candidate.candidate_digest:
        raise ValueError("ServiceCall migration candidate changed after read-back")
    atomic_write(evidence_path, target.read_bytes(), mode=0o600)
    os.replace(candidate.candidate_path, target)
    descriptor = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def migrate_service_call_root_v3(root: Path) -> ServiceCallRootMigrationReceipt:
    """Forward one complete v2 root, including owner/cancel sidecars, as one generation."""
    root = Path(root)
    streams = tuple(sorted(root.glob("*.jsonl")))
    if not streams:
        raise ValueError("ServiceCall root migration source is empty")
    source_hasher = hashlib.sha256()
    inventories: list[tuple[Path, bytes, list[dict[str, object]], str]] = []
    expected_sidecars: set[Path] = set()
    for stream in streams:
        data, records, call_id = _decode_v2(stream)
        if stream.name != f"{hashlib.sha256(call_id.encode()).hexdigest()}.jsonl":
            raise ValueError("legacy ServiceCall filename does not match its CallId")
        owner = stream.with_suffix(".owner.json")
        cancel = stream.with_suffix(".cancel")
        if not owner.is_file():
            raise ValueError("legacy ServiceCall stream is missing its owner sidecar")
        expected_sidecars.add(owner)
        if cancel.exists():
            expected_sidecars.add(cancel)
        for path in (stream, owner, *((cancel,) if cancel.exists() else ())):
            payload = path.read_bytes()
            source_hasher.update(path.name.encode() + b"\0" + payload + b"\0")
        inventories.append((stream, data, records, call_id))
    actual_sidecars = set(root.glob("*.owner.json")) | set(root.glob("*.cancel"))
    if actual_sidecars != expected_sidecars:
        raise ValueError("ServiceCall root contains orphan or unaccounted sidecars")
    source_digest = "sha256:" + source_hasher.hexdigest()
    candidate = root.with_name(f".{root.name}.v3-candidate")
    if candidate.exists():
        shutil.rmtree(candidate)
    candidate.mkdir(parents=True, mode=0o700)
    for stream, _data, records, call_id in inventories:
        encoded = b"".join(
            json.dumps({**record, "schema_version": 3}, sort_keys=True, separators=(",", ":")).encode() + b"\n"
            for record in records
        )
        target = candidate / stream.name
        atomic_write(target, encoded, mode=0o600)
        for line in target.read_bytes().splitlines():
            _ADAPTER.validate_json(line)
        owner_raw = json.loads(stream.with_suffix(".owner.json").read_text(encoding="utf-8"))
        if (
            type(owner_raw) is not dict
            or set(owner_raw) != {"generation"}
            or type(owner_raw["generation"]) is not int
            or owner_raw["generation"] < 1
        ):
            raise ValueError("legacy ServiceCall owner sidecar is invalid")
        epoch = ServiceCallOwnerEpoch(
            service_call_id=call_id,
            owner_id="migration-retired-owner",
            generation=owner_raw["generation"],
            fencing_token=owner_raw["generation"],
            revision=1,
        )
        atomic_write(candidate / stream.with_suffix(".owner.json").name, epoch.model_dump_json().encode(), mode=0o600)
        legacy_cancel = stream.with_suffix(".cancel")
        if legacy_cancel.exists():
            raw_cancel = json.loads(legacy_cancel.read_text(encoding="utf-8"))
            if raw_cancel != {"command": "cancel", "schema": 1}:
                raise ValueError("legacy ServiceCall cancel sidecar is invalid")
            command = ServiceCancelCommand(
                command_id="svc-cancel-" + hashlib.sha256(f"{call_id}\0{len(records)}".encode()).hexdigest(),
                service_call_id=call_id,
                authority_id="v2-migration",
                expected_stream_revision=len(records),
            )
            atomic_write(candidate / legacy_cancel.name, command.model_dump_json().encode(), mode=0o600)
    atomic_write(
        candidate / "activation-manifest.json",
        json.dumps(
            {
                "schema": SERVICE_CALL_ROOT_MANIFEST_SCHEMA,
                "generation": 3,
                "source_digest": source_digest,
                "evidence_retention_days": 180,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode(),
        mode=0o600,
    )
    evidence = root.with_name(f"{root.name}.v2-evidence-{source_hasher.hexdigest()}")
    if evidence.exists():
        raise ValueError("ServiceCall migration evidence path already exists")
    os.replace(root, evidence)
    try:
        os.replace(candidate, root)
    except BaseException:
        os.replace(evidence, root)
        raise
    descriptor = os.open(root.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return ServiceCallRootMigrationReceipt(len(streams), source_digest, evidence)


__all__ = [
    "ServiceCallMigrationCandidate",
    "ServiceCallMigrationInventory",
    "activate_candidate",
    "build_v3_candidate",
    "inventory_v2",
    "ServiceCallRootMigrationReceipt",
    "migrate_service_call_root_v3",
]
