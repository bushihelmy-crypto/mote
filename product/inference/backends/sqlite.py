"""Single-writer SQLite receipt/outbox authority for Embedded and Shared Process."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mote.contracts.clock import UNIX_UTC_CLOCK
from mote.contracts.events.governance import RestoreCopyMetadata
from mote.contracts.inference.epochs import ExecutionEpochSnapshot
from mote.contracts.inference.execution_owner import ExecutionId, ExecutionOwnerRecord
from mote.contracts.inference.generation_artifact import GenerationArtifact, verify_generation_artifact_digest
from mote.contracts.inference.governance import (
    BudgetAdmissionDisposition,
    BudgetAdmissionError,
    BudgetDimension,
    BudgetReservation,
    BudgetReservationRequest,
    BudgetScope,
    BudgetScopeKind,
    ReservationState,
    UsageSettlement,
)
from mote.contracts.inference.persisted_event import PersistedLifecycleEvent
from mote.contracts.inference.provider_evidence import ProviderEvidence, ProviderEvidenceConflictError
from mote.contracts.inference.receipt import (
    TERMINAL_RECEIPT_STATES,
    AttemptReceipt,
    ReceiptState,
    validate_receipt_transition,
)
from mote.contracts.inference.reconciliation import (
    OwnerAcknowledgement,
    OwnerCommand,
    ReconciliationState,
    ResolutionProposal,
)
from mote.contracts.inference.session import (
    TERMINAL_SESSION_STATES,
    SessionReceipt,
    SessionReceiptState,
    validate_session_receipt_transition,
)
from mote.contracts.ports.clock import ClockSource
from mote.runtime.inference.generation import GenerationState
from mote.runtime.inference.reconciliation import ReconciliationRecord, acknowledge_owner_action, require_owner_action
from mote.runtime.persistence.async_io import run_disk_io as _run_disk_io

INFERENCE_GATEWAY_LOGICAL_STORE = "inference-gateway-authority"
INFERENCE_GATEWAY_CUTOVER_UNIT = "inference-gateway-sqlite-v1"
INFERENCE_GATEWAY_STORE_GENERATION = 1
INFERENCE_GATEWAY_STORAGE_FORMAT_VERSION = 1
_BACKUP_METADATA_TABLE = "mote_restore_copy_metadata"


class ReceiptConflictError(BudgetAdmissionError):
    def __init__(
        self,
        message: str,
        disposition: BudgetAdmissionDisposition = BudgetAdmissionDisposition.IDENTITY_CONFLICT,
    ) -> None:
        super().__init__(disposition, message)


class ReceiptFencedError(RuntimeError):
    pass


class SQLiteIntegrityError(RuntimeError):
    pass


class SQLiteBusyError(RuntimeError):
    pass


async def run_disk_io(operation, *args):
    try:
        return await _run_disk_io(operation, *args)
    except sqlite3.OperationalError as exc:
        message = str(exc).lower()
        if "locked" in message or "busy" in message:
            raise SQLiteBusyError("SQLite authority remained busy past deadline") from exc
        raise


@dataclass(frozen=True, slots=True)
class SQLiteStartupReport:
    integrity: str
    free_bytes: int
    database_bytes: int


@dataclass(frozen=True, slots=True)
class OutboxRecord:
    sequence: int
    attempt_id: str
    generation_id: str
    receipt_revision: int
    payload: str


@dataclass(frozen=True, slots=True)
class ProviderEvidenceRecord:
    evidence: ProviderEvidence
    generation_id: str
    digest: str
    received_at: datetime


@dataclass(frozen=True, slots=True)
class ReconciliationOutboxRecord:
    sequence: int
    proposal: ResolutionProposal


class SQLiteAttemptReceiptStore:
    """Durable receipt CAS and transactional outbox in one SQLite authority."""

    def __init__(self, path: Path, *, busy_timeout_seconds: float = 5.0) -> None:
        if busy_timeout_seconds <= 0:
            raise ValueError("busy timeout must be positive")
        self._path = path
        self._busy_timeout_ms = int(busy_timeout_seconds * 1000)
        self._failed_startup_image: bytes | None = None

    async def initialize(self) -> None:
        await run_disk_io(self._initialize)

    async def verify_startup(self, *, hard_min_free_bytes: int) -> SQLiteStartupReport:
        if hard_min_free_bytes < 0:
            raise ValueError("hard disk watermark cannot be negative")
        return await run_disk_io(self._verify_startup, hard_min_free_bytes)

    async def backup_to(self, destination: Path) -> None:
        await run_disk_io(self._backup_to, destination)

    async def verify_backup(self, source: Path) -> str:
        return await run_disk_io(self._verify_backup, source)

    async def describe_backup(self, source: Path) -> RestoreCopyMetadata:
        return await run_disk_io(self._describe_backup, source)

    async def restore_from(self, source: Path) -> RestoreCopyMetadata:
        return await run_disk_io(self._restore_from, source)

    async def preserve_corrupt_copy(self) -> Path:
        return await run_disk_io(self._preserve_corrupt_copy)

    async def reconcile_incomplete(self) -> tuple[int, int]:
        return await run_disk_io(self._reconcile_incomplete)

    async def stage_generation(self, artifact: GenerationArtifact) -> None:
        await run_disk_io(self._stage_generation, artifact)

    async def activate_generation(self, generation_id: str, artifact_digest: str) -> None:
        await run_disk_io(self._activate_generation, generation_id, artifact_digest)

    async def load_generations(
        self,
    ) -> tuple[tuple[GenerationArtifact, GenerationState], ...]:
        return await run_disk_io(self._load_generations)

    async def execution_epoch_snapshot(self):
        return await run_disk_io(self._execution_epoch_snapshot)

    async def advance_backup_epoch(self):
        return await run_disk_io(self._advance_backup_epoch)

    async def append_event(self, event: PersistedLifecycleEvent) -> PersistedLifecycleEvent:
        return await run_disk_io(self._append_event, event)

    async def read_events(
        self, execution_id: str, *, after_sequence: int, limit: int = 256
    ) -> tuple[PersistedLifecycleEvent, ...]:
        if not execution_id or after_sequence < 0 or limit <= 0:
            raise ValueError("invalid lifecycle event cursor")
        return await run_disk_io(self._read_events, execution_id, after_sequence, limit)

    async def put_owner_record(self, record: ExecutionOwnerRecord) -> ExecutionOwnerRecord:
        return await run_disk_io(self._put_owner_record, record)

    async def get_owner_record(self, execution_id: ExecutionId) -> ExecutionOwnerRecord | None:
        return await run_disk_io(self._get_owner_record, execution_id)

    async def get(self, attempt_id: str, generation_id: str) -> AttemptReceipt | None:
        return await run_disk_io(self._get, attempt_id, generation_id)

    async def list_receipts(self, *, state: ReceiptState | None = None, limit: int = 100) -> tuple[AttemptReceipt, ...]:
        if limit <= 0 or limit > 1000:
            raise ValueError("receipt projection limit is invalid")
        return await run_disk_io(self._list_receipts, state, limit)

    async def accept(self, receipt: AttemptReceipt) -> AttemptReceipt:
        return await run_disk_io(self._accept, receipt)

    async def compare_and_swap(
        self,
        receipt: AttemptReceipt,
        *,
        expected_revision: int,
        fencing_token: int,
    ) -> AttemptReceipt:
        return await run_disk_io(self._compare_and_swap, receipt, expected_revision, fencing_token)

    async def read_outbox(self, *, after_sequence: int = 0, limit: int = 100) -> tuple[OutboxRecord, ...]:
        if after_sequence < 0 or limit <= 0:
            raise ValueError("invalid outbox cursor or limit")
        return await run_disk_io(self._read_outbox, after_sequence, limit)

    async def mark_published(self, sequence: int) -> None:
        if sequence <= 0:
            raise ValueError("outbox sequence must be positive")
        await run_disk_io(self._mark_published, sequence)

    def _initialize(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self._path.parent, 0o700)
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS attempt_receipts (
                    attempt_id TEXT NOT NULL,
                    generation_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    fencing_token INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    permit_digest TEXT,
                    request_digest TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (attempt_id, generation_id),
                    UNIQUE (permit_digest)
                );
                CREATE TABLE IF NOT EXISTS receipt_outbox (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    attempt_id TEXT NOT NULL,
                    generation_id TEXT NOT NULL,
                    receipt_revision INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    published INTEGER NOT NULL DEFAULT 0,
                    UNIQUE (attempt_id, generation_id, receipt_revision)
                );
                CREATE TABLE IF NOT EXISTS usage_budgets (
                    tenant_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    limit_units INTEGER NOT NULL,
                    settled_units INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (tenant_id, project_id)
                );
                CREATE TABLE IF NOT EXISTS usage_reservations (
                    reservation_id TEXT PRIMARY KEY,
                    attempt_id TEXT NOT NULL UNIQUE,
                    tenant_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    units INTEGER NOT NULL,
                    fencing_token INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS usage_settlements (
                    settlement_id TEXT PRIMARY KEY,
                    reservation_id TEXT NOT NULL,
                    attempt_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    FOREIGN KEY (reservation_id) REFERENCES usage_reservations(reservation_id)
                );
                CREATE TABLE IF NOT EXISTS usage_reservation_scopes (
                    reservation_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    units INTEGER NOT NULL,
                    PRIMARY KEY (reservation_id, tenant_id, project_id),
                    FOREIGN KEY (reservation_id) REFERENCES usage_reservations(reservation_id)
                );
                CREATE TABLE IF NOT EXISTS session_receipts (
                    session_id TEXT NOT NULL,
                    generation_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    fencing_token INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (session_id, generation_id)
                );
                CREATE TABLE IF NOT EXISTS session_outbox (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    generation_id TEXT NOT NULL,
                    receipt_revision INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    published INTEGER NOT NULL DEFAULT 0,
                    UNIQUE (session_id, generation_id, receipt_revision)
                );
                CREATE TABLE IF NOT EXISTS lifecycle_events (
                    execution_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    receipt_revision INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    terminal INTEGER NOT NULL,
                    payload BLOB NOT NULL,
                    contract TEXT NOT NULL,
                    PRIMARY KEY (execution_id, sequence)
                );
                CREATE TABLE IF NOT EXISTS execution_owner_records (
                    execution_id TEXT PRIMARY KEY,
                    record TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reconciliation_bindings (
                    execution_id TEXT PRIMARY KEY,
                    generation_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    strategy_id TEXT NOT NULL,
                    UNIQUE (execution_id, generation_id)
                );
                CREATE TABLE IF NOT EXISTS provider_evidence (
                    provider TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    execution_id TEXT NOT NULL,
                    generation_id TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (provider, event_id),
                    UNIQUE (digest),
                    FOREIGN KEY (execution_id)
                        REFERENCES reconciliation_bindings(execution_id)
                );
                CREATE TABLE IF NOT EXISTS reconciliation_records (
                    proposal_id TEXT PRIMARY KEY,
                    execution_id TEXT NOT NULL UNIQUE,
                    generation_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    proposal TEXT NOT NULL,
                    acknowledgement TEXT,
                    FOREIGN KEY (execution_id)
                        REFERENCES reconciliation_bindings(execution_id)
                );
                CREATE TABLE IF NOT EXISTS reconciliation_outbox (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    proposal_id TEXT NOT NULL UNIQUE,
                    payload TEXT NOT NULL,
                    published INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY (proposal_id)
                        REFERENCES reconciliation_records(proposal_id)
                );
                CREATE TABLE IF NOT EXISTS owner_command_outbox (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    command_id TEXT NOT NULL UNIQUE,
                    owner_id TEXT NOT NULL,
                    proposal_id TEXT NOT NULL UNIQUE,
                    payload TEXT NOT NULL,
                    published INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY (proposal_id)
                        REFERENCES reconciliation_records(proposal_id)
                );
                CREATE TABLE IF NOT EXISTS gateway_generations (
                    generation_id TEXT PRIMARY KEY,
                    artifact_digest TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    artifact TEXT NOT NULL,
                    activation_sequence INTEGER
                );
                CREATE TABLE IF NOT EXISTS execution_epochs (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    backup_epoch INTEGER NOT NULL CHECK (backup_epoch >= 1),
                    admission_epoch INTEGER NOT NULL CHECK (admission_epoch >= 1)
                );
                INSERT OR IGNORE INTO execution_epochs
                    (singleton, backup_epoch, admission_epoch) VALUES (1, 1, 1);
                """)
            self._validate_usage_budget_schema(connection)
        os.chmod(self._path, 0o600)

    @staticmethod
    def _validate_usage_budget_schema(connection: sqlite3.Connection) -> None:
        """Fail closed unless every usage record uses the sole current schema."""
        rows = connection.execute(
            "SELECT reservation_id, tenant_id, project_id, units, payload FROM usage_reservations"
        ).fetchall()
        for reservation_id, tenant_id, project_id, units, payload in rows:
            raw = json.loads(payload)
            if raw.get("schema_version") != 2:
                raise SQLiteIntegrityError("usage reservation schema is unknown")
            scope_count = connection.execute(
                "SELECT COUNT(*) FROM usage_reservation_scopes WHERE reservation_id = ?",
                (reservation_id,),
            ).fetchone()[0]
            if scope_count < 1:
                raise SQLiteIntegrityError("usage reservation scopes are unavailable")
        settlements = connection.execute("SELECT settlement_id, payload FROM usage_settlements").fetchall()
        for settlement_id, payload in settlements:
            raw = json.loads(payload)
            if raw.get("schema_version") != 2:
                raise SQLiteIntegrityError("usage settlement schema is unknown")

    def _verify_startup(self, hard_min_free_bytes: int) -> SQLiteStartupReport:
        if not self._path.is_file():
            raise SQLiteIntegrityError("SQLite authority does not exist")
        free_bytes = shutil.disk_usage(self._path.parent).free
        if free_bytes < hard_min_free_bytes:
            raise SQLiteIntegrityError(f"SQLite disk hard watermark reached: {free_bytes} bytes free")
        startup_image = self._path.read_bytes()
        try:
            with self._connect() as connection:
                row = connection.execute("PRAGMA quick_check").fetchone()
        except sqlite3.DatabaseError as exc:
            self._failed_startup_image = startup_image
            raise SQLiteIntegrityError("SQLite quick_check could not run") from exc
        integrity = str(row[0]) if row else "missing-result"
        if integrity != "ok":
            self._failed_startup_image = startup_image
            raise SQLiteIntegrityError(f"SQLite quick_check failed: {integrity}")
        self._failed_startup_image = None
        return SQLiteStartupReport(
            integrity=integrity,
            free_bytes=free_bytes,
            database_bytes=self._path.stat().st_size,
        )

    def _backup_to(self, destination: Path) -> None:
        if destination == self._path:
            raise ValueError("SQLite backup destination must differ from authority")
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            with self._connect() as source, sqlite3.connect(temporary) as target:
                source.backup(target)
                result = target.execute("PRAGMA quick_check").fetchone()
                if result is None or result[0] != "ok":
                    raise SQLiteIntegrityError("SQLite backup verification failed")
            metadata = self._metadata_for_verified_copy(temporary, created_at=datetime.now(timezone.utc))
            with sqlite3.connect(temporary) as target:
                target.execute(
                    f"CREATE TABLE {_BACKUP_METADATA_TABLE} ("
                    "singleton INTEGER PRIMARY KEY CHECK (singleton = 1), "
                    "payload TEXT NOT NULL)"
                )
                target.execute(
                    f"INSERT INTO {_BACKUP_METADATA_TABLE} (singleton, payload) " "VALUES (1, ?)",
                    (json.dumps(self._metadata_to_json(metadata), sort_keys=True),),
                )
                target.commit()
                result = target.execute("PRAGMA quick_check").fetchone()
                if result is None or result[0] != "ok":
                    raise SQLiteIntegrityError("SQLite backup metadata verification failed")
            with sqlite3.connect(temporary) as target:
                target.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            os.chmod(temporary, 0o600)
            os.replace(temporary, destination)
            self._fsync_directory(destination.parent)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def _restore_from(self, source: Path) -> RestoreCopyMetadata:
        if source == self._path:
            raise ValueError("SQLite restore source is invalid")
        metadata = self._describe_backup(source)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{self._path.name}.restore.", dir=self._path.parent)
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            with (
                sqlite3.connect(source) as backup,
                sqlite3.connect(temporary) as target,
            ):
                backup.backup(target)
                target.execute(f"DROP TABLE {_BACKUP_METADATA_TABLE}")
                target.commit()
                target.execute("PRAGMA journal_mode=DELETE")
                verified = target.execute("PRAGMA quick_check").fetchone()
                if verified is None or verified[0] != "ok":
                    raise SQLiteIntegrityError("restored SQLite copy failed verification")
            for suffix in ("-wal", "-shm"):
                self._path.with_name(self._path.name + suffix).unlink(missing_ok=True)
            os.chmod(temporary, 0o600)
            os.replace(temporary, self._path)
            self._fsync_directory(self._path.parent)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        return metadata

    @staticmethod
    def _verify_backup(source: Path) -> str:
        if not source.is_file():
            raise ValueError("SQLite backup source does not exist")
        try:
            with sqlite3.connect(f"file:{source}?mode=ro&immutable=1", uri=True) as candidate:
                result = candidate.execute("PRAGMA quick_check").fetchone()
                digest = SQLiteAttemptReceiptStore._logical_authority_digest(candidate)
        except sqlite3.DatabaseError as exc:
            raise SQLiteIntegrityError("SQLite backup verification could not run") from exc
        if result is None or result[0] != "ok":
            raise SQLiteIntegrityError("SQLite backup failed verification")
        return digest

    @staticmethod
    def _logical_authority_digest(connection: sqlite3.Connection) -> str:
        """Digest canonical SQLite content, excluding transport metadata."""

        digest = hashlib.sha256()
        objects = connection.execute(
            "SELECT type, name, sql FROM sqlite_master " "WHERE name != ? AND sql IS NOT NULL ORDER BY type, name",
            (_BACKUP_METADATA_TABLE,),
        ).fetchall()
        for object_type, name, sql in objects:
            digest.update(
                json.dumps(
                    [object_type, name, sql],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            digest.update(b"\0")
            if object_type != "table" or name.startswith("sqlite_"):
                continue
            quoted = '"' + name.replace('"', '""') + '"'
            rows = [
                json.dumps(
                    [SQLiteAttemptReceiptStore._canonical_sql_value(value) for value in row],
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                for row in connection.execute(f"SELECT * FROM {quoted}").fetchall()
            ]
            for row in sorted(rows):
                digest.update(row.encode("utf-8"))
                digest.update(b"\0")
        sequence_exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sqlite_sequence'"
        ).fetchone()
        if sequence_exists:
            for name, sequence in connection.execute("SELECT name, seq FROM sqlite_sequence ORDER BY name").fetchall():
                digest.update(f"sqlite_sequence:{name}:{sequence}\0".encode())
        return "sha256:" + digest.hexdigest()

    @staticmethod
    def _canonical_sql_value(value: object) -> object:
        if isinstance(value, bytes):
            return {"bytes": base64.b64encode(value).decode("ascii")}
        if value is None or type(value) in {str, int, float}:
            return value
        raise TypeError(f"unsupported SQLite authority value: {type(value).__name__}")

    @classmethod
    def _describe_backup(cls, source: Path) -> RestoreCopyMetadata:
        digest = cls._verify_backup(source)
        try:
            with sqlite3.connect(f"file:{source}?mode=ro&immutable=1", uri=True) as candidate:
                row = candidate.execute(f"SELECT payload FROM {_BACKUP_METADATA_TABLE} WHERE singleton = 1").fetchone()
            if row is None:
                raise ValueError("missing restore metadata row")
            payload = json.loads(row[0])
            metadata = cls._metadata_from_json(payload)
        except (sqlite3.DatabaseError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise SQLiteIntegrityError("SQLite restore copy metadata is invalid") from exc
        if metadata.authority_digest != digest:
            raise SQLiteIntegrityError("SQLite restore copy metadata digest does not match authority")
        observed = cls._metadata_for_verified_copy(source, created_at=metadata.created_at)
        if observed != metadata:
            raise SQLiteIntegrityError("SQLite restore copy metadata does not match contained state")
        return metadata

    @classmethod
    def _metadata_for_verified_copy(cls, source: Path, *, created_at: datetime) -> RestoreCopyMetadata:
        digest = cls._verify_backup(source)
        with sqlite3.connect(f"file:{source}?mode=ro&immutable=1", uri=True) as candidate:
            generations = candidate.execute(
                "SELECT generation_id, artifact_digest FROM gateway_generations " "WHERE state = ?",
                (GenerationState.ACTIVE.value,),
            ).fetchall()
            if len(generations) != 1:
                raise SQLiteIntegrityError("restore copy must contain exactly one active generation")
            high_water = candidate.execute(
                "SELECT "
                "COALESCE((SELECT MAX(sequence) FROM lifecycle_events), 0), "
                "COALESCE((SELECT MAX(sequence) FROM receipt_outbox), 0), "
                "COALESCE((SELECT MAX(sequence) FROM session_outbox), 0)"
            ).fetchone()
        generation_id, artifact_digest = generations[0]
        return RestoreCopyMetadata(
            logical_store=INFERENCE_GATEWAY_LOGICAL_STORE,
            cutover_unit_id=INFERENCE_GATEWAY_CUTOVER_UNIT,
            source_generation=INFERENCE_GATEWAY_STORE_GENERATION,
            storage_format_version=INFERENCE_GATEWAY_STORAGE_FORMAT_VERSION,
            created_at=created_at,
            authority_digest=digest,
            sequence_checkpoint_domain="gateway-sqlite-transaction",
            high_water_mark=(
                f"active={generation_id}@{artifact_digest};"
                f"lifecycle={high_water[0]};receipt_outbox={high_water[1]};session_outbox={high_water[2]}"
            ),
            retention_policy="operator-managed crash-consistent backup retention",
            legal_hold_policy="operator legal hold prevents destruction, not format admission",
            destruction_policy="securely delete when retention and legal hold permit",
            restore_conversion_contract="inference-gateway-sqlite-v1-exact",
        )

    @staticmethod
    def _metadata_to_json(metadata: RestoreCopyMetadata) -> dict[str, object]:
        return {
            "schema": "inference-gateway-restore-copy-v1",
            "logical_store": metadata.logical_store,
            "cutover_unit_id": metadata.cutover_unit_id,
            "source_generation": metadata.source_generation,
            "storage_format_version": metadata.storage_format_version,
            "created_at": metadata.created_at.isoformat(),
            "authority_digest": metadata.authority_digest,
            "sequence_checkpoint_domain": metadata.sequence_checkpoint_domain,
            "high_water_mark": metadata.high_water_mark,
            "retention_policy": metadata.retention_policy,
            "legal_hold_policy": metadata.legal_hold_policy,
            "destruction_policy": metadata.destruction_policy,
            "restore_conversion_contract": metadata.restore_conversion_contract,
        }

    @staticmethod
    def _metadata_from_json(payload: object) -> RestoreCopyMetadata:
        if not isinstance(payload, dict) or payload.get("schema") != ("inference-gateway-restore-copy-v1"):
            raise ValueError("unsupported restore metadata schema")
        return RestoreCopyMetadata(
            logical_store=str(payload["logical_store"]),
            cutover_unit_id=str(payload["cutover_unit_id"]),
            source_generation=int(payload["source_generation"]),
            storage_format_version=int(payload["storage_format_version"]),
            created_at=datetime.fromisoformat(str(payload["created_at"])),
            authority_digest=str(payload["authority_digest"]),
            sequence_checkpoint_domain=str(payload["sequence_checkpoint_domain"]),
            high_water_mark=str(payload["high_water_mark"]),
            retention_policy=str(payload["retention_policy"]),
            legal_hold_policy=str(payload["legal_hold_policy"]),
            destruction_policy=str(payload["destruction_policy"]),
            restore_conversion_contract=str(payload["restore_conversion_contract"]),
        )

    def _preserve_corrupt_copy(self) -> Path:
        if not self._path.is_file():
            raise SQLiteIntegrityError("SQLite authority does not exist")
        # WAL pages are part of the authority's durable state.  Checkpoint a
        # healthy database before taking the evidence image so the copied
        # main file contains the same state as the live authority (and is not
        # merely a stale pre-WAL snapshot).  A corrupt database cannot be
        # opened, so preserve its bytes verbatim below.
        original = self._failed_startup_image or self._path.read_bytes()
        try:
            with sqlite3.connect(f"file:{self._path}?mode=ro&immutable=1", uri=True) as probe:
                result = probe.execute("PRAGMA quick_check").fetchone()
            healthy = result is not None and result[0] == "ok"
        except sqlite3.DatabaseError:
            healthy = False
        if healthy and self._failed_startup_image is None:
            with self._connect() as connection:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        destination = self._path.with_name(f"{self._path.name}.corrupt-{timestamp}")
        if healthy and self._failed_startup_image is None:
            shutil.copy2(self._path, destination)
        else:
            destination.write_bytes(original)
        os.chmod(destination, 0o600)
        self._fsync_directory(destination.parent)
        return destination

    def _reconcile_incomplete(self) -> tuple[int, int]:
        attempt_count = 0
        session_count = 0
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            attempts = connection.execute("SELECT payload FROM attempt_receipts").fetchall()
            for row in attempts:
                receipt = AttemptReceipt.model_validate_json(row[0])
                if receipt.state in TERMINAL_RECEIPT_STATES or receipt.state in {
                    ReceiptState.ACCEPTED,
                    ReceiptState.SEND_INTENT_DURABLE,
                }:
                    continue
                reconciled = receipt.model_copy(
                    update={
                        "revision": receipt.revision + 1,
                        "state": ReceiptState.IN_DOUBT,
                        "updated_at": datetime.now(timezone.utc),
                    }
                )
                validate_receipt_transition(receipt, reconciled)
                payload = reconciled.model_dump_json()
                connection.execute(
                    "UPDATE attempt_receipts SET revision = ?, state = ?, payload = ? "
                    "WHERE attempt_id = ? AND generation_id = ? AND revision = ?",
                    (
                        reconciled.revision,
                        reconciled.state.value,
                        payload,
                        reconciled.attempt_id,
                        reconciled.generation_id,
                        receipt.revision,
                    ),
                )
                self._append_outbox(connection, reconciled, payload)
                attempt_count += 1
            sessions = connection.execute("SELECT payload FROM session_receipts").fetchall()
            for row in sessions:
                receipt = SessionReceipt.model_validate_json(row[0])
                if receipt.state in TERMINAL_SESSION_STATES or receipt.state is SessionReceiptState.ACCEPTED:
                    continue
                reconciled = receipt.model_copy(
                    update={
                        "revision": receipt.revision + 1,
                        "state": SessionReceiptState.IN_DOUBT,
                        "updated_at": datetime.now(timezone.utc),
                    }
                )
                validate_session_receipt_transition(receipt, reconciled)
                payload = reconciled.model_dump_json()
                connection.execute(
                    "UPDATE session_receipts SET revision = ?, state = ?, payload = ? "
                    "WHERE session_id = ? AND generation_id = ? AND revision = ?",
                    (
                        reconciled.revision,
                        reconciled.state.value,
                        payload,
                        reconciled.session_id,
                        reconciled.generation_id,
                        receipt.revision,
                    ),
                )
                SQLiteSessionReceiptStore._append_outbox(connection, reconciled, payload)
                session_count += 1
            connection.commit()
        return attempt_count, session_count

    def _stage_generation(self, artifact: GenerationArtifact) -> None:
        verify_generation_artifact_digest(artifact)
        payload = artifact.model_dump_json()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT artifact_digest, artifact FROM gateway_generations " "WHERE generation_id = ?",
                (artifact.generation_id,),
            ).fetchone()
            if row is not None:
                if row != (artifact.artifact_digest, payload):
                    raise ReceiptConflictError("generation identity reused with different artifact")
                connection.commit()
                return
            connection.execute(
                "INSERT INTO gateway_generations "
                "(generation_id, artifact_digest, state, artifact) "
                "VALUES (?, ?, ?, ?)",
                (
                    artifact.generation_id,
                    artifact.artifact_digest,
                    GenerationState.STAGED.value,
                    payload,
                ),
            )
            connection.commit()

    def _activate_generation(self, generation_id: str, artifact_digest: str) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT artifact_digest, state FROM gateway_generations " "WHERE generation_id = ?",
                (generation_id,),
            ).fetchone()
            if row is None:
                raise ReceiptConflictError("unknown durable generation")
            if row[0] != artifact_digest:
                raise ReceiptConflictError("generation artifact digest mismatch")
            if row[1] != GenerationState.STAGED.value:
                raise ReceiptConflictError(f"generation cannot activate from {row[1]}")
            sequence = connection.execute(
                "SELECT COALESCE(MAX(activation_sequence), 0) + 1 " "FROM gateway_generations"
            ).fetchone()[0]
            connection.execute(
                "UPDATE gateway_generations SET state = ? " "WHERE state = ? AND generation_id != ?",
                (
                    GenerationState.DRAINING.value,
                    GenerationState.ACTIVE.value,
                    generation_id,
                ),
            )
            connection.execute(
                "UPDATE gateway_generations SET state = ?, activation_sequence = ? " "WHERE generation_id = ?",
                (GenerationState.ACTIVE.value, sequence, generation_id),
            )
            connection.execute(
                "UPDATE execution_epochs SET admission_epoch = admission_epoch + 1 " "WHERE singleton = 1"
            )
            connection.commit()

    def _execution_epoch_snapshot(self):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT backup_epoch, admission_epoch FROM execution_epochs WHERE singleton = 1"
            ).fetchone()
        if row is None:
            raise SQLiteIntegrityError("execution epoch authority is missing")
        return ExecutionEpochSnapshot(int(row[0]), int(row[1]))

    def _advance_backup_epoch(self):
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("UPDATE execution_epochs SET backup_epoch = backup_epoch + 1 " "WHERE singleton = 1")
            row = connection.execute(
                "SELECT backup_epoch, admission_epoch FROM execution_epochs WHERE singleton = 1"
            ).fetchone()
            connection.commit()
        if row is None:
            raise SQLiteIntegrityError("execution epoch authority is missing")
        return ExecutionEpochSnapshot(int(row[0]), int(row[1]))

    def _load_generations(
        self,
    ) -> tuple[tuple[GenerationArtifact, GenerationState], ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT artifact, state FROM gateway_generations " "ORDER BY COALESCE(activation_sequence, 0), rowid"
            ).fetchall()
        return tuple((GenerationArtifact.model_validate_json(row[0]), GenerationState(row[1])) for row in rows)

    def _append_event(self, event: PersistedLifecycleEvent) -> PersistedLifecycleEvent:
        contract = event.model_dump_json()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT contract FROM lifecycle_events " "WHERE execution_id = ? AND sequence = ?",
                (event.execution_id, event.sequence),
            ).fetchone()
            if row is not None:
                existing = PersistedLifecycleEvent.model_validate_json(row[0])
                if existing != event:
                    raise ReceiptConflictError("lifecycle sequence reused with different event")
                connection.commit()
                return existing
            previous = connection.execute(
                "SELECT sequence, terminal FROM lifecycle_events "
                "WHERE execution_id = ? ORDER BY sequence DESC LIMIT 1",
                (event.execution_id,),
            ).fetchone()
            if previous is not None:
                if previous[1]:
                    raise ReceiptConflictError("lifecycle event follows terminal")
                if event.sequence != previous[0] + 1:
                    raise ReceiptConflictError("lifecycle event sequence has a gap")
            elif event.sequence != 1:
                raise ReceiptConflictError("first lifecycle event sequence must be one")
            connection.execute(
                "INSERT INTO lifecycle_events VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    event.execution_id,
                    event.sequence,
                    event.receipt_revision,
                    event.event_type,
                    int(event.terminal),
                    event.payload,
                    contract,
                ),
            )
            connection.commit()
        return event

    def _read_events(self, execution_id: str, after_sequence: int, limit: int) -> tuple[PersistedLifecycleEvent, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT contract FROM lifecycle_events "
                "WHERE execution_id = ? AND sequence > ? "
                "ORDER BY sequence LIMIT ?",
                (execution_id, after_sequence, limit),
            ).fetchall()
        return tuple(PersistedLifecycleEvent.model_validate_json(row[0]) for row in rows)

    def _put_owner_record(self, record: ExecutionOwnerRecord) -> ExecutionOwnerRecord:
        payload = record.model_dump_json()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT record FROM execution_owner_records WHERE execution_id = ?",
                (record.execution_id,),
            ).fetchone()
            if row is not None:
                existing = ExecutionOwnerRecord.model_validate_json(row[0])
                if existing != record:
                    raise ReceiptConflictError("execution owner identity conflicts with existing record")
                connection.commit()
                return existing
            connection.execute(
                "INSERT INTO execution_owner_records (execution_id, record) VALUES (?, ?)",
                (record.execution_id, payload),
            )
            connection.commit()
        return record

    def _get_owner_record(self, execution_id: ExecutionId) -> ExecutionOwnerRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT record FROM execution_owner_records WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
        return ExecutionOwnerRecord.model_validate_json(row[0]) if row is not None else None

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _get(self, attempt_id: str, generation_id: str) -> AttemptReceipt | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM attempt_receipts WHERE attempt_id = ? AND generation_id = ?",
                (attempt_id, generation_id),
            ).fetchone()
        return AttemptReceipt.model_validate_json(row[0]) if row else None

    def _list_receipts(self, state: ReceiptState | None, limit: int) -> tuple[AttemptReceipt, ...]:
        query = "SELECT payload FROM attempt_receipts"
        parameters: tuple[object, ...]
        if state is None:
            parameters = (limit,)
        else:
            query += " WHERE state = ?"
            parameters = (state.value, limit)
        query += " ORDER BY rowid DESC LIMIT ?"
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return tuple(AttemptReceipt.model_validate_json(row[0]) for row in rows)

    def _accept(self, receipt: AttemptReceipt) -> AttemptReceipt:
        payload = receipt.model_dump_json()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload FROM attempt_receipts WHERE attempt_id = ? AND generation_id = ?",
                (receipt.attempt_id, receipt.generation_id),
            ).fetchone()
            if row:
                existing = AttemptReceipt.model_validate_json(row[0])
                if existing.request_digest != receipt.request_digest:
                    raise ReceiptConflictError("receipt identity reused with different request digest")
                connection.commit()
                return existing
            connection.execute(
                "INSERT INTO attempt_receipts VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    receipt.attempt_id,
                    receipt.generation_id,
                    receipt.revision,
                    receipt.fencing_token,
                    receipt.state.value,
                    receipt.permit_digest,
                    receipt.request_digest,
                    payload,
                ),
            )
            self._append_outbox(connection, receipt, payload)
            connection.commit()
            return receipt

    def _compare_and_swap(
        self,
        receipt: AttemptReceipt,
        expected_revision: int,
        fencing_token: int,
    ) -> AttemptReceipt:
        payload = receipt.model_dump_json()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload, revision, fencing_token FROM attempt_receipts "
                "WHERE attempt_id = ? AND generation_id = ?",
                (receipt.attempt_id, receipt.generation_id),
            ).fetchone()
            if row is None:
                raise ReceiptConflictError("receipt does not exist")
            current = AttemptReceipt.model_validate_json(row[0])
            if row[1] != expected_revision:
                raise ReceiptConflictError(f"receipt expected revision {expected_revision}, actual {row[1]}")
            if fencing_token < row[2] or receipt.fencing_token != fencing_token:
                raise ReceiptFencedError("receipt fencing token is stale or mismatched")
            validate_receipt_transition(current, receipt)
            updated = connection.execute(
                "UPDATE attempt_receipts SET revision = ?, fencing_token = ?, state = ?, "
                "permit_digest = ?, request_digest = ?, payload = ? "
                "WHERE attempt_id = ? AND generation_id = ? AND revision = ? AND fencing_token <= ?",
                (
                    receipt.revision,
                    receipt.fencing_token,
                    receipt.state.value,
                    receipt.permit_digest,
                    receipt.request_digest,
                    payload,
                    receipt.attempt_id,
                    receipt.generation_id,
                    expected_revision,
                    fencing_token,
                ),
            )
            if updated.rowcount != 1:
                raise ReceiptConflictError("receipt compare-and-swap lost race")
            self._append_outbox(connection, receipt, payload)
            connection.commit()
            return receipt

    def _read_outbox(self, after_sequence: int, limit: int) -> tuple[OutboxRecord, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT sequence, attempt_id, generation_id, receipt_revision, payload "
                "FROM receipt_outbox WHERE published = 0 AND sequence > ? ORDER BY sequence LIMIT ?",
                (after_sequence, limit),
            ).fetchall()
        return tuple(OutboxRecord(*row) for row in rows)

    def _mark_published(self, sequence: int) -> None:
        with self._connect() as connection:
            updated = connection.execute(
                "UPDATE receipt_outbox SET published = 1 WHERE sequence = ?",
                (sequence,),
            )
            if updated.rowcount != 1:
                raise ReceiptConflictError("unknown or already removed outbox sequence")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=self._busy_timeout_ms / 1000, isolation_level=None)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
        return connection

    @staticmethod
    def _append_outbox(connection: sqlite3.Connection, receipt: AttemptReceipt, payload: str) -> None:
        connection.execute(
            "INSERT INTO receipt_outbox " "(attempt_id, generation_id, receipt_revision, payload) VALUES (?, ?, ?, ?)",
            (receipt.attempt_id, receipt.generation_id, receipt.revision, payload),
        )


class SQLiteReconciliationAuthority:
    """Immutable evidence and owner-controlled reconciliation in one authority."""

    def __init__(self, authority: SQLiteAttemptReceiptStore) -> None:
        self._authority = authority

    async def bind_execution(
        self,
        *,
        execution_id: str,
        generation_id: str,
        provider: str,
        owner_id: str,
        strategy_id: str,
    ) -> None:
        values = (execution_id, generation_id, provider, owner_id, strategy_id)
        if any(not value for value in values):
            raise ValueError("reconciliation binding fields must be non-empty")
        await run_disk_io(self._bind_execution, *values)

    async def append(self, evidence: ProviderEvidence) -> bool:
        return await run_disk_io(self._append, evidence)

    async def provider_for(self, execution_id: str, generation_id: str) -> str:
        if not execution_id or not generation_id:
            raise ValueError("reconciliation execution identity is required")
        return await run_disk_io(self._provider_for, execution_id, generation_id)

    async def list_evidence(self, execution_id: str, *, limit: int = 100) -> tuple[ProviderEvidenceRecord, ...]:
        if not execution_id or limit <= 0 or limit > 1000:
            raise ValueError("invalid provider evidence query")
        return await run_disk_io(self._list_evidence, execution_id, limit)

    async def propose(self, execution_id: str) -> ReconciliationRecord:
        if not execution_id:
            raise ValueError("execution identity is required")
        return await run_disk_io(self._propose, execution_id)

    async def get(self, execution_id: str) -> ReconciliationRecord | None:
        if not execution_id:
            raise ValueError("execution identity is required")
        return await run_disk_io(self._get, execution_id)

    async def list_records(
        self, *, state: ReconciliationState | None = None, limit: int = 100
    ) -> tuple[ReconciliationRecord, ...]:
        if limit <= 0 or limit > 1000:
            raise ValueError("reconciliation projection limit is invalid")
        return await run_disk_io(self._list_records, state, limit)

    async def acknowledge(self, acknowledgement: OwnerAcknowledgement) -> ReconciliationRecord:
        return await run_disk_io(self._acknowledge, acknowledgement)

    async def read_outbox(self, *, after_sequence: int = 0, limit: int = 100) -> tuple[ReconciliationOutboxRecord, ...]:
        if after_sequence < 0 or limit <= 0 or limit > 1000:
            raise ValueError("invalid reconciliation outbox cursor")
        return await run_disk_io(self._read_outbox, after_sequence, limit)

    async def mark_published(self, sequence: int) -> None:
        if sequence <= 0:
            raise ValueError("outbox sequence must be positive")
        await run_disk_io(self._mark_published, sequence)

    async def read_owner_commands(
        self, owner_id: str, *, after_sequence: int = 0, limit: int = 100
    ) -> tuple[tuple[int, OwnerCommand], ...]:
        if not owner_id or after_sequence < 0 or limit <= 0 or limit > 1000:
            raise ValueError("invalid owner command cursor")
        return await run_disk_io(self._read_owner_commands, owner_id, after_sequence, limit)

    def _bind_execution(
        self,
        execution_id: str,
        generation_id: str,
        provider: str,
        owner_id: str,
        strategy_id: str,
    ) -> None:
        identity = (execution_id, generation_id, provider, owner_id, strategy_id)
        with self._authority._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT execution_id, generation_id, provider, owner_id, strategy_id "
                "FROM reconciliation_bindings WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
            if row is not None:
                if tuple(row) != identity:
                    raise ReceiptConflictError("reconciliation execution binding conflict")
                connection.commit()
                return
            connection.execute(
                "INSERT INTO reconciliation_bindings VALUES (?, ?, ?, ?, ?)",
                identity,
            )
            connection.commit()

    def _provider_for(self, execution_id: str, generation_id: str) -> str:
        with self._authority._connect() as connection:
            row = connection.execute(
                "SELECT provider FROM reconciliation_bindings " "WHERE execution_id = ? AND generation_id = ?",
                (execution_id, generation_id),
            ).fetchone()
        if row is None:
            raise ReceiptConflictError("unknown reconciliation execution generation")
        return str(row[0])

    @staticmethod
    def _evidence_payload(evidence: ProviderEvidence) -> str:
        return json.dumps(
            evidence.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )

    def _append(self, evidence: ProviderEvidence) -> bool:
        payload = self._evidence_payload(evidence)
        digest = "sha256:" + hashlib.sha256(payload.encode()).hexdigest()
        received_at = datetime.now(timezone.utc)
        with self._authority._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            binding = connection.execute(
                "SELECT generation_id, provider FROM reconciliation_bindings " "WHERE execution_id = ?",
                (evidence.execution_id,),
            ).fetchone()
            if binding is None:
                raise ProviderEvidenceConflictError("provider evidence has no registered execution binding")
            if binding[1] != evidence.provider:
                raise ProviderEvidenceConflictError("provider evidence identity conflict")
            existing = connection.execute(
                "SELECT execution_id, digest FROM provider_evidence " "WHERE provider = ? AND event_id = ?",
                (evidence.provider, evidence.event_id),
            ).fetchone()
            if existing is not None:
                if existing != (evidence.execution_id, digest):
                    raise ProviderEvidenceConflictError("provider event identity reused with different evidence")
                connection.commit()
                return False
            proposal = connection.execute(
                "SELECT proposal_id FROM reconciliation_records " "WHERE execution_id = ?",
                (evidence.execution_id,),
            ).fetchone()
            if proposal is not None:
                raise ProviderEvidenceConflictError("provider evidence arrived after proposal snapshot")
            resource_rows = connection.execute(
                "SELECT payload FROM provider_evidence WHERE execution_id = ?",
                (evidence.execution_id,),
            ).fetchall()
            existing_evidence = tuple(ProviderEvidence.model_validate_json(row[0]) for row in resource_rows)
            resource_ids = {
                item.provider_resource_id for item in existing_evidence if item.provider_resource_id is not None
            }
            if (
                evidence.provider_resource_id is not None
                and resource_ids
                and evidence.provider_resource_id not in resource_ids
            ):
                raise ProviderEvidenceConflictError("provider resource identity changed within execution")
            connection.execute(
                "INSERT INTO provider_evidence VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    evidence.provider,
                    evidence.event_id,
                    evidence.execution_id,
                    binding[0],
                    digest,
                    received_at.isoformat(),
                    payload,
                ),
            )
            connection.commit()
            return True

    def _list_evidence(self, execution_id: str, limit: int) -> tuple[ProviderEvidenceRecord, ...]:
        with self._authority._connect() as connection:
            rows = connection.execute(
                "SELECT payload, generation_id, digest, received_at "
                "FROM provider_evidence WHERE execution_id = ? "
                "ORDER BY received_at, provider, event_id LIMIT ?",
                (execution_id, limit),
            ).fetchall()
        return tuple(
            ProviderEvidenceRecord(
                evidence=ProviderEvidence.model_validate_json(row[0]),
                generation_id=row[1],
                digest=row[2],
                received_at=datetime.fromisoformat(row[3]),
            )
            for row in rows
        )

    def _propose(self, execution_id: str) -> ReconciliationRecord:
        with self._authority._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = self._get_with_connection(connection, execution_id)
            if existing is not None:
                connection.commit()
                return existing
            binding = connection.execute(
                "SELECT generation_id, owner_id, strategy_id " "FROM reconciliation_bindings WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
            if binding is None:
                raise ReceiptConflictError("unknown reconciliation execution")
            evidence_rows = connection.execute(
                "SELECT digest FROM provider_evidence WHERE execution_id = ? "
                "ORDER BY received_at, provider, event_id",
                (execution_id,),
            ).fetchall()
            if not evidence_rows:
                raise ReceiptConflictError("reconciliation proposal requires durable provider evidence")
            digests = tuple(row[0] for row in evidence_rows)
            proposal_seed = json.dumps(
                [binding[1], execution_id, binding[0], binding[2], digests],
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode()
            proposal = ResolutionProposal(
                proposal_id="proposal:sha256:" + hashlib.sha256(proposal_seed).hexdigest(),
                owner_id=binding[1],
                execution_id=execution_id,
                generation_id=binding[0],
                strategy_id=binding[2],
                evidence_digests=digests,
            )
            record = require_owner_action(proposal)
            connection.execute(
                "INSERT INTO reconciliation_records VALUES (?, ?, ?, ?, ?, NULL)",
                (
                    proposal.proposal_id,
                    execution_id,
                    binding[0],
                    record.state.value,
                    proposal.model_dump_json(),
                ),
            )
            connection.execute(
                "INSERT INTO reconciliation_outbox (proposal_id, payload) " "VALUES (?, ?)",
                (proposal.proposal_id, proposal.model_dump_json()),
            )
            command = OwnerCommand(
                command_id=f"owner-command:{proposal.proposal_id}",
                proposal_id=proposal.proposal_id,
                owner_id=proposal.owner_id,
                execution_id=proposal.execution_id,
                generation_id=proposal.generation_id,
                strategy_id=proposal.strategy_id,
                evidence_digests=proposal.evidence_digests,
                issued_at=proposal.created_at,
            )
            connection.execute(
                "INSERT INTO owner_command_outbox " "(command_id, owner_id, proposal_id, payload) VALUES (?, ?, ?, ?)",
                (
                    command.command_id,
                    command.owner_id,
                    command.proposal_id,
                    command.model_dump_json(),
                ),
            )
            connection.commit()
            return record

    def _get(self, execution_id: str) -> ReconciliationRecord | None:
        with self._authority._connect() as connection:
            return self._get_with_connection(connection, execution_id)

    @staticmethod
    def _get_with_connection(connection: sqlite3.Connection, execution_id: str) -> ReconciliationRecord | None:
        row = connection.execute(
            "SELECT state, proposal, acknowledgement FROM reconciliation_records " "WHERE execution_id = ?",
            (execution_id,),
        ).fetchone()
        if row is None:
            return None
        return ReconciliationRecord(
            proposal=ResolutionProposal.model_validate_json(row[1]),
            state=ReconciliationState(row[0]),
            acknowledgement=(OwnerAcknowledgement.model_validate_json(row[2]) if row[2] is not None else None),
        )

    def _list_records(self, state: ReconciliationState | None, limit: int) -> tuple[ReconciliationRecord, ...]:
        query = "SELECT state, proposal, acknowledgement " "FROM reconciliation_records"
        parameters: tuple[object, ...]
        if state is None:
            parameters = (limit,)
        else:
            query += " WHERE state = ?"
            parameters = (state.value, limit)
        query += " ORDER BY rowid DESC LIMIT ?"
        with self._authority._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return tuple(
            ReconciliationRecord(
                proposal=ResolutionProposal.model_validate_json(row[1]),
                state=ReconciliationState(row[0]),
                acknowledgement=(OwnerAcknowledgement.model_validate_json(row[2]) if row[2] is not None else None),
            )
            for row in rows
        )

    def _acknowledge(self, acknowledgement: OwnerAcknowledgement) -> ReconciliationRecord:
        with self._authority._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT execution_id FROM reconciliation_records " "WHERE proposal_id = ?",
                (acknowledgement.proposal_id,),
            ).fetchone()
            if row is None:
                raise ReceiptConflictError("unknown reconciliation proposal")
            current = self._get_with_connection(connection, row[0])
            if current is None:
                raise ReceiptConflictError("reconciliation record disappeared")
            updated = acknowledge_owner_action(current, acknowledgement)
            if updated is current:
                connection.commit()
                return current
            connection.execute(
                "UPDATE reconciliation_records SET state = ?, acknowledgement = ? " "WHERE proposal_id = ?",
                (
                    updated.state.value,
                    acknowledgement.model_dump_json(),
                    acknowledgement.proposal_id,
                ),
            )
            connection.commit()
            return updated

    def _read_outbox(self, after_sequence: int, limit: int) -> tuple[ReconciliationOutboxRecord, ...]:
        with self._authority._connect() as connection:
            rows = connection.execute(
                "SELECT sequence, payload FROM reconciliation_outbox "
                "WHERE published = 0 AND sequence > ? ORDER BY sequence LIMIT ?",
                (after_sequence, limit),
            ).fetchall()
        return tuple(
            ReconciliationOutboxRecord(
                sequence=row[0],
                proposal=ResolutionProposal.model_validate_json(row[1]),
            )
            for row in rows
        )

    def _mark_published(self, sequence: int) -> None:
        with self._authority._connect() as connection:
            updated = connection.execute(
                "UPDATE reconciliation_outbox SET published = 1 " "WHERE sequence = ? AND published = 0",
                (sequence,),
            )
            if updated.rowcount != 1:
                raise ReceiptConflictError("unknown or already published reconciliation outbox sequence")

    def _read_owner_commands(
        self, owner_id: str, after_sequence: int, limit: int
    ) -> tuple[tuple[int, OwnerCommand], ...]:
        with self._authority._connect() as connection:
            rows = connection.execute(
                "SELECT sequence, payload FROM owner_command_outbox "
                "WHERE owner_id = ? AND sequence > ? ORDER BY sequence LIMIT ?",
                (owner_id, after_sequence, limit),
            ).fetchall()
        return tuple((row[0], OwnerCommand.model_validate_json(row[1])) for row in rows)


class SQLiteSessionReceiptStore:
    def __init__(self, authority: SQLiteAttemptReceiptStore) -> None:
        self._authority = authority

    async def get(self, session_id: str, generation_id: str) -> SessionReceipt | None:
        return await run_disk_io(self._get, session_id, generation_id)

    async def accept(self, receipt: SessionReceipt) -> SessionReceipt:
        return await run_disk_io(self._accept, receipt)

    async def compare_and_swap(
        self,
        receipt: SessionReceipt,
        *,
        expected_revision: int,
        fencing_token: int,
    ) -> SessionReceipt:
        return await run_disk_io(
            self._compare_and_swap,
            receipt,
            expected_revision,
            fencing_token,
        )

    def _get(self, session_id: str, generation_id: str) -> SessionReceipt | None:
        with self._authority._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM session_receipts " "WHERE session_id = ? AND generation_id = ?",
                (session_id, generation_id),
            ).fetchone()
        return SessionReceipt.model_validate_json(row[0]) if row else None

    def _accept(self, receipt: SessionReceipt) -> SessionReceipt:
        payload = receipt.model_dump_json()
        with self._authority._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload FROM session_receipts " "WHERE session_id = ? AND generation_id = ?",
                (receipt.session_id, receipt.generation_id),
            ).fetchone()
            if row:
                existing = SessionReceipt.model_validate_json(row[0])
                if (
                    existing.generation_artifact_digest != receipt.generation_artifact_digest
                    or existing.endpoint_binding_id != receipt.endpoint_binding_id
                ):
                    raise ReceiptConflictError("session receipt identity conflict")
                connection.commit()
                return existing
            connection.execute(
                "INSERT INTO session_receipts VALUES (?, ?, ?, ?, ?, ?)",
                (
                    receipt.session_id,
                    receipt.generation_id,
                    receipt.revision,
                    receipt.fencing_token,
                    receipt.state.value,
                    payload,
                ),
            )
            self._append_outbox(connection, receipt, payload)
            connection.commit()
            return receipt

    def _compare_and_swap(
        self,
        receipt: SessionReceipt,
        expected_revision: int,
        fencing_token: int,
    ) -> SessionReceipt:
        payload = receipt.model_dump_json()
        with self._authority._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload, revision, fencing_token FROM session_receipts "
                "WHERE session_id = ? AND generation_id = ?",
                (receipt.session_id, receipt.generation_id),
            ).fetchone()
            if row is None:
                raise ReceiptConflictError("session receipt does not exist")
            current = SessionReceipt.model_validate_json(row[0])
            if row[1] != expected_revision:
                raise ReceiptConflictError("session receipt revision conflict")
            if fencing_token < row[2] or receipt.fencing_token != fencing_token:
                raise ReceiptFencedError("session receipt fencing token rejected")
            validate_session_receipt_transition(current, receipt)
            updated = connection.execute(
                "UPDATE session_receipts SET revision = ?, fencing_token = ?, "
                "state = ?, payload = ? WHERE session_id = ? AND generation_id = ? "
                "AND revision = ? AND fencing_token <= ?",
                (
                    receipt.revision,
                    receipt.fencing_token,
                    receipt.state.value,
                    payload,
                    receipt.session_id,
                    receipt.generation_id,
                    expected_revision,
                    fencing_token,
                ),
            )
            if updated.rowcount != 1:
                raise ReceiptConflictError("session receipt CAS conflict")
            self._append_outbox(connection, receipt, payload)
            connection.commit()
            return receipt

    @staticmethod
    def _append_outbox(
        connection: sqlite3.Connection,
        receipt: SessionReceipt,
        payload: str,
    ) -> None:
        connection.execute(
            "INSERT INTO session_outbox " "(session_id, generation_id, receipt_revision, payload) VALUES (?, ?, ?, ?)",
            (
                receipt.session_id,
                receipt.generation_id,
                receipt.revision,
                payload,
            ),
        )


class SQLiteUsageLedger:
    """Budget authority sharing the receipt store's SQLite transaction domain."""

    def __init__(
        self,
        authority: SQLiteAttemptReceiptStore,
        *,
        clock_source: ClockSource,
    ) -> None:
        self._authority = authority
        self._clock_source = clock_source

    def _now(self) -> datetime:
        return self._clock_source.now().to_datetime(expected_clock=UNIX_UTC_CLOCK)

    def reservations_by_id(self, reservation_ids: tuple[str, ...]) -> tuple[BudgetReservation, ...]:
        if not reservation_ids or len(set(reservation_ids)) != len(reservation_ids):
            raise ValueError("budget reservation identities must be non-empty and unique")
        with self._authority._connect() as connection:
            reservations: list[BudgetReservation] = []
            for reservation_id in reservation_ids:
                row = connection.execute(
                    "SELECT payload FROM usage_reservations WHERE reservation_id = ?",
                    (reservation_id,),
                ).fetchone()
                if row is None:
                    raise SQLiteIntegrityError(f"budget reservation {reservation_id!r} is missing")
                reservation = BudgetReservation.model_validate_json(row[0])
                if reservation.reservation_id != reservation_id:
                    raise SQLiteIntegrityError(f"budget reservation {reservation_id!r} identity is corrupt")
                reservations.append(reservation)
            return tuple(reservations)

    async def configure_budget(self, tenant_id: str, project_id: str, limit_units: int) -> None:
        if (
            type(tenant_id) is not str
            or not tenant_id
            or type(project_id) is not str
            or not project_id
            or type(limit_units) is not int
            or limit_units < 0
        ):
            raise ValueError("invalid budget configuration")
        await run_disk_io(self._configure_budget, tenant_id, project_id, limit_units)

    async def reserve_many(
        self,
        requests: tuple[BudgetReservationRequest, ...],
        *,
        ttl_seconds: float,
    ) -> tuple[BudgetReservation, ...]:
        if not requests or type(ttl_seconds) not in {int, float} or not math.isfinite(ttl_seconds) or ttl_seconds <= 0:
            raise ValueError("budget reservation batch and ttl must be non-empty")
        return await run_disk_io(self._reserve_many, requests, ttl_seconds)

    async def reserve(
        self,
        *,
        reservation_id: str,
        attempt_id: str,
        tenant_id: str,
        project_id: str,
        units: int,
        ttl_seconds: float,
        dimension: BudgetDimension = BudgetDimension.INFERENCE_UNIT,
        scopes: tuple[BudgetScope, ...] = (),
    ) -> BudgetReservation:
        if (
            type(units) is not int
            or units <= 0
            or type(ttl_seconds) not in {int, float}
            or not math.isfinite(ttl_seconds)
            or ttl_seconds <= 0
        ):
            raise ValueError("reservation units and ttl must be positive")
        return await run_disk_io(
            self._reserve,
            reservation_id,
            attempt_id,
            tenant_id,
            project_id,
            units,
            ttl_seconds,
            dimension,
            scopes,
        )

    async def settle(
        self,
        reservation: BudgetReservation,
        *,
        settlement_id: str,
        actual_units: int,
    ) -> UsageSettlement:
        if type(actual_units) is not int or actual_units < 0:
            raise ValueError("actual usage cannot be negative")
        return await run_disk_io(
            self._settle,
            reservation,
            settlement_id,
            actual_units,
            ReservationState.SETTLED,
        )

    async def release(self, reservation: BudgetReservation, *, settlement_id: str) -> UsageSettlement:
        return await run_disk_io(
            self._settle,
            reservation,
            settlement_id,
            0,
            ReservationState.RELEASED,
        )

    async def pending_reconciliation(self, reservation: BudgetReservation, *, settlement_id: str) -> UsageSettlement:
        return await run_disk_io(
            self._settle,
            reservation,
            settlement_id,
            0,
            ReservationState.PENDING_RECONCILIATION,
        )

    async def reconcile(
        self,
        reservation: BudgetReservation,
        *,
        settlement_id: str,
        actual_units: int,
        fencing_token: int,
    ) -> UsageSettlement:
        if (
            type(actual_units) is not int
            or actual_units < 0
            or type(fencing_token) is not int
            or fencing_token <= reservation.fencing_token
        ):
            raise ValueError("reconciliation requires known usage and a higher fencing token")
        return await run_disk_io(
            self._reconcile,
            reservation,
            settlement_id,
            actual_units,
            fencing_token,
        )

    async def reclaim_expired(self, *, now: datetime, fencing_token: int) -> tuple[UsageSettlement, ...]:
        if (
            not isinstance(now, datetime)
            or now.utcoffset() is None
            or type(fencing_token) is not int
            or fencing_token <= 0
        ):
            raise ValueError("expiry recovery requires aware time and positive fencing token")
        return await run_disk_io(self._reclaim_expired, now, fencing_token)

    def _configure_budget(self, tenant_id: str, project_id: str, limit_units: int) -> None:
        with self._authority._connect() as connection:
            connection.execute(
                "INSERT INTO usage_budgets (tenant_id, project_id, limit_units) VALUES (?, ?, ?) "
                "ON CONFLICT (tenant_id, project_id) DO UPDATE SET limit_units = excluded.limit_units",
                (tenant_id, project_id, limit_units),
            )

    def _reserve_many(
        self,
        requests: tuple[BudgetReservationRequest, ...],
        ttl_seconds: float,
    ) -> tuple[BudgetReservation, ...]:
        reservation_ids = {request.reservation_id for request in requests}
        attempt_ids = {request.attempt_id for request in requests}
        if len(reservation_ids) != len(requests) or len(attempt_ids) != len(requests):
            raise ReceiptConflictError("budget batch identities are duplicated")
        with self._authority._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing: list[BudgetReservation] = []
            for request in requests:
                row = connection.execute(
                    "SELECT payload FROM usage_reservations WHERE reservation_id = ? OR attempt_id = ?",
                    (request.reservation_id, request.attempt_id),
                ).fetchone()
                if row is not None:
                    reservation = BudgetReservation.model_validate_json(row[0])
                    if not self._request_matches_reservation(request, reservation):
                        raise ReceiptConflictError("budget batch identity conflict")
                    existing.append(reservation)
            if existing:
                if len(existing) != len(requests):
                    raise ReceiptConflictError("budget batch is partially committed")
                connection.commit()
                return tuple(existing)

            pending: dict[tuple[str, str], int] = {}
            for request in requests:
                for scope in request.scopes:
                    key = (scope.tenant_id, scope.project_id)
                    budget = connection.execute(
                        "SELECT limit_units, settled_units FROM usage_budgets WHERE tenant_id = ? AND project_id = ?",
                        key,
                    ).fetchone()
                    if budget is None:
                        raise ReceiptConflictError(
                            "budget scope is not configured",
                            BudgetAdmissionDisposition.NOT_CONFIGURED,
                        )
                    active = connection.execute(
                        "SELECT COALESCE(SUM(s.units), 0) FROM usage_reservation_scopes s "
                        "JOIN usage_reservations r ON r.reservation_id = s.reservation_id "
                        "WHERE s.tenant_id = ? AND s.project_id = ? AND r.state IN (?, ?)",
                        (*key, ReservationState.RESERVED.value, ReservationState.PENDING_RECONCILIATION.value),
                    ).fetchone()[0]
                    requested = pending.get(key, 0) + request.units
                    if budget[1] + active + requested > budget[0]:
                        raise ReceiptConflictError(
                            "budget exhausted",
                            BudgetAdmissionDisposition.EXHAUSTED,
                        )
                    pending[key] = requested

            expires_at = self._now() + timedelta(seconds=ttl_seconds)
            reservations = tuple(
                BudgetReservation(
                    reservation_id=request.reservation_id,
                    attempt_id=request.attempt_id,
                    tenant_id=request.tenant_id,
                    project_id=request.project_id,
                    units=request.units,
                    dimension=request.dimension,
                    scopes=request.scopes,
                    fencing_token=1,
                    expires_at=expires_at,
                )
                for request in requests
            )
            for reservation in reservations:
                connection.execute(
                    "INSERT INTO usage_reservations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        reservation.reservation_id,
                        reservation.attempt_id,
                        reservation.tenant_id,
                        reservation.project_id,
                        reservation.units,
                        reservation.fencing_token,
                        reservation.state.value,
                        reservation.expires_at.isoformat(),
                        reservation.model_dump_json(),
                    ),
                )
                connection.executemany(
                    "INSERT INTO usage_reservation_scopes VALUES (?, ?, ?, ?)",
                    [
                        (reservation.reservation_id, scope.tenant_id, scope.project_id, reservation.units)
                        for scope in reservation.scopes
                    ],
                )
            connection.commit()
            return reservations

    @staticmethod
    def _request_matches_reservation(
        request: BudgetReservationRequest,
        reservation: BudgetReservation,
    ) -> bool:
        return (
            request.reservation_id == reservation.reservation_id
            and request.attempt_id == reservation.attempt_id
            and request.tenant_id == reservation.tenant_id
            and request.project_id == reservation.project_id
            and request.units == reservation.units
            and request.dimension is reservation.dimension
            and request.scopes == reservation.scopes
        )

    def _reserve(
        self,
        reservation_id: str,
        attempt_id: str,
        tenant_id: str,
        project_id: str,
        units: int,
        ttl_seconds: float,
        dimension: BudgetDimension,
        scopes: tuple[BudgetScope, ...],
    ) -> BudgetReservation:
        with self._authority._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            effective_scopes = scopes or (
                BudgetScope(
                    kind=BudgetScopeKind.INFERENCE,
                    tenant_id=tenant_id,
                    project_id=project_id,
                ),
            )
            if len(set(effective_scopes)) != len(effective_scopes):
                raise ReceiptConflictError("budget reservation scopes are duplicated")
            if effective_scopes[0].tenant_id != tenant_id or effective_scopes[0].project_id != project_id:
                raise ReceiptConflictError("primary budget scope identity conflict")
            existing = connection.execute(
                "SELECT payload FROM usage_reservations WHERE reservation_id = ? OR attempt_id = ?",
                (reservation_id, attempt_id),
            ).fetchone()
            if existing:
                reservation = BudgetReservation.model_validate_json(existing[0])
                if (
                    reservation.reservation_id != reservation_id
                    or reservation.attempt_id != attempt_id
                    or reservation.tenant_id != tenant_id
                    or reservation.project_id != project_id
                    or reservation.units != units
                    or reservation.dimension is not dimension
                    or reservation.scopes != effective_scopes
                ):
                    raise ReceiptConflictError("budget reservation identity conflict")
                connection.commit()
                return reservation
            for scope in effective_scopes:
                budget = connection.execute(
                    "SELECT limit_units, settled_units FROM usage_budgets WHERE tenant_id = ? AND project_id = ?",
                    (scope.tenant_id, scope.project_id),
                ).fetchone()
                if budget is None:
                    raise ReceiptConflictError(
                        "budget scope is not configured",
                        BudgetAdmissionDisposition.NOT_CONFIGURED,
                    )
                active = connection.execute(
                    "SELECT COALESCE(SUM(s.units), 0) FROM usage_reservation_scopes s "
                    "JOIN usage_reservations r ON r.reservation_id = s.reservation_id "
                    "WHERE s.tenant_id = ? AND s.project_id = ? AND r.state IN (?, ?)",
                    (
                        scope.tenant_id,
                        scope.project_id,
                        ReservationState.RESERVED.value,
                        ReservationState.PENDING_RECONCILIATION.value,
                    ),
                ).fetchone()[0]
                if budget[1] + active + units > budget[0]:
                    raise ReceiptConflictError(
                        "budget exhausted",
                        BudgetAdmissionDisposition.EXHAUSTED,
                    )
            reservation = BudgetReservation(
                reservation_id=reservation_id,
                attempt_id=attempt_id,
                tenant_id=tenant_id,
                project_id=project_id,
                units=units,
                dimension=dimension,
                scopes=effective_scopes,
                fencing_token=1,
                expires_at=self._now() + timedelta(seconds=ttl_seconds),
            )
            connection.execute(
                "INSERT INTO usage_reservations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    reservation.reservation_id,
                    reservation.attempt_id,
                    reservation.tenant_id,
                    reservation.project_id,
                    reservation.units,
                    reservation.fencing_token,
                    reservation.state.value,
                    reservation.expires_at.isoformat(),
                    reservation.model_dump_json(),
                ),
            )
            connection.executemany(
                "INSERT INTO usage_reservation_scopes VALUES (?, ?, ?, ?)",
                [
                    (reservation.reservation_id, scope.tenant_id, scope.project_id, units)
                    for scope in reservation.scopes
                ],
            )
            connection.commit()
            return reservation

    def _settle(
        self,
        reservation: BudgetReservation,
        settlement_id: str,
        actual_units: int,
        state: ReservationState,
    ) -> UsageSettlement:
        if actual_units > reservation.units:
            raise ReceiptConflictError("actual usage exceeds reserved budget")
        with self._authority._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT payload FROM usage_settlements WHERE settlement_id = ?",
                (settlement_id,),
            ).fetchone()
            if existing:
                settlement = UsageSettlement.model_validate_json(existing[0])
                if (
                    settlement.reservation_id != reservation.reservation_id
                    or settlement.attempt_id != reservation.attempt_id
                    or settlement.actual_units != actual_units
                    or settlement.state is not state
                ):
                    raise ReceiptConflictError("settlement identity conflict")
                connection.commit()
                return settlement
            row = connection.execute(
                "SELECT payload, state FROM usage_reservations WHERE reservation_id = ?",
                (reservation.reservation_id,),
            ).fetchone()
            if row is None:
                raise ReceiptConflictError("unknown budget reservation")
            current = BudgetReservation.model_validate_json(row[0])
            if current != reservation or row[1] != ReservationState.RESERVED.value:
                if row[1] != ReservationState.RESERVED.value:
                    raise ReceiptConflictError("budget reservation already settled")
                raise ReceiptConflictError("budget reservation payload mismatch")
            settlement = UsageSettlement(
                settlement_id=settlement_id,
                reservation_id=reservation.reservation_id,
                attempt_id=reservation.attempt_id,
                actual_units=actual_units,
                dimension=reservation.dimension,
                state=state,
            )
            updated_reservation = reservation.model_copy(update={"state": state})
            connection.execute(
                "UPDATE usage_reservations SET state = ?, payload = ? WHERE reservation_id = ?",
                (
                    state.value,
                    updated_reservation.model_dump_json(),
                    reservation.reservation_id,
                ),
            )
            if state is ReservationState.SETTLED:
                connection.executemany(
                    "UPDATE usage_budgets SET settled_units = settled_units + ? "
                    "WHERE tenant_id = ? AND project_id = ?",
                    [(actual_units, scope.tenant_id, scope.project_id) for scope in reservation.scopes],
                )
            connection.execute(
                "INSERT INTO usage_settlements VALUES (?, ?, ?, ?)",
                (
                    settlement.settlement_id,
                    settlement.reservation_id,
                    settlement.attempt_id,
                    settlement.model_dump_json(),
                ),
            )
            connection.commit()
            return settlement

    def _reconcile(
        self,
        reservation: BudgetReservation,
        settlement_id: str,
        actual_units: int,
        fencing_token: int,
    ) -> UsageSettlement:
        if actual_units > reservation.units:
            raise ReceiptConflictError("actual usage exceeds reserved budget")
        with self._authority._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT payload FROM usage_settlements WHERE settlement_id = ?",
                (settlement_id,),
            ).fetchone()
            if existing:
                settlement = UsageSettlement.model_validate_json(existing[0])
                if (
                    settlement.reservation_id != reservation.reservation_id
                    or settlement.attempt_id != reservation.attempt_id
                    or settlement.actual_units != actual_units
                    or settlement.state is not ReservationState.SETTLED
                ):
                    raise ReceiptConflictError("reconciliation identity conflict")
                connection.commit()
                return settlement
            row = connection.execute(
                "SELECT payload FROM usage_reservations WHERE reservation_id = ?",
                (reservation.reservation_id,),
            ).fetchone()
            if row is None:
                raise ReceiptConflictError("unknown budget reservation")
            current = BudgetReservation.model_validate_json(row[0])
            if not self._same_reservation_identity(current, reservation):
                raise ReceiptConflictError("budget reservation payload mismatch")
            if current.state is not ReservationState.PENDING_RECONCILIATION:
                raise ReceiptConflictError("budget reservation is not pending reconciliation")
            if fencing_token <= current.fencing_token:
                raise ReceiptFencedError("reconciliation fencing token is stale")
            settlement = UsageSettlement(
                settlement_id=settlement_id,
                reservation_id=reservation.reservation_id,
                attempt_id=reservation.attempt_id,
                actual_units=actual_units,
                dimension=reservation.dimension,
                state=ReservationState.SETTLED,
            )
            updated = current.model_copy(
                update={
                    "state": ReservationState.SETTLED,
                    "fencing_token": fencing_token,
                }
            )
            connection.execute(
                "UPDATE usage_reservations SET fencing_token = ?, state = ?, payload = ? " "WHERE reservation_id = ?",
                (
                    fencing_token,
                    ReservationState.SETTLED.value,
                    updated.model_dump_json(),
                    reservation.reservation_id,
                ),
            )
            connection.executemany(
                "UPDATE usage_budgets SET settled_units = settled_units + ? " "WHERE tenant_id = ? AND project_id = ?",
                [(actual_units, scope.tenant_id, scope.project_id) for scope in reservation.scopes],
            )
            connection.execute(
                "INSERT INTO usage_settlements VALUES (?, ?, ?, ?)",
                (
                    settlement.settlement_id,
                    settlement.reservation_id,
                    settlement.attempt_id,
                    settlement.model_dump_json(),
                ),
            )
            connection.commit()
            return settlement

    def _reclaim_expired(self, now: datetime, fencing_token: int) -> tuple[UsageSettlement, ...]:
        settlements: list[UsageSettlement] = []
        with self._authority._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT payload FROM usage_reservations " "WHERE state = ? AND expires_at <= ? AND fencing_token < ?",
                (ReservationState.RESERVED.value, now.isoformat(), fencing_token),
            ).fetchall()
            for row in rows:
                current = BudgetReservation.model_validate_json(row[0])
                settlement = UsageSettlement(
                    settlement_id=f"expiry:{current.reservation_id}:{fencing_token}",
                    reservation_id=current.reservation_id,
                    attempt_id=current.attempt_id,
                    actual_units=0,
                    dimension=current.dimension,
                    state=ReservationState.RELEASED,
                )
                updated = current.model_copy(
                    update={
                        "state": ReservationState.RELEASED,
                        "fencing_token": fencing_token,
                    }
                )
                connection.execute(
                    "UPDATE usage_reservations SET fencing_token = ?, state = ?, payload = ? "
                    "WHERE reservation_id = ?",
                    (
                        fencing_token,
                        ReservationState.RELEASED.value,
                        updated.model_dump_json(),
                        current.reservation_id,
                    ),
                )
                connection.execute(
                    "INSERT INTO usage_settlements VALUES (?, ?, ?, ?)",
                    (
                        settlement.settlement_id,
                        settlement.reservation_id,
                        settlement.attempt_id,
                        settlement.model_dump_json(),
                    ),
                )
                settlements.append(settlement)
            connection.commit()
        return tuple(settlements)

    @staticmethod
    def _same_reservation_identity(left: BudgetReservation, right: BudgetReservation) -> bool:
        return (
            left.reservation_id == right.reservation_id
            and left.attempt_id == right.attempt_id
            and left.tenant_id == right.tenant_id
            and left.project_id == right.project_id
            and left.units == right.units
            and left.dimension is right.dimension
            and left.scopes == right.scopes
        )
