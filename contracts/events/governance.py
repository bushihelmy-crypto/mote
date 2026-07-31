"""Typed governance declarations for durable event generations and migrations."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Generic, TypeVar

from mote.contracts.events.envelope import EventType

EventT = TypeVar("EventT")


class CodecState(StrEnum):
    ACTIVE = "active"
    CANDIDATE = "candidate"


class Sensitivity(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    RESTRICTED = "restricted"


class CompactionDisposition(StrEnum):
    RETAIN = "retain"
    SNAPSHOT_REWRITE = "snapshot_rewrite"
    STREAM_DELETE = "stream_delete"


class ArtifactPolicy(StrEnum):
    FORBIDDEN = "forbidden"
    REFERENCES_ONLY = "references_only"


class SemanticAuthority(StrEnum):
    AUTHORITATIVE = "authoritative"
    ADVISORY = "advisory"
    OBSERVATIONAL = "observational"


class PersistenceKind(StrEnum):
    TRANSIENT = "transient"
    JOURNALED = "journaled"
    EXTERNALLY_DURABLE = "externally_durable"


class RepresentationStage(StrEnum):
    RUNTIME_SEMANTIC = "runtime_semantic"
    PERSISTED_DOMAIN = "persisted_domain"
    DURABLE_ENVELOPE = "durable_envelope"
    CURRENT_MODEL = "current_model"
    VIEW = "view"
    WIRE = "wire"


class SideEffectPolicy(StrEnum):
    PURE_REDUCER = "pure_reducer"
    TRANSACTIONAL_PROJECTION = "transactional_projection"
    IDEMPOTENT_EXTERNAL_EFFECT = "idempotent_external_effect"
    OUTBOX_INBOX = "outbox_inbox"


@dataclass(frozen=True, slots=True)
class TransformationDeclaration:
    transformation_id: str
    bounded_domain: str
    source_type: str
    source_stage: RepresentationStage
    target_type: str
    target_stage: RepresentationStage
    authority: SemanticAuthority
    persistence: PersistenceKind
    conversion_owner: str
    converter: str
    side_effect_policy: SideEffectPolicy

    def __post_init__(self) -> None:
        required = (
            self.transformation_id,
            self.bounded_domain,
            self.source_type,
            self.target_type,
            self.conversion_owner,
            self.converter,
        )
        if any(not value for value in required):
            raise ValueError("transformation declarations must be complete")
        if self.source_stage is self.target_stage:
            raise ValueError("a transformation must cross representation stages")


@dataclass(frozen=True, slots=True)
class StoragePolicy:
    sensitivity: Sensitivity
    semantic_inline_size_limit: int
    retention_requirement: str
    redaction_at_source: bool
    compaction_disposition: CompactionDisposition
    legal_hold_behavior: str
    artifact_policy: ArtifactPolicy
    secondary_copy_policy: str

    def __post_init__(self) -> None:
        if self.semantic_inline_size_limit < 1:
            raise ValueError("semantic inline size limit must be positive")
        for value in (
            self.retention_requirement,
            self.legal_hold_behavior,
            self.secondary_copy_policy,
        ):
            if not value:
                raise ValueError("storage policy text fields must be non-empty")


@dataclass(frozen=True, slots=True)
class EventCodecEntry(Generic[EventT]):
    logical_store: str
    event_family: str
    event_type: EventType
    event_schema_version: int
    store_generation: int
    state: CodecState
    owner_id: str
    encoder: Callable[..., object]
    decoder: Callable[..., EventT]
    validator: Callable[[EventT], None]
    policy: StoragePolicy

    def __post_init__(self) -> None:
        if not self.logical_store or not self.event_family or not self.owner_id:
            raise ValueError("codec identity and owner are required")
        if self.event_schema_version < 1 or self.store_generation < 1:
            raise ValueError("codec and store generations must be positive")


class CutoverMode(StrEnum):
    OFFLINE_CUTOVER = "offline_cutover"
    GENERATION_COPY = "generation_copy"
    ARCHIVE_EXPORT = "archive_export"
    DESTROY_ON_EXPIRY = "destroy_on_expiry"


class CutoverState(StrEnum):
    PREPARED = "PREPARED"
    WRITER_FENCED = "WRITER_FENCED"
    QUIESCED = "QUIESCED"
    MIGRATED = "MIGRATED"
    ACTIVATED = "ACTIVATED"
    OBSERVED = "OBSERVED"
    CLEANED = "CLEANED"
    ABORTED_PRE_FENCE = "ABORTED_PRE_FENCE"
    BLOCKED_POST_FENCE = "BLOCKED_POST_FENCE"
    FAILED_VALIDATION = "FAILED_VALIDATION"
    CLEANUP_BLOCKED = "CLEANUP_BLOCKED"


@dataclass(frozen=True, slots=True)
class CutoverDeclaration:
    cutover_unit_id: str
    logical_store: str
    included_event_families: tuple[str, ...]
    source_generation: int
    target_generation: int
    mode: CutoverMode
    shared_sequence_domain: str
    shared_checksum_domain: str
    shared_checkpoint_domain: str
    transaction_boundary: str
    writer_fence: str
    lease_quiesce_policy: str
    activation_record: str
    forward_recovery_owner: str
    cleanup_prerequisite: str
    max_write_unavailable_seconds: float
    drain_deadline_seconds: float

    def __post_init__(self) -> None:
        if not self.cutover_unit_id or not self.logical_store or not self.included_event_families:
            raise ValueError("cutover identity and event families are required")
        if self.source_generation < 1 or self.target_generation <= self.source_generation:
            raise ValueError("cutover target must advance the source generation")
        if self.max_write_unavailable_seconds <= 0 or self.drain_deadline_seconds <= 0:
            raise ValueError("cutover timing bounds must be positive")


@dataclass(frozen=True, slots=True)
class CutoverTransition:
    previous: CutoverState
    next: CutoverState
    expected_activation_generation: int
    cas_revision: int
    actor: str
    owner_id: str
    occurred_at: datetime
    prerequisite_evidence_digests: tuple[str, ...]
    failure_reason: str = ""
    checkpoint_reference: str = ""

    def __post_init__(self) -> None:
        if self.expected_activation_generation < 1 or self.cas_revision < 1:
            raise ValueError("cutover generation and CAS revision must be positive")
        if not self.actor or not self.owner_id:
            raise ValueError("cutover actor and owner are required")
        if self.occurred_at.utcoffset() is None:
            raise ValueError("cutover transition timestamp must be timezone-aware")
        if any(not digest.startswith("sha256:") for digest in self.prerequisite_evidence_digests):
            raise ValueError("cutover evidence digests must be sha256 identities")


class MigrationDebtRole(StrEnum):
    MIGRATION_READER = "migration_reader"
    COMPATIBILITY_FACADE = "compatibility_facade"
    DEPRECATED_API = "deprecated_api"
    TEMPORARY_ALIAS = "temporary_alias"
    ROLLOUT_FLAG = "rollout_flag"
    LEGACY_WRITER = "legacy_writer"
    TEMPORARY_DISCOVERY_PATH = "temporary_discovery_path"
    TEMPORARY_CONSTRUCTION_PATH = "temporary_construction_path"


@dataclass(frozen=True, slots=True)
class MigrationDebtDeclaration:
    debt_id: str
    role: MigrationDebtRole
    logical_store: str
    cutover_unit_id: str
    source_generation: int
    owner_id: str
    last_write_at: datetime
    inventory_snapshot_id: str
    inventory_scope: str
    remaining_record_count: int
    remaining_stream_count: int
    migration_strategy: str
    migration_job: str
    verification_method: str
    deadline: datetime
    deletion_change: str
    deletion_evidence: str
    archive_or_destroy_policy: str
    forward_recovery_boundary: str
    observation_exit_evidence: str

    def __post_init__(self) -> None:
        if self.source_generation < 1 or self.remaining_record_count < 0 or self.remaining_stream_count < 0:
            raise ValueError("migration generation and remaining counts are invalid")
        if self.last_write_at.utcoffset() is None or self.deadline.utcoffset() is None:
            raise ValueError("migration timestamps must be timezone-aware")
        if self.deadline <= self.last_write_at:
            raise ValueError("migration deadline must follow the last legacy write")
        required = (
            self.debt_id,
            self.logical_store,
            self.cutover_unit_id,
            self.owner_id,
            self.inventory_snapshot_id,
            self.inventory_scope,
            self.migration_strategy,
            self.migration_job,
            self.verification_method,
            self.deletion_change,
            self.deletion_evidence,
            self.archive_or_destroy_policy,
            self.forward_recovery_boundary,
            self.observation_exit_evidence,
        )
        if any(not value for value in required):
            raise ValueError("migration debt declarations must be complete")


@dataclass(frozen=True, slots=True)
class RestoreCopyDeclaration:
    copy_id: str
    logical_store: str
    cutover_unit_id: str
    source_generation: int
    storage_format_version: int
    created_at: datetime
    authority_digest: str
    sequence_checkpoint_domain: str
    high_water_mark: str
    retention_policy: str
    legal_hold_policy: str
    destruction_policy: str
    restore_conversion_contract: str

    def __post_init__(self) -> None:
        if self.source_generation < 1 or self.storage_format_version < 1:
            raise ValueError("restore copy generations must be positive")
        if self.created_at.utcoffset() is None:
            raise ValueError("restore copy timestamp must be timezone-aware")
        if not self.authority_digest.startswith("sha256:"):
            raise ValueError("restore copy authority digest must be a sha256 identity")
        required = (
            self.copy_id,
            self.logical_store,
            self.cutover_unit_id,
            self.sequence_checkpoint_domain,
            self.high_water_mark,
            self.retention_policy,
            self.legal_hold_policy,
            self.destruction_policy,
            self.restore_conversion_contract,
        )
        if any(not value for value in required):
            raise ValueError("restore copy declarations must be complete")


@dataclass(frozen=True, slots=True)
class RestoreCopyMetadata:
    logical_store: str
    cutover_unit_id: str
    source_generation: int
    storage_format_version: int
    created_at: datetime
    authority_digest: str
    sequence_checkpoint_domain: str
    high_water_mark: str
    retention_policy: str
    legal_hold_policy: str
    destruction_policy: str
    restore_conversion_contract: str

    def __post_init__(self) -> None:
        RestoreCopyDeclaration(
            copy_id="restore-metadata-validation",
            logical_store=self.logical_store,
            cutover_unit_id=self.cutover_unit_id,
            source_generation=self.source_generation,
            storage_format_version=self.storage_format_version,
            created_at=self.created_at,
            authority_digest=self.authority_digest,
            sequence_checkpoint_domain=self.sequence_checkpoint_domain,
            high_water_mark=self.high_water_mark,
            retention_policy=self.retention_policy,
            legal_hold_policy=self.legal_hold_policy,
            destruction_policy=self.destruction_policy,
            restore_conversion_contract=self.restore_conversion_contract,
        )


class RestoreSourceDisposition(StrEnum):
    RESTORE_CAPABLE = "restore_capable"
    EVIDENCE_ONLY = "evidence_only"
    NOT_PRODUCTION_DATA = "not_production_data"


@dataclass(frozen=True, slots=True)
class RestoreSourceClassification:
    source_id: str
    source_symbol: str
    disposition: RestoreSourceDisposition
    logical_store: str
    admission_contract: str
    metadata_authority: str
    lifecycle_policy: str

    def __post_init__(self) -> None:
        required = (
            self.source_id,
            self.source_symbol,
            self.logical_store,
            self.admission_contract,
            self.metadata_authority,
            self.lifecycle_policy,
        )
        if any(not value for value in required):
            raise ValueError("restore source classifications must be complete")


@dataclass(frozen=True, slots=True)
class ActiveStoreDeclaration:
    cutover_unit_id: str
    logical_store: str
    active_generation: int
    included_event_families: tuple[str, ...]
    storage_format_version: int
    canonical_reader: str
    canonical_writer: str
    activation_authority: str
    restore_admission: str

    def __post_init__(self) -> None:
        if self.active_generation < 1 or self.storage_format_version < 1:
            raise ValueError("active store generations must be positive")
        required = (
            self.cutover_unit_id,
            self.logical_store,
            self.canonical_reader,
            self.canonical_writer,
            self.activation_authority,
            self.restore_admission,
        )
        if any(not value for value in required) or not self.included_event_families:
            raise ValueError("active store declarations must be complete")


@dataclass(frozen=True, slots=True)
class ArchiveCapabilityDeclaration:
    capability_id: str
    online_archive_reader: str
    archival_generation: int | None
    authority: str
    disposition: str

    def __post_init__(self) -> None:
        if not self.capability_id or not self.authority or not self.disposition:
            raise ValueError("archive capability declarations must be complete")
        if self.archival_generation is None and self.online_archive_reader:
            raise ValueError("an absent archive capability cannot expose an archive reader")
        if self.archival_generation is not None and (self.archival_generation < 1 or not self.online_archive_reader):
            raise ValueError("an active archive capability requires a generation and reader")


class WireAuthorityKind(StrEnum):
    CONTRACT_FIRST = "contract_first"
    CODE_FIRST = "code_first"
    EXTERNAL_STANDARD = "external_standard"


@dataclass(frozen=True, slots=True)
class WireAuthorityDeclaration:
    api_id: str
    protocol_generation: int
    authority_kind: WireAuthorityKind
    authority_path: str
    generated_outputs: tuple[str, ...]
    conformance_checker: str
    owner_id: str

    def __post_init__(self) -> None:
        if self.protocol_generation < 1:
            raise ValueError("wire protocol generation must be positive")
        required = (
            self.api_id,
            self.authority_path,
            self.conformance_checker,
            self.owner_id,
        )
        if any(not value for value in required):
            raise ValueError("wire authority declarations must be complete")
        if self.authority_kind is WireAuthorityKind.CONTRACT_FIRST and not self.generated_outputs:
            raise ValueError("contract-first wire authority requires generated outputs")


__all__ = [
    "ArtifactPolicy",
    "ActiveStoreDeclaration",
    "ArchiveCapabilityDeclaration",
    "CodecState",
    "CompactionDisposition",
    "CutoverDeclaration",
    "CutoverMode",
    "CutoverState",
    "CutoverTransition",
    "EventCodecEntry",
    "PersistenceKind",
    "RepresentationStage",
    "SemanticAuthority",
    "SideEffectPolicy",
    "MigrationDebtDeclaration",
    "MigrationDebtRole",
    "RestoreCopyDeclaration",
    "RestoreCopyMetadata",
    "RestoreSourceClassification",
    "RestoreSourceDisposition",
    "Sensitivity",
    "StoragePolicy",
    "TransformationDeclaration",
    "WireAuthorityDeclaration",
    "WireAuthorityKind",
]
