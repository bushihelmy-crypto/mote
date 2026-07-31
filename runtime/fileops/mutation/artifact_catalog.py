"""Exact, durable lifecycle catalog for content-addressed artifacts."""

from __future__ import annotations

import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Iterable, Iterator

from mote.contracts.content.identity import ContentIdentity
from mote.contracts.file.errors import SnapshotDurabilityError

_FORMAT_VERSION = 4
_DIGEST = re.compile(r"[0-9a-f]{64}")
_NANOSECONDS_PER_SECOND = 1_000_000_000


class ArtifactReservationState(StrEnum):
    ACTIVE = "active"
    RELEASED = "released"
    EXPIRED = "expired"


class ArtifactStageState(StrEnum):
    OPEN = "open"
    SEALED = "sealed"
    LIVE = "live"


class ArtifactObjectState(StrEnum):
    STAGING = "staging"
    LIVE = "live"
    QUARANTINED = "quarantined"
    DELETING = "deleting"


class ArtifactQuotaExceededError(SnapshotDurabilityError):
    """A reservation would exceed the catalog's hard physical budget."""


class ArtifactLifecycleConflictError(SnapshotDurabilityError):
    """A lifecycle transition does not match the durable current state."""


class ArtifactCatalogGenerationConflictError(ArtifactLifecycleConflictError):
    """The catalog changed after a garbage-collection root snapshot."""


@dataclass(frozen=True, slots=True)
class ArtifactReservation:
    reservation_id: str
    owner: str
    capacity_bytes: int
    remaining_bytes: int
    expires_at_ns: int
    state: ArtifactReservationState


@dataclass(frozen=True, slots=True)
class ArtifactStage:
    stage_id: str
    reservation_id: str
    allocation_bytes: int
    state: ArtifactStageState
    artifact: ContentIdentity | None = None


@dataclass(frozen=True, slots=True)
class ArtifactObject:
    artifact: ContentIdentity
    state: ArtifactObjectState
    quarantined_at_ns: int | None = None


@dataclass(frozen=True, slots=True)
class ArtifactCatalogSnapshot:
    generation: int
    objects: tuple[ArtifactObject, ...]


@dataclass(frozen=True, slots=True)
class ArtifactQuarantineReconciliation:
    restored_objects: tuple[ArtifactObject, ...]
    deletion_candidates: tuple[ArtifactObject, ...]


@dataclass(frozen=True, slots=True)
class ArtifactLifecycleHealth:
    generation: int
    hard_limit_bytes: int
    physical_bytes: int
    reserved_bytes: int
    staged_allocation_bytes: int
    accounted_bytes: int
    active_reservations: int
    open_stages: int
    staging_objects: int
    quarantined_objects: int
    deleting_objects: int

    @property
    def quota_pressure(self) -> float:
        if self.hard_limit_bytes == 0:
            return 1.0 if self.accounted_bytes else 0.0
        return self.accounted_bytes / self.hard_limit_bytes


@dataclass(frozen=True, slots=True)
class ArtifactRecoveryReport:
    promoted_objects: int
    abandoned_stages: int
    expired_reservations: int


_SCHEMA = (
    """
    CREATE TABLE catalog_meta (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        format_version INTEGER NOT NULL,
        hard_limit_bytes INTEGER NOT NULL CHECK (hard_limit_bytes >= 0),
        logical_now_ns INTEGER NOT NULL CHECK (logical_now_ns >= 0),
        generation INTEGER NOT NULL CHECK (generation >= 0)
    ) STRICT
    """,
    """
    CREATE TABLE reservations (
        reservation_id TEXT PRIMARY KEY,
        owner TEXT NOT NULL CHECK (length(owner) > 0),
        capacity_bytes INTEGER NOT NULL CHECK (capacity_bytes >= 0),
        remaining_bytes INTEGER NOT NULL CHECK (remaining_bytes >= 0),
        expires_at_ns INTEGER NOT NULL CHECK (expires_at_ns >= 0),
        state TEXT NOT NULL CHECK (state IN ('active', 'released', 'expired'))
    ) STRICT
    """,
    """
    CREATE TABLE stages (
        stage_id TEXT PRIMARY KEY,
        reservation_id TEXT NOT NULL REFERENCES reservations(reservation_id),
        allocation_bytes INTEGER NOT NULL CHECK (allocation_bytes >= 0),
        state TEXT NOT NULL CHECK (state IN ('open', 'sealed', 'live')),
        digest TEXT,
        size INTEGER,
        CHECK (
            (state = 'open' AND digest IS NULL AND size IS NULL) OR
            (state IN ('sealed', 'live') AND length(digest) = 64 AND size >= 0)
        )
    ) STRICT
    """,
    """
    CREATE TABLE objects (
        digest TEXT PRIMARY KEY CHECK (length(digest) = 64),
        size INTEGER NOT NULL CHECK (size >= 0),
        state TEXT NOT NULL CHECK (
            state IN ('staging', 'live', 'quarantined', 'deleting')
        ),
        stage_id TEXT UNIQUE REFERENCES stages(stage_id),
        quarantined_at_ns INTEGER,
        CHECK (
            (state IN ('staging', 'live') AND quarantined_at_ns IS NULL) OR
            (state IN ('quarantined', 'deleting') AND
             quarantined_at_ns IS NOT NULL AND quarantined_at_ns >= 0)
        )
    ) STRICT
    """,
    """
    CREATE TABLE reservation_objects (
        reservation_id TEXT NOT NULL REFERENCES reservations(reservation_id),
        digest TEXT NOT NULL REFERENCES objects(digest),
        PRIMARY KEY (reservation_id, digest)
    ) STRICT, WITHOUT ROWID
    """,
)

_EXPECTED_COLUMNS = {
    "catalog_meta": (
        ("singleton", "INTEGER", 0, 1),
        ("format_version", "INTEGER", 1, 0),
        ("hard_limit_bytes", "INTEGER", 1, 0),
        ("logical_now_ns", "INTEGER", 1, 0),
        ("generation", "INTEGER", 1, 0),
    ),
    "reservations": (
        ("reservation_id", "TEXT", 1, 1),
        ("owner", "TEXT", 1, 0),
        ("capacity_bytes", "INTEGER", 1, 0),
        ("remaining_bytes", "INTEGER", 1, 0),
        ("expires_at_ns", "INTEGER", 1, 0),
        ("state", "TEXT", 1, 0),
    ),
    "stages": (
        ("stage_id", "TEXT", 1, 1),
        ("reservation_id", "TEXT", 1, 0),
        ("allocation_bytes", "INTEGER", 1, 0),
        ("state", "TEXT", 1, 0),
        ("digest", "TEXT", 0, 0),
        ("size", "INTEGER", 0, 0),
    ),
    "objects": (
        ("digest", "TEXT", 1, 1),
        ("size", "INTEGER", 1, 0),
        ("state", "TEXT", 1, 0),
        ("stage_id", "TEXT", 0, 0),
        ("quarantined_at_ns", "INTEGER", 0, 0),
    ),
    "reservation_objects": (
        ("reservation_id", "TEXT", 1, 1),
        ("digest", "TEXT", 1, 2),
    ),
}


class ArtifactLifecycleCatalog:
    """Sole durable authority for artifact admission and lifecycle state.

    Payload storage deliberately remains outside this class. ``record_staged``
    may only be called after the referenced payload has been durably sealed by
    the content-addressed repository.
    """

    def __init__(self, root: Path, *, hard_limit_bytes: int) -> None:
        if type(hard_limit_bytes) is not int or hard_limit_bytes < 0:
            raise ValueError("artifact hard limit must be a non-negative integer")
        self.root = Path(root)
        self.control_root = self.root / ".lifecycle"
        self.path = self.control_root / "catalog.sqlite3"
        self.hard_limit_bytes = hard_limit_bytes
        self.control_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._initialize()
        self.recover()

    def reserve(
        self,
        max_bytes: int,
        owner: str,
        ttl_seconds: float,
        *,
        now_ns: int | None = None,
    ) -> ArtifactReservation:
        self._validate_bytes(max_bytes, "reservation capacity")
        if type(owner) is not str or not owner:
            raise ValueError("artifact reservation owner must be non-empty")
        ttl_ns = self._ttl_ns(ttl_seconds)
        with self._transaction() as connection:
            now = self._advance_clock(connection, now_ns)
            accounted = self._accounted_bytes(connection)
            if accounted + max_bytes > self.hard_limit_bytes:
                raise ArtifactQuotaExceededError(
                    "artifact quota reservation exceeds the hard limit",
                    requested=max_bytes,
                    accounted=accounted,
                    hard_limit=self.hard_limit_bytes,
                )
            reservation_id = uuid.uuid4().hex
            expires_at = now + ttl_ns
            connection.execute(
                """
                INSERT INTO reservations (
                    reservation_id, owner, capacity_bytes, remaining_bytes,
                    expires_at_ns, state
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    reservation_id,
                    owner,
                    max_bytes,
                    max_bytes,
                    expires_at,
                    ArtifactReservationState.ACTIVE.value,
                ),
            )
            self._bump_generation(connection)
            return ArtifactReservation(
                reservation_id=reservation_id,
                owner=owner,
                capacity_bytes=max_bytes,
                remaining_bytes=max_bytes,
                expires_at_ns=expires_at,
                state=ArtifactReservationState.ACTIVE,
            )

    def renew(
        self,
        reservation: ArtifactReservation | str,
        ttl_seconds: float,
        *,
        now_ns: int | None = None,
    ) -> ArtifactReservation:
        ttl_ns = self._ttl_ns(ttl_seconds)
        reservation_id = self._reservation_id(reservation)
        with self._transaction() as connection:
            now = self._advance_clock(connection, now_ns)
            current = self._reservation(connection, reservation_id)
            if current.state != ArtifactReservationState.ACTIVE or current.expires_at_ns <= now:
                raise ArtifactLifecycleConflictError(
                    "artifact reservation is not renewable",
                    reservation_id=reservation_id,
                )
            expires_at = now + ttl_ns
            connection.execute(
                "UPDATE reservations SET expires_at_ns = ? WHERE reservation_id = ?",
                (expires_at, reservation_id),
            )
            self._bump_generation(connection)
            return ArtifactReservation(
                reservation_id=current.reservation_id,
                owner=current.owner,
                capacity_bytes=current.capacity_bytes,
                remaining_bytes=current.remaining_bytes,
                expires_at_ns=expires_at,
                state=current.state,
            )

    def stage(
        self,
        reservation: ArtifactReservation | str,
        maximum_bytes: int,
        *,
        now_ns: int | None = None,
    ) -> ArtifactStage:
        self._validate_bytes(maximum_bytes, "artifact stage allocation")
        reservation_id = self._reservation_id(reservation)
        with self._transaction() as connection:
            now = self._advance_clock(connection, now_ns)
            current = self._reservation(connection, reservation_id)
            if current.state != ArtifactReservationState.ACTIVE or current.expires_at_ns <= now:
                raise ArtifactLifecycleConflictError(
                    "artifact reservation is not active",
                    reservation_id=reservation_id,
                )
            if maximum_bytes > current.remaining_bytes:
                raise ArtifactQuotaExceededError(
                    "artifact stage exceeds its reservation",
                    requested=maximum_bytes,
                    remaining=current.remaining_bytes,
                    reservation_id=reservation_id,
                )
            stage_id = uuid.uuid4().hex
            connection.execute(
                "UPDATE reservations SET remaining_bytes = remaining_bytes - ? " "WHERE reservation_id = ?",
                (maximum_bytes, reservation_id),
            )
            connection.execute(
                """
                INSERT INTO stages (
                    stage_id, reservation_id, allocation_bytes, state,
                    digest, size
                ) VALUES (?, ?, ?, ?, NULL, NULL)
                """,
                (
                    stage_id,
                    reservation_id,
                    maximum_bytes,
                    ArtifactStageState.OPEN.value,
                ),
            )
            self._bump_generation(connection)
            return ArtifactStage(
                stage_id=stage_id,
                reservation_id=reservation_id,
                allocation_bytes=maximum_bytes,
                state=ArtifactStageState.OPEN,
            )

    def record_staged(
        self,
        stage: ArtifactStage | str,
        artifact: ContentIdentity,
        *,
        now_ns: int | None = None,
    ) -> ArtifactObject:
        self._validate_ref(artifact)
        stage_id = self._stage_id(stage)
        with self._transaction() as connection:
            now = self._advance_clock(connection, now_ns)
            current = self._stage(connection, stage_id)
            reservation = self._reservation(connection, current.reservation_id)
            if reservation.state != ArtifactReservationState.ACTIVE or reservation.expires_at_ns <= now:
                raise ArtifactLifecycleConflictError(
                    "artifact stage reservation is not active",
                    stage_id=stage_id,
                    reservation_id=current.reservation_id,
                )
            if current.state != ArtifactStageState.OPEN:
                if current.artifact == artifact:
                    return self._object(connection, artifact.digest)
                raise ArtifactLifecycleConflictError(
                    "artifact stage is already sealed to another object",
                    stage_id=stage_id,
                )
            if artifact.size > current.allocation_bytes:
                raise ArtifactQuotaExceededError(
                    "sealed artifact exceeds its stage allocation",
                    stage_id=stage_id,
                    size=artifact.size,
                    allocation=current.allocation_bytes,
                )
            existing = self._optional_object(connection, artifact.digest)
            if existing is not None:
                if existing.artifact.size != artifact.size:
                    raise ArtifactLifecycleConflictError(
                        "artifact digest resolves to a conflicting size",
                        digest=artifact.digest,
                    )
                if existing.state != ArtifactObjectState.LIVE:
                    raise ArtifactLifecycleConflictError(
                        "artifact digest is owned by another lifecycle transition",
                        digest=artifact.digest,
                        state=existing.state.value,
                    )
                self._finish_deduplicated_stage(connection, current, artifact)
                self._bump_generation(connection)
                return existing
            connection.execute(
                """
                INSERT INTO objects (digest, size, state, stage_id)
                VALUES (?, ?, ?, ?)
                """,
                (
                    artifact.digest,
                    artifact.size,
                    ArtifactObjectState.STAGING.value,
                    stage_id,
                ),
            )
            connection.execute(
                """
                UPDATE stages
                SET allocation_bytes = 0, state = ?, digest = ?, size = ?
                WHERE stage_id = ?
                """,
                (
                    ArtifactStageState.SEALED.value,
                    artifact.digest,
                    artifact.size,
                    stage_id,
                ),
            )
            connection.execute(
                """
                UPDATE reservations
                SET remaining_bytes = remaining_bytes + ?
                WHERE reservation_id = ?
                """,
                (
                    current.allocation_bytes - artifact.size,
                    current.reservation_id,
                ),
            )
            self._bump_generation(connection)
            return ArtifactObject(artifact, ArtifactObjectState.STAGING)

    def mark_live(
        self,
        stage: ArtifactStage | str,
        artifact: ContentIdentity,
    ) -> ArtifactObject:
        self._validate_ref(artifact)
        stage_id = self._stage_id(stage)
        with self._transaction() as connection:
            current = self._stage(connection, stage_id)
            if current.state == ArtifactStageState.LIVE:
                if current.artifact != artifact:
                    raise ArtifactLifecycleConflictError(
                        "artifact stage is live with another object",
                        stage_id=stage_id,
                    )
                durable = self._object(connection, artifact.digest)
                if durable.state != ArtifactObjectState.LIVE:
                    raise ArtifactLifecycleConflictError(
                        "live artifact stage has a non-live object",
                        stage_id=stage_id,
                    )
                return durable
            if current.state != ArtifactStageState.SEALED or current.artifact != artifact:
                raise ArtifactLifecycleConflictError(
                    "artifact stage does not seal the requested object",
                    stage_id=stage_id,
                    digest=artifact.digest,
                )
            durable = self._object(connection, artifact.digest)
            if durable.state != ArtifactObjectState.STAGING:
                raise ArtifactLifecycleConflictError(
                    "artifact object is not staging",
                    digest=artifact.digest,
                    state=durable.state.value,
                )
            self._promote(connection, current, artifact)
            self._bump_generation(connection)
            return ArtifactObject(artifact, ArtifactObjectState.LIVE)

    def abort_stage(self, stage: ArtifactStage | str) -> None:
        stage_id = self._stage_id(stage)
        with self._transaction() as connection:
            current = self._stage(connection, stage_id)
            if current.state != ArtifactStageState.OPEN:
                raise ArtifactLifecycleConflictError(
                    "sealed artifact stage cannot be abandoned",
                    stage_id=stage_id,
                )
            self._abandon_open_stage(connection, current)
            self._bump_generation(connection)

    def release(self, reservation: ArtifactReservation | str) -> ArtifactReservation:
        reservation_id = self._reservation_id(reservation)
        with self._transaction() as connection:
            current = self._reservation(connection, reservation_id)
            if current.state == ArtifactReservationState.RELEASED:
                return current
            if current.state != ArtifactReservationState.ACTIVE:
                raise ArtifactLifecycleConflictError(
                    "artifact reservation is not releasable",
                    reservation_id=reservation_id,
                    state=current.state.value,
                )
            pending = connection.execute(
                """
                SELECT COUNT(*) FROM stages
                WHERE reservation_id = ? AND state != ?
                """,
                (reservation_id, ArtifactStageState.LIVE.value),
            ).fetchone()[0]
            if pending:
                raise ArtifactLifecycleConflictError(
                    "artifact reservation still owns stages",
                    reservation_id=reservation_id,
                    stages=pending,
                )
            connection.execute(
                "DELETE FROM reservation_objects WHERE reservation_id = ?",
                (reservation_id,),
            )
            self._clear_completed_stages(connection, reservation_id)
            connection.execute(
                """
                UPDATE reservations
                SET remaining_bytes = 0, state = ?
                WHERE reservation_id = ?
                """,
                (ArtifactReservationState.RELEASED.value, reservation_id),
            )
            self._bump_generation(connection)
            return ArtifactReservation(
                reservation_id=current.reservation_id,
                owner=current.owner,
                capacity_bytes=current.capacity_bytes,
                remaining_bytes=0,
                expires_at_ns=current.expires_at_ns,
                state=ArtifactReservationState.RELEASED,
            )

    def recover(self, *, now_ns: int | None = None) -> ArtifactRecoveryReport:
        promoted = 0
        abandoned = 0
        with self._transaction() as connection:
            now = self._advance_clock(connection, now_ns)
            rows = connection.execute("SELECT stage_id FROM stages ORDER BY stage_id").fetchall()
            for row in rows:
                stage = self._stage(connection, row["stage_id"])
                if stage.state == ArtifactStageState.SEALED:
                    if stage.artifact is None:
                        raise ArtifactLifecycleConflictError(
                            "sealed artifact stage has no object",
                            stage_id=stage.stage_id,
                        )
                    self._promote(connection, stage, stage.artifact)
                    promoted += 1
                elif stage.state == ArtifactStageState.OPEN:
                    reservation = self._reservation(
                        connection,
                        stage.reservation_id,
                    )
                    if reservation.expires_at_ns <= now:
                        self._abandon_open_stage(connection, stage)
                        abandoned += 1
            expired = connection.execute(
                """
                SELECT reservation_id FROM reservations
                WHERE state = ? AND expires_at_ns <= ?
                ORDER BY reservation_id
                """,
                (ArtifactReservationState.ACTIVE.value, now),
            ).fetchall()
            for row in expired:
                reservation_id = row["reservation_id"]
                connection.execute(
                    "DELETE FROM reservation_objects WHERE reservation_id = ?",
                    (reservation_id,),
                )
                self._clear_completed_stages(connection, reservation_id)
                connection.execute(
                    """
                    UPDATE reservations
                    SET remaining_bytes = 0, state = ?
                    WHERE reservation_id = ?
                    """,
                    (ArtifactReservationState.EXPIRED.value, reservation_id),
                )
            if promoted or abandoned or expired:
                self._bump_generation(connection)
            return ArtifactRecoveryReport(
                promoted_objects=promoted,
                abandoned_stages=abandoned,
                expired_reservations=len(expired),
            )

    def health(self) -> ArtifactLifecycleHealth:
        with self._read_connection() as connection:
            generation = self._generation(connection)
            physical = connection.execute("SELECT COALESCE(SUM(size), 0) FROM objects").fetchone()[0]
            reserved = connection.execute(
                """
                SELECT COALESCE(SUM(remaining_bytes), 0) FROM reservations
                WHERE state = ?
                """,
                (ArtifactReservationState.ACTIVE.value,),
            ).fetchone()[0]
            staged = connection.execute("SELECT COALESCE(SUM(allocation_bytes), 0) FROM stages").fetchone()[0]
            active = connection.execute(
                "SELECT COUNT(*) FROM reservations WHERE state = ?",
                (ArtifactReservationState.ACTIVE.value,),
            ).fetchone()[0]
            open_stages = connection.execute(
                "SELECT COUNT(*) FROM stages WHERE state = ?",
                (ArtifactStageState.OPEN.value,),
            ).fetchone()[0]
            staging_objects = connection.execute(
                "SELECT COUNT(*) FROM objects WHERE state = ?",
                (ArtifactObjectState.STAGING.value,),
            ).fetchone()[0]
            quarantined_objects = connection.execute(
                "SELECT COUNT(*) FROM objects WHERE state = ?",
                (ArtifactObjectState.QUARANTINED.value,),
            ).fetchone()[0]
            deleting_objects = connection.execute(
                "SELECT COUNT(*) FROM objects WHERE state = ?",
                (ArtifactObjectState.DELETING.value,),
            ).fetchone()[0]
        return ArtifactLifecycleHealth(
            generation=generation,
            hard_limit_bytes=self.hard_limit_bytes,
            physical_bytes=physical,
            reserved_bytes=reserved,
            staged_allocation_bytes=staged,
            accounted_bytes=physical + reserved + staged,
            active_reservations=active,
            open_stages=open_stages,
            staging_objects=staging_objects,
            quarantined_objects=quarantined_objects,
            deleting_objects=deleting_objects,
        )

    def reservation(self, reservation_id: str) -> ArtifactReservation:
        with self._read_connection() as connection:
            return self._reservation(connection, reservation_id)

    def object(self, digest: str) -> ArtifactObject | None:
        if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
            raise ValueError("artifact digest is invalid")
        with self._read_connection() as connection:
            return self._optional_object(connection, digest)

    def reservation_objects(
        self,
        reservation: ArtifactReservation | str,
    ) -> tuple[ArtifactObject, ...]:
        reservation_id = self._reservation_id(reservation)
        with self._read_connection() as connection:
            rows = connection.execute(
                """
                SELECT objects.digest, objects.size, objects.state,
                       objects.quarantined_at_ns
                FROM reservation_objects
                JOIN objects USING (digest)
                WHERE reservation_id = ?
                ORDER BY objects.digest
                """,
                (reservation_id,),
            ).fetchall()
            return tuple(
                ArtifactObject(
                    ContentIdentity(digest=row["digest"], size=row["size"]),
                    ArtifactObjectState(row["state"]),
                    row["quarantined_at_ns"],
                )
                for row in rows
            )

    def gc_snapshot(self) -> ArtifactCatalogSnapshot:
        with self._read_connection() as connection:
            generation = self._generation(connection)
            rows = connection.execute(
                """
                SELECT digest, size, state, quarantined_at_ns
                FROM objects ORDER BY digest
                """
            ).fetchall()
        return ArtifactCatalogSnapshot(
            generation=generation,
            objects=tuple(
                ArtifactObject(
                    ContentIdentity(digest=row["digest"], size=row["size"]),
                    ArtifactObjectState(row["state"]),
                    row["quarantined_at_ns"],
                )
                for row in rows
            ),
        )

    def deletion_candidates(self, *, limit: int) -> tuple[ArtifactObject, ...]:
        if type(limit) is not int or limit <= 0:
            raise ValueError("artifact deletion candidate limit must be positive")
        with self._read_connection() as connection:
            rows = connection.execute(
                """
                SELECT digest, size, quarantined_at_ns
                FROM objects WHERE state = ? ORDER BY digest LIMIT ?
                """,
                (ArtifactObjectState.DELETING.value, limit),
            ).fetchall()
        return tuple(
            ArtifactObject(
                ContentIdentity(digest=row["digest"], size=row["size"]),
                ArtifactObjectState.DELETING,
                row["quarantined_at_ns"],
            )
            for row in rows
        )

    def quarantine_unreachable(
        self,
        candidates: Iterable[ContentIdentity],
        *,
        expected_generation: int,
        now_ns: int | None = None,
    ) -> tuple[ArtifactObject, ...]:
        expected_generation = self._validate_generation(expected_generation)
        artifacts = self._canonical_refs(candidates)
        with self._transaction() as connection:
            now = self._advance_clock(connection, now_ns)
            self._require_generation(connection, expected_generation)
            eligible: list[ContentIdentity] = []
            for artifact in artifacts:
                current = self._object(connection, artifact.digest)
                if current.artifact != artifact or current.state != ArtifactObjectState.LIVE:
                    raise ArtifactLifecycleConflictError(
                        "artifact quarantine candidate changed",
                        digest=artifact.digest,
                        state=current.state.value,
                    )
                protected = connection.execute(
                    """
                    SELECT 1 FROM reservation_objects
                    WHERE digest = ? LIMIT 1
                    """,
                    (artifact.digest,),
                ).fetchone()
                if protected is not None:
                    continue
                eligible.append(artifact)
            for artifact in eligible:
                connection.execute(
                    """
                    UPDATE objects
                    SET state = ?, quarantined_at_ns = ?
                    WHERE digest = ? AND state = ?
                    """,
                    (
                        ArtifactObjectState.QUARANTINED.value,
                        now,
                        artifact.digest,
                        ArtifactObjectState.LIVE.value,
                    ),
                )
            if eligible:
                self._bump_generation(connection)
            return tuple(
                ArtifactObject(
                    artifact,
                    ArtifactObjectState.QUARANTINED,
                    now,
                )
                for artifact in eligible
            )

    def reconcile_quarantine(
        self,
        protected_artifacts: Iterable[ContentIdentity],
        *,
        expected_generation: int,
        minimum_age_ns: int,
        maximum_deletions: int,
        now_ns: int | None = None,
    ) -> ArtifactQuarantineReconciliation:
        expected_generation = self._validate_generation(expected_generation)
        if type(minimum_age_ns) is not int or minimum_age_ns < 0:
            raise ValueError("artifact quarantine age must be non-negative")
        if type(maximum_deletions) is not int or maximum_deletions < 0:
            raise ValueError("artifact deletion maximum must be non-negative")
        protected = {artifact.digest: artifact for artifact in self._canonical_refs(protected_artifacts)}
        restored: list[ArtifactObject] = []
        deleting: list[ArtifactObject] = []
        with self._transaction() as connection:
            now = self._advance_clock(connection, now_ns)
            self._require_generation(connection, expected_generation)
            for artifact in protected.values():
                current = self._optional_object(connection, artifact.digest)
                if current is None:
                    raise ArtifactLifecycleConflictError(
                        "protected artifact is absent from the catalog",
                        digest=artifact.digest,
                    )
                if current.artifact != artifact:
                    raise ArtifactLifecycleConflictError(
                        "protected artifact conflicts with the catalog",
                        digest=artifact.digest,
                    )
                if current.state not in (
                    ArtifactObjectState.LIVE,
                    ArtifactObjectState.QUARANTINED,
                ):
                    raise ArtifactLifecycleConflictError(
                        "non-recoverable artifact became protected",
                        digest=artifact.digest,
                        state=current.state.value,
                    )
            rows = connection.execute(
                """
                SELECT digest, size, quarantined_at_ns
                FROM objects WHERE state = ? ORDER BY digest
                """,
                (ArtifactObjectState.QUARANTINED.value,),
            ).fetchall()
            for row in rows:
                artifact = ContentIdentity(digest=row["digest"], size=row["size"])
                quarantined_at = row["quarantined_at_ns"]
                protected_ref = protected.get(artifact.digest)
                if protected_ref is not None:
                    if protected_ref != artifact:
                        raise ArtifactLifecycleConflictError(
                            "protected artifact conflicts with quarantine",
                            digest=artifact.digest,
                        )
                    connection.execute(
                        """
                        UPDATE objects
                        SET state = ?, quarantined_at_ns = NULL
                        WHERE digest = ? AND state = ?
                        """,
                        (
                            ArtifactObjectState.LIVE.value,
                            artifact.digest,
                            ArtifactObjectState.QUARANTINED.value,
                        ),
                    )
                    restored.append(ArtifactObject(artifact, ArtifactObjectState.LIVE))
                    continue
                if now - quarantined_at < minimum_age_ns:
                    continue
                if len(deleting) >= maximum_deletions:
                    continue
                reservation = connection.execute(
                    """
                    SELECT 1 FROM reservation_objects
                    WHERE digest = ? LIMIT 1
                    """,
                    (artifact.digest,),
                ).fetchone()
                if reservation is not None:
                    raise ArtifactLifecycleConflictError(
                        "quarantined artifact is reservation-owned",
                        digest=artifact.digest,
                    )
                connection.execute(
                    """
                    UPDATE objects SET state = ?
                    WHERE digest = ? AND state = ?
                    """,
                    (
                        ArtifactObjectState.DELETING.value,
                        artifact.digest,
                        ArtifactObjectState.QUARANTINED.value,
                    ),
                )
                deleting.append(
                    ArtifactObject(
                        artifact,
                        ArtifactObjectState.DELETING,
                        quarantined_at,
                    )
                )
            if restored or deleting:
                self._bump_generation(connection)
            return ArtifactQuarantineReconciliation(
                restored_objects=tuple(restored),
                deletion_candidates=tuple(deleting),
            )

    def complete_deletion(self, artifact: ContentIdentity) -> None:
        self._validate_ref(artifact)
        with self._transaction() as connection:
            current = self._object(connection, artifact.digest)
            if current.artifact != artifact or current.state != ArtifactObjectState.DELETING:
                raise ArtifactLifecycleConflictError(
                    "artifact is not an exact deleting candidate",
                    digest=artifact.digest,
                    state=current.state.value,
                )
            reservation = connection.execute(
                "SELECT 1 FROM reservation_objects WHERE digest = ? LIMIT 1",
                (artifact.digest,),
            ).fetchone()
            if reservation is not None:
                raise ArtifactLifecycleConflictError(
                    "deleting artifact is reservation-owned",
                    digest=artifact.digest,
                )
            connection.execute(
                "DELETE FROM objects WHERE digest = ?",
                (artifact.digest,),
            )
            self._bump_generation(connection)

    def _initialize(self) -> None:
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                tables = self._user_tables(connection)
                if version == 0 and not tables:
                    for statement in _SCHEMA:
                        connection.execute(statement)
                    connection.execute(
                        """
                        INSERT INTO catalog_meta (
                            singleton, format_version, hard_limit_bytes,
                            logical_now_ns, generation
                        ) VALUES (1, ?, ?, 0, 0)
                        """,
                        (_FORMAT_VERSION, self.hard_limit_bytes),
                    )
                    connection.execute(f"PRAGMA user_version = {_FORMAT_VERSION}")
                self._validate_schema(connection)
                stored_limit = connection.execute(
                    "SELECT hard_limit_bytes FROM catalog_meta WHERE singleton = 1"
                ).fetchone()[0]
                if stored_limit != self.hard_limit_bytes:
                    raise ArtifactLifecycleConflictError(
                        "artifact catalog hard limit does not match configuration",
                        stored=stored_limit,
                        configured=self.hard_limit_bytes,
                    )
                connection.commit()
        except Exception as exc:
            if isinstance(exc, SnapshotDurabilityError):
                raise
            raise SnapshotDurabilityError(
                "cannot initialize artifact lifecycle catalog",
                path=str(self.path),
                cause=exc,
            ) from exc

    def _validate_schema(self, connection: sqlite3.Connection) -> None:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if type(version) is not int or version != _FORMAT_VERSION:
            raise ArtifactLifecycleConflictError(
                "artifact lifecycle catalog format is unsupported",
                version=version,
            )
        if self._user_tables(connection) != frozenset(_EXPECTED_COLUMNS):
            raise ArtifactLifecycleConflictError("artifact lifecycle catalog tables are not canonical")
        stored_sql = {
            row["name"]: self._canonical_sql(row["sql"])
            for row in connection.execute(
                """
                SELECT name, sql FROM sqlite_schema
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                """
            )
        }
        expected_sql = {
            table: self._canonical_sql(statement) for table, statement in zip(_EXPECTED_COLUMNS, _SCHEMA, strict=True)
        }
        if stored_sql != expected_sql:
            raise ArtifactLifecycleConflictError("artifact lifecycle catalog definitions are not canonical")
        unexpected_schema = connection.execute(
            """
            SELECT name FROM sqlite_schema
            WHERE type IN ('view', 'trigger')
               OR (type = 'index' AND sql IS NOT NULL)
            LIMIT 1
            """
        ).fetchone()
        if unexpected_schema is not None:
            raise ArtifactLifecycleConflictError(
                "artifact lifecycle catalog contains unexpected schema objects",
                name=unexpected_schema["name"],
            )
        for table, expected in _EXPECTED_COLUMNS.items():
            actual = tuple(
                (row["name"], row["type"], row["notnull"], row["pk"])
                for row in connection.execute(f"PRAGMA table_info({table})")
            )
            if actual != expected:
                raise ArtifactLifecycleConflictError(
                    "artifact lifecycle catalog columns are not canonical",
                    table=table,
                )
        meta = connection.execute(
            """
            SELECT singleton, format_version, hard_limit_bytes, logical_now_ns,
                   generation
            FROM catalog_meta
            """
        ).fetchall()
        if len(meta) != 1 or meta[0]["singleton"] != 1:
            raise ArtifactLifecycleConflictError("artifact lifecycle catalog metadata is not canonical")
        if meta[0]["format_version"] != _FORMAT_VERSION:
            raise ArtifactLifecycleConflictError("artifact lifecycle catalog metadata version is unsupported")

    @contextmanager
    def _read_connection(self) -> Iterator[sqlite3.Connection]:
        with self._connect() as connection:
            try:
                yield connection
            except sqlite3.Error as exc:
                raise SnapshotDurabilityError(
                    "artifact lifecycle catalog read failed",
                    path=str(self.path),
                    cause=exc,
                ) from exc

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                if self._accounted_bytes(connection) > self.hard_limit_bytes:
                    raise ArtifactQuotaExceededError("artifact lifecycle accounting exceeds the hard limit")
                connection.commit()
            except sqlite3.Error as exc:
                connection.rollback()
                raise SnapshotDurabilityError(
                    "artifact lifecycle transaction failed",
                    path=str(self.path),
                    cause=exc,
                ) from exc
            except Exception:
                connection.rollback()
                raise

    def _connect(self) -> sqlite3.Connection:
        deadline = time.monotonic() + 30.0
        delay = 0.05
        while True:
            connection: sqlite3.Connection | None = None
            try:
                connection = sqlite3.connect(
                    self.path,
                    timeout=30.0,
                    isolation_level=None,
                )
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA busy_timeout = 30000")
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("PRAGMA synchronous = FULL")
                return connection
            except sqlite3.OperationalError as exc:
                if connection is not None:
                    connection.close()
                if "locked" not in str(exc).lower() or time.monotonic() >= deadline:
                    raise SnapshotDurabilityError(
                        "cannot open artifact lifecycle catalog",
                        path=str(self.path),
                        cause=exc,
                    ) from exc
                time.sleep(delay)
                delay = min(delay * 2, 0.5)
            except sqlite3.Error as exc:
                if connection is not None:
                    connection.close()
                raise SnapshotDurabilityError(
                    "cannot open artifact lifecycle catalog",
                    path=str(self.path),
                    cause=exc,
                ) from exc

    @staticmethod
    def _user_tables(connection: sqlite3.Connection) -> frozenset[str]:
        return frozenset(
            row["name"]
            for row in connection.execute(
                """
                SELECT name FROM sqlite_schema
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                """
            )
        )

    @staticmethod
    def _canonical_sql(value: str) -> str:
        if type(value) is not str or not value:
            raise ArtifactLifecycleConflictError("artifact lifecycle catalog definition is missing")
        return " ".join(value.split())

    @staticmethod
    def _advance_clock(
        connection: sqlite3.Connection,
        now_ns: int | None,
    ) -> int:
        supplied = time.time_ns() if now_ns is None else now_ns
        if type(supplied) is not int or supplied < 0:
            raise ValueError("artifact lifecycle time must be a non-negative integer")
        prior = connection.execute("SELECT logical_now_ns FROM catalog_meta WHERE singleton = 1").fetchone()[0]
        logical = max(prior, supplied)
        connection.execute(
            "UPDATE catalog_meta SET logical_now_ns = ? WHERE singleton = 1",
            (logical,),
        )
        return logical

    @staticmethod
    def _generation(connection: sqlite3.Connection) -> int:
        return connection.execute("SELECT generation FROM catalog_meta WHERE singleton = 1").fetchone()[0]

    @classmethod
    def _bump_generation(cls, connection: sqlite3.Connection) -> int:
        generation = cls._generation(connection) + 1
        connection.execute(
            "UPDATE catalog_meta SET generation = ? WHERE singleton = 1",
            (generation,),
        )
        return generation

    @classmethod
    def _require_generation(
        cls,
        connection: sqlite3.Connection,
        expected_generation: int,
    ) -> None:
        actual = cls._generation(connection)
        if actual != expected_generation:
            raise ArtifactCatalogGenerationConflictError(
                "artifact catalog generation changed during root scan",
                expected_generation=expected_generation,
                actual_generation=actual,
            )

    @staticmethod
    def _validate_generation(value: int) -> int:
        if type(value) is not int or value < 0:
            raise ValueError("artifact catalog generation must be non-negative")
        return value

    @staticmethod
    def _ttl_ns(ttl_seconds: float) -> int:
        if type(ttl_seconds) not in (int, float) or isinstance(ttl_seconds, bool) or ttl_seconds <= 0:
            raise ValueError("artifact reservation TTL must be positive")
        ttl_ns = int(ttl_seconds * _NANOSECONDS_PER_SECOND)
        if ttl_ns <= 0:
            raise ValueError("artifact reservation TTL is too small")
        return ttl_ns

    @staticmethod
    def _validate_bytes(value: int, field: str) -> None:
        if type(value) is not int or value < 0:
            raise ValueError(f"{field} must be a non-negative integer")

    @staticmethod
    def _validate_ref(artifact: ContentIdentity) -> None:
        if not isinstance(artifact, ContentIdentity):
            raise TypeError("artifact reference is invalid")
        if _DIGEST.fullmatch(artifact.digest) is None:
            raise ValueError("artifact digest is invalid")
        if type(artifact.size) is not int or artifact.size < 0:
            raise ValueError("artifact size is invalid")

    @classmethod
    def _canonical_refs(cls, artifacts: Iterable[ContentIdentity]) -> tuple[ContentIdentity, ...]:
        canonical: dict[str, ContentIdentity] = {}
        for artifact in artifacts:
            cls._validate_ref(artifact)
            prior = canonical.setdefault(artifact.digest, artifact)
            if prior != artifact:
                raise ValueError("artifact digest resolves to conflicting sizes")
        return tuple(canonical[digest] for digest in sorted(canonical))

    @staticmethod
    def _reservation_id(reservation: ArtifactReservation | str) -> str:
        reservation_id = reservation.reservation_id if isinstance(reservation, ArtifactReservation) else reservation
        if type(reservation_id) is not str or not reservation_id:
            raise ValueError("artifact reservation id is invalid")
        return reservation_id

    @staticmethod
    def _stage_id(stage: ArtifactStage | str) -> str:
        stage_id = stage.stage_id if isinstance(stage, ArtifactStage) else stage
        if type(stage_id) is not str or not stage_id:
            raise ValueError("artifact stage id is invalid")
        return stage_id

    @staticmethod
    def _accounted_bytes(connection: sqlite3.Connection) -> int:
        physical = connection.execute("SELECT COALESCE(SUM(size), 0) FROM objects").fetchone()[0]
        reserved = connection.execute(
            """
            SELECT COALESCE(SUM(remaining_bytes), 0) FROM reservations
            WHERE state = ?
            """,
            (ArtifactReservationState.ACTIVE.value,),
        ).fetchone()[0]
        staged = connection.execute("SELECT COALESCE(SUM(allocation_bytes), 0) FROM stages").fetchone()[0]
        return physical + reserved + staged

    @staticmethod
    def _reservation(
        connection: sqlite3.Connection,
        reservation_id: str,
    ) -> ArtifactReservation:
        row = connection.execute(
            """
            SELECT reservation_id, owner, capacity_bytes, remaining_bytes,
                   expires_at_ns, state
            FROM reservations WHERE reservation_id = ?
            """,
            (reservation_id,),
        ).fetchone()
        if row is None:
            raise ArtifactLifecycleConflictError(
                "artifact reservation does not exist",
                reservation_id=reservation_id,
            )
        return ArtifactReservation(
            reservation_id=row["reservation_id"],
            owner=row["owner"],
            capacity_bytes=row["capacity_bytes"],
            remaining_bytes=row["remaining_bytes"],
            expires_at_ns=row["expires_at_ns"],
            state=ArtifactReservationState(row["state"]),
        )

    @staticmethod
    def _stage(connection: sqlite3.Connection, stage_id: str) -> ArtifactStage:
        row = connection.execute(
            """
            SELECT stage_id, reservation_id, allocation_bytes, state,
                   digest, size
            FROM stages WHERE stage_id = ?
            """,
            (stage_id,),
        ).fetchone()
        if row is None:
            raise ArtifactLifecycleConflictError(
                "artifact stage does not exist",
                stage_id=stage_id,
            )
        artifact = None if row["digest"] is None else ContentIdentity(digest=row["digest"], size=row["size"])
        return ArtifactStage(
            stage_id=row["stage_id"],
            reservation_id=row["reservation_id"],
            allocation_bytes=row["allocation_bytes"],
            state=ArtifactStageState(row["state"]),
            artifact=artifact,
        )

    @staticmethod
    def _optional_object(
        connection: sqlite3.Connection,
        digest: str,
    ) -> ArtifactObject | None:
        row = connection.execute(
            """
            SELECT digest, size, state, quarantined_at_ns
            FROM objects WHERE digest = ?
            """,
            (digest,),
        ).fetchone()
        if row is None:
            return None
        return ArtifactObject(
            ContentIdentity(digest=row["digest"], size=row["size"]),
            ArtifactObjectState(row["state"]),
            row["quarantined_at_ns"],
        )

    @classmethod
    def _object(
        cls,
        connection: sqlite3.Connection,
        digest: str,
    ) -> ArtifactObject:
        artifact = cls._optional_object(connection, digest)
        if artifact is None:
            raise ArtifactLifecycleConflictError(
                "artifact object does not exist",
                digest=digest,
            )
        return artifact

    @staticmethod
    def _abandon_open_stage(
        connection: sqlite3.Connection,
        stage: ArtifactStage,
    ) -> None:
        connection.execute(
            """
            UPDATE reservations
            SET remaining_bytes = remaining_bytes + ?
            WHERE reservation_id = ? AND state = ?
            """,
            (
                stage.allocation_bytes,
                stage.reservation_id,
                ArtifactReservationState.ACTIVE.value,
            ),
        )
        connection.execute("DELETE FROM stages WHERE stage_id = ?", (stage.stage_id,))

    @staticmethod
    def _finish_deduplicated_stage(
        connection: sqlite3.Connection,
        stage: ArtifactStage,
        artifact: ContentIdentity,
    ) -> None:
        connection.execute(
            """
            UPDATE reservations
            SET remaining_bytes = remaining_bytes + ?
            WHERE reservation_id = ?
            """,
            (stage.allocation_bytes, stage.reservation_id),
        )
        connection.execute(
            """
            UPDATE stages
            SET allocation_bytes = 0, state = ?, digest = ?, size = ?
            WHERE stage_id = ?
            """,
            (
                ArtifactStageState.LIVE.value,
                artifact.digest,
                artifact.size,
                stage.stage_id,
            ),
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO reservation_objects (reservation_id, digest)
            VALUES (?, ?)
            """,
            (stage.reservation_id, artifact.digest),
        )

    @staticmethod
    def _clear_completed_stages(
        connection: sqlite3.Connection,
        reservation_id: str,
    ) -> None:
        connection.execute(
            """
            UPDATE objects SET stage_id = NULL
            WHERE stage_id IN (
                SELECT stage_id FROM stages WHERE reservation_id = ?
            )
            """,
            (reservation_id,),
        )
        connection.execute(
            "DELETE FROM stages WHERE reservation_id = ? AND state = ?",
            (reservation_id, ArtifactStageState.LIVE.value),
        )

    @staticmethod
    def _promote(
        connection: sqlite3.Connection,
        stage: ArtifactStage,
        artifact: ContentIdentity,
    ) -> None:
        connection.execute(
            """
            UPDATE objects SET state = ?
            WHERE digest = ? AND state = ? AND stage_id = ?
            """,
            (
                ArtifactObjectState.LIVE.value,
                artifact.digest,
                ArtifactObjectState.STAGING.value,
                stage.stage_id,
            ),
        )
        if connection.execute("SELECT changes()").fetchone()[0] != 1:
            raise ArtifactLifecycleConflictError(
                "artifact staging ownership changed",
                digest=artifact.digest,
                stage_id=stage.stage_id,
            )
        connection.execute(
            """
            INSERT OR IGNORE INTO reservation_objects (reservation_id, digest)
            VALUES (?, ?)
            """,
            (stage.reservation_id, artifact.digest),
        )
        connection.execute(
            "UPDATE stages SET state = ? WHERE stage_id = ?",
            (ArtifactStageState.LIVE.value, stage.stage_id),
        )


__all__ = [
    "ArtifactCatalogSnapshot",
    "ArtifactCatalogGenerationConflictError",
    "ArtifactLifecycleCatalog",
    "ArtifactLifecycleConflictError",
    "ArtifactLifecycleHealth",
    "ArtifactObject",
    "ArtifactObjectState",
    "ArtifactQuotaExceededError",
    "ArtifactQuarantineReconciliation",
    "ArtifactRecoveryReport",
    "ArtifactReservation",
    "ArtifactReservationState",
    "ArtifactStage",
    "ArtifactStageState",
]
