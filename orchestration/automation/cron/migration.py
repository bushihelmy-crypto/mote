"""Offline, migration-only Cron v2 inventory and v3 candidate construction."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from mote.contracts.clock import UNIX_UTC_CLOCK, AbsoluteInstant
from mote.orchestration.automation.cron.store import CronScheduleSnapshot, CronTaskStore
from mote.orchestration.automation.cron.task import CronTask
from mote.runtime.persistence import atomic_write

_V2_SCHEMA = "mote.cron-schedule/v2"
_V3_SCHEMA = "mote.cron-schedule/v3"
_ENVELOPE_FIELDS = {"schema", "schedule_id", "revision", "tasks", "occurrences"}
_TASK_FIELDS = {
    "id",
    "revision",
    "cron",
    "prompt",
    "created_at",
    "last_fired_at",
    "recurring",
    "permanent",
    "agent_id",
    "target_session_id",
    "timezone_name",
    "misfire_policy",
    "overlap_policy",
    "dst_policy",
}
_TASK_FIELDS_V3 = _TASK_FIELDS | {"prompt_artifact_ref"}
_OCCURRENCE_FIELDS = {
    "occurrence_id",
    "task_id",
    "task_revision",
    "scheduled_at",
    "observed_at",
    "state",
    "attempt",
    "receipt_id",
    "reason",
    "next_attempt_at",
    "delete_on_accept",
}


@dataclass(frozen=True, slots=True)
class CronMigrationInventory:
    source_digest: str
    schedule_id: str
    revision: int
    task_count: int
    occurrence_count: int


@dataclass(frozen=True, slots=True)
class CronMigrationCandidate:
    source_digest: str
    candidate_digest: str
    candidate_path: Path
    snapshot: CronScheduleSnapshot


@dataclass(frozen=True, slots=True)
class CronActivationManifest:
    """Auditable cohort identity for one atomic Cron cutover generation."""

    generation: str
    source_digest: str
    candidate_digest: str
    cohort: tuple[str, ...]
    decision_id: str = "D01-cron-v2-v3-cutover"
    recipe_id: str = "cron-v3-activation-r1"
    source_revision: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "mote.cron-activation/v1",
            "generation": self.generation,
            "source_digest": self.source_digest,
            "candidate_digest": self.candidate_digest,
            "cohort": list(self.cohort),
            "decision_id": self.decision_id,
            "recipe_id": self.recipe_id,
            "source_revision": self.source_revision,
        }


@dataclass(frozen=True, slots=True)
class CronActivationReceipt:
    generation: str
    target_digest: str
    legacy_v2_exited: bool


@dataclass(frozen=True, slots=True)
class CronPrepareReceipt:
    member: str
    generation: str
    source_digest: str
    candidate_digest: str
    source_revision: int


@dataclass(frozen=True, slots=True)
class CronLegacyRetirementReceipt:
    source_digest: str
    decoder_identity: str
    retired: bool


def retire_v2_decoder(
    *, source: Path, source_digest: str, retention_elapsed: bool, receipt_path: Path
) -> CronLegacyRetirementReceipt:
    """Typed, explicit retirement command for the migration-only v2 decoder."""
    if not retention_elapsed:
        raise ValueError("Cron v2 decoder retention window has not elapsed")
    if not source.is_file() or _digest(source.read_bytes()) != source_digest:
        raise ValueError("Cron v2 evidence source preimage is unavailable")
    receipt = CronLegacyRetirementReceipt(source_digest, "mote.cron-schedule/v2-decoder", True)
    atomic_write(
        receipt_path,
        (
            json.dumps(
                {
                    "schema": "mote.cron-retirement/v1",
                    "source_digest": receipt.source_digest,
                    "decoder_identity": receipt.decoder_identity,
                    "retired": receipt.retired,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode(),
    )
    if not receipt_path.is_file():
        raise RuntimeError("Cron decoder retirement receipt is not durable")
    return receipt


_ACTIVATION_COHORT = (
    "cron-writer",
    "cron-reconciler",
    "workflow-consumer",
    "delivery-consumer",
    "cron-legacy-exit",
)


def prepare_activation(candidate: CronMigrationCandidate, generation: str) -> tuple[CronPrepareReceipt, ...]:
    if not generation or any(ch.isspace() for ch in generation):
        raise ValueError("Cron activation generation is invalid")
    return tuple(
        CronPrepareReceipt(
            member,
            generation,
            candidate.source_digest,
            candidate.candidate_digest,
            candidate.snapshot.revision,
        )
        for member in _ACTIVATION_COHORT
    )


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _legacy_identity(value: object) -> str:
    if type(value) is not str or len(value) != 8 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError("legacy Cron task identity is invalid")
    return hashlib.sha256(f"legacy-v2:{value}".encode()).hexdigest()[:32]


def _load_v2(path: Path) -> tuple[bytes, dict[str, object]]:
    data = path.read_bytes()
    try:
        raw = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("legacy Cron source is corrupt") from exc
    if type(raw) is not dict or set(raw) != _ENVELOPE_FIELDS or raw.get("schema") != _V2_SCHEMA:
        raise ValueError("legacy Cron source is not strict v2")
    if type(raw["schedule_id"]) is not str or type(raw["revision"]) is not int or raw["revision"] < 0:
        raise ValueError("legacy Cron header is invalid")
    if type(raw["tasks"]) is not list or type(raw["occurrences"]) is not list:
        raise ValueError("legacy Cron collections are invalid")
    return data, raw


def inventory_v2(path: Path) -> CronMigrationInventory:
    data, raw = _load_v2(path)
    tasks = raw["tasks"]
    occurrences = raw["occurrences"]
    assert isinstance(tasks, list) and isinstance(occurrences, list)
    ids: set[str] = set()
    for task in tasks:
        if type(task) is not dict or set(task) not in (_TASK_FIELDS, _TASK_FIELDS_V3):
            raise ValueError("legacy Cron task fields are invalid")
        identity = _legacy_identity(task["id"])
        if identity in ids:
            raise ValueError("legacy Cron task identity is duplicated")
        ids.add(identity)
    occurrence_ids: set[str] = set()
    for occurrence in occurrences:
        if type(occurrence) is not dict or set(occurrence) != _OCCURRENCE_FIELDS:
            raise ValueError("legacy Cron occurrence fields are invalid")
        identity = occurrence["occurrence_id"]
        if type(identity) is not str or identity in occurrence_ids:
            raise ValueError("legacy Cron occurrence identity is invalid or duplicated")
        occurrence_ids.add(identity)
    schedule_id = raw["schedule_id"]
    revision = raw["revision"]
    if type(schedule_id) is not str or type(revision) is not int:
        raise ValueError("legacy Cron header changed during inventory")
    return CronMigrationInventory(_digest(data), schedule_id, revision, len(tasks), len(occurrences))


def _mapped_occurrence(raw: dict[str, object], task_ids: dict[str, str]) -> dict[str, object]:
    old_task_id = raw["task_id"]
    if type(old_task_id) is not str or old_task_id not in task_ids:
        raise ValueError("legacy Cron occurrence references an unknown task")
    task_id = task_ids[old_task_id]
    scheduled = AbsoluteInstant.from_dict(raw["scheduled_at"])
    scheduled.require_clock(UNIX_UTC_CLOCK)
    if scheduled.epoch_nanoseconds % 1_000_000:
        raise ValueError("legacy Cron scheduled instant precision is invalid")
    task_revision = raw["task_revision"]
    if type(task_revision) is not int or task_revision < 0:
        raise ValueError("legacy Cron occurrence revision is invalid")
    mapped = dict(raw)
    mapped["task_id"] = task_id
    mapped["occurrence_id"] = f"cron:{task_id}:{task_revision}:{scheduled.epoch_nanoseconds // 1_000_000}"
    return mapped


def build_v3_candidate(source: Path, candidate: Path) -> CronMigrationCandidate:
    data, raw = _load_v2(source)
    tasks = raw["tasks"]
    occurrences = raw["occurrences"]
    assert isinstance(tasks, list) and isinstance(occurrences, list)
    task_ids = {task["id"]: _legacy_identity(task["id"]) for task in tasks if isinstance(task, dict)}
    mapped_tasks: list[dict[str, object]] = []
    for task in tasks:
        if type(task) is not dict or set(task) not in (_TASK_FIELDS, _TASK_FIELDS_V3):
            raise ValueError("legacy Cron task fields are invalid")
        mapped = dict(task)
        mapped.setdefault("prompt_artifact_ref", None)
        mapped["id"] = task_ids[task["id"]]
        CronTask.from_dict(mapped)
        mapped_tasks.append(mapped)
    mapped_occurrences = []
    for occurrence in occurrences:
        if type(occurrence) is not dict or set(occurrence) != _OCCURRENCE_FIELDS:
            raise ValueError("legacy Cron occurrence fields are invalid")
        mapped_occurrences.append(_mapped_occurrence(occurrence, task_ids))
    body = dict(raw)
    body["schema"] = _V3_SCHEMA
    body["tasks"] = mapped_tasks
    body["occurrences"] = mapped_occurrences
    encoded = (json.dumps(body, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n").encode()
    atomic_write(candidate, encoded)
    store = CronTaskStore(base_dir=str(source.parent))
    # Decode through the canonical owner's public migration seam.
    snapshot = store.decode_candidate(json.loads(encoded))
    if snapshot.schedule_id != raw["schedule_id"]:
        raise ValueError("Cron candidate schedule identity does not match its target path")
    return CronMigrationCandidate(_digest(data), _digest(encoded), candidate, snapshot)


def activate_candidate(
    candidate: CronMigrationCandidate,
    target: Path,
    evidence_path: Path,
    *,
    expected_source_digest: str,
    activation_manifest_path: Path,
    activation_generation: str,
    prepare_receipts: tuple[CronPrepareReceipt, ...],
) -> CronActivationReceipt:
    if candidate.source_digest != expected_source_digest or not target.is_file():
        raise ValueError("Cron migration source preimage is unavailable")
    source_data = target.read_bytes()
    if _digest(source_data) != expected_source_digest:
        raise ValueError("Cron migration source changed after inventory")
    source_inventory = inventory_v2(target)
    if source_inventory.revision != candidate.snapshot.revision:
        raise ValueError("Cron migration source revision changed after prepare")
    if _digest(candidate.candidate_path.read_bytes()) != candidate.candidate_digest:
        raise ValueError("Cron migration candidate changed after read-back")
    if not activation_generation or any(ch.isspace() for ch in activation_generation):
        raise ValueError("Cron activation generation is invalid")
    expected_prepares = prepare_activation(candidate, activation_generation)
    if prepare_receipts != expected_prepares:
        raise ValueError("Cron activation cohort is incomplete or stale")
    manifest = CronActivationManifest(
        activation_generation,
        expected_source_digest,
        candidate.candidate_digest,
        _ACTIVATION_COHORT,
        source_revision=candidate.snapshot.revision,
    )
    atomic_write(
        activation_manifest_path,
        (json.dumps(manifest.to_dict(), sort_keys=True, separators=(",", ":")) + "\n").encode(),
    )
    if not activation_manifest_path.is_file():
        raise RuntimeError("Cron activation manifest commit is not durable")
    atomic_write(evidence_path, source_data)
    if _digest(evidence_path.read_bytes()) != expected_source_digest:
        raise RuntimeError("Cron migration evidence read-back failed")
    os.replace(candidate.candidate_path, target)
    descriptor = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    if _digest(target.read_bytes()) != candidate.candidate_digest:
        raise RuntimeError("Cron activation target read-back failed")
    return CronActivationReceipt(activation_generation, candidate.candidate_digest, True)


__all__ = [
    "CronActivationManifest",
    "CronActivationReceipt",
    "CronPrepareReceipt",
    "CronMigrationCandidate",
    "CronMigrationInventory",
    "activate_candidate",
    "build_v3_candidate",
    "prepare_activation",
    "CronLegacyRetirementReceipt",
    "retire_v2_decoder",
    "inventory_v2",
]
