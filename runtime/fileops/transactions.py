"""Managed mutation-set transactions over sealed snapshots and durable events."""

from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from mote.contracts.content.identity import ContentIdentity
from mote.contracts.events.file.facts import (
    FileTransactionAbortedEvent,
    FileTransactionCommittedEvent,
    FileTransactionInDoubtEvent,
    FileTransactionPreparedEvent,
)
from mote.contracts.file.errors import EncodingRejectedError, RecoveryInDoubtError, StaleSnapshotError
from mote.contracts.file.identity import (
    AbsentVersion,
    FileSnapshot,
    LockMode,
    LockSpec,
    PresentVersion,
    ProjectIdentity,
)
from mote.contracts.file.mutations import CreateMutation, DeleteMutation, Mutation, MutationSet, ReplaceMutation
from mote.contracts.file.transactions import (
    FileOperationKind,
    HunkRecord,
    MutationResult,
    TransactionRecord,
    TransactionStatus,
)
from mote.runtime.artifacts.repository import ArtifactRepository as ContentRepository
from mote.runtime.fileops.control import ProjectOperationControl
from mote.runtime.fileops.encoding import decode_text, editable_text
from mote.runtime.fileops.fences import RecoveryFence
from mote.runtime.fileops.hunks import split_hunks
from mote.runtime.fileops.identity import name_identity
from mote.runtime.fileops.journal import DurableFileOperationsJournal
from mote.runtime.fileops.locking import NAME_LOCK_LEVEL, PROJECT_LOCK_LEVEL, TARGET_LOCK_LEVEL, HierarchicalLockManager
from mote.runtime.fileops.metadata_manifest import PreservedMetadata, decode_metadata_manifest
from mote.runtime.fileops.mutation.artifact_roots import ArtifactReachabilityProjector, ArtifactRoot, ArtifactRootKind
from mote.runtime.fileops.mutation.artifacts import ArtifactRepository, ArtifactWriteScope, ArtifactWriteScopeState
from mote.runtime.fileops.publisher import AtomicPublisher
from mote.runtime.fileops.resource_limits import ARTIFACT_HARD_LIMIT_BYTES
from mote.runtime.fileops.snapshots import SealedSnapshotReader

_DURABLE_PLAN_PROOF = object()


@dataclass(frozen=True, slots=True)
class ScopedMutationArtifacts:
    scope: ArtifactWriteScope

    def __post_init__(self) -> None:
        if type(self.scope) is not ArtifactWriteScope:
            raise TypeError("scoped mutation ownership requires a write scope")


@dataclass(frozen=True, slots=True)
class DurableEditPlanArtifacts:
    plan_id: str
    manifest: ContentIdentity
    closure: tuple[ContentIdentity, ...]
    _proof: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._proof is not _DURABLE_PLAN_PROOF:
            raise TypeError("durable edit plan ownership must be projected from its manifest")

    @classmethod
    def project(
        cls,
        *,
        plan_id: str,
        journal: DurableFileOperationsJournal,
        reachability: ArtifactReachabilityProjector,
    ) -> "DurableEditPlanArtifacts":
        if type(plan_id) is not str or not plan_id:
            raise ValueError("durable edit plan ownership requires a plan id")
        manifest = journal.edit_plan_manifest(plan_id)
        if manifest is None:
            raise ValueError("edit plan is not durably reachable")
        closure = reachability.close(
            (
                ArtifactRoot(
                    manifest,
                    ArtifactRootKind.EDIT_PLAN_MANIFEST,
                    plan_id,
                ),
            )
        )
        return cls(
            plan_id=plan_id,
            manifest=manifest,
            closure=closure,
            _proof=_DURABLE_PLAN_PROOF,
        )


MutationArtifactOwnership = ScopedMutationArtifacts | DurableEditPlanArtifacts


class MutationCoordinator:
    """Prepares, publishes, and reconciles managed mutation sets."""

    operation_kind = FileOperationKind.MUTATION

    def __init__(
        self,
        *,
        session_id: str,
        artifacts: ArtifactRepository,
        reader: SealedSnapshotReader,
        locks: HierarchicalLockManager,
        publisher: AtomicPublisher,
        journal: DurableFileOperationsJournal,
        control: ProjectOperationControl,
    ) -> None:
        self.session_id = session_id
        self.artifacts = artifacts
        self.reader = reader
        self.locks = locks
        self.publisher = publisher
        self.journal = journal
        self.control = control

    @contextmanager
    def lease(self, snapshots: tuple[FileSnapshot, ...]):
        if not snapshots:
            raise ValueError("mutation lease requires at least one snapshot")
        mutations = tuple(
            ReplaceMutation(
                before=snapshot,
                after=snapshot.artifact,
            )
            for snapshot in snapshots
        )
        with self.control.mutation_lease(
            projects=self._projects_for_mutations(mutations),
            specs=self._lock_specs_for_mutations(mutations),
        ):
            yield

    def commit(
        self,
        mutation_set: MutationSet,
        ownership: MutationArtifactOwnership,
        *,
        hunks: tuple[HunkRecord, ...] = (),
    ) -> MutationResult:
        if not isinstance(
            ownership,
            (ScopedMutationArtifacts, DurableEditPlanArtifacts),
        ):
            raise TypeError("mutation artifact ownership is invalid")
        if mutation_set.session_id != self.session_id:
            raise ValueError("mutation set belongs to a different session")
        existing = self.journal.get(mutation_set.transaction_id)
        if existing is not None:
            if existing.mutation_set != mutation_set:
                raise ValueError("transaction id is already bound to another mutation set")
            self._verify_artifact_ownership(mutation_set, hunks, ownership)
            self._verify_artifacts(mutation_set)
            if isinstance(ownership, ScopedMutationArtifacts):
                ownership.scope.complete(
                    durability_root=self.journal.path.parent,
                )
            if existing.status == TransactionStatus.PREPARED:
                projects = self._projects_for_mutations(mutation_set.mutations)
                for project in projects:
                    self.control.reconcile(project, label=project.key)
                existing = self.journal.get(mutation_set.transaction_id)
                if existing is None or existing.status == TransactionStatus.PREPARED:
                    return self._reconcile(existing or self._prepared_record(mutation_set))
            return self._result_from_record(existing)
        self._verify_artifact_ownership(mutation_set, hunks, ownership)
        self._verify_artifacts(mutation_set)
        specs = self._lock_specs_for_set(mutation_set)
        projects = self._projects_for_mutations(mutation_set.mutations)
        with self.control.mutation(
            projects=projects,
            specs=specs,
            transaction_id=mutation_set.transaction_id,
            session_id=self.session_id,
            journal_path=self.journal.path,
            artifact_root=self.artifacts.root,
        ):
            for mutation in mutation_set.mutations:
                self._assert_current(mutation)
            self.journal.append(
                FileTransactionPreparedEvent(
                    mutation_set=mutation_set,
                    hunks=hunks,
                )
            )
            try:
                if isinstance(ownership, ScopedMutationArtifacts):
                    ownership.scope.complete(
                        durability_root=self.journal.path.parent,
                    )
                for mutation in mutation_set.mutations:
                    self._publish_mutation(mutation_set, mutation)
                versions = self._committed_versions(mutation_set)
                self.journal.append(
                    FileTransactionCommittedEvent(
                        transaction_id=mutation_set.transaction_id,
                        paths=tuple(mutation.target_path.display for mutation in mutation_set.mutations),
                        versions=versions,
                    )
                )
                return MutationResult(
                    transaction_id=mutation_set.transaction_id,
                    status=TransactionStatus.COMMITTED,
                    versions=versions,
                )
            except Exception:
                self._resolve_prepared(mutation_set)
                raise

    @staticmethod
    def _prepared_record(mutation_set: MutationSet) -> TransactionRecord:
        return TransactionRecord(
            mutation_set=mutation_set,
            status=TransactionStatus.PREPARED,
        )

    @staticmethod
    def _result_from_record(record: TransactionRecord) -> MutationResult:
        transaction_id = record.mutation_set.transaction_id
        if record.status == TransactionStatus.IN_DOUBT:
            raise RecoveryInDoubtError(
                "edit transaction recovery is in doubt",
                transaction_id=transaction_id,
                detail=record.detail,
            )
        return MutationResult(
            transaction_id=transaction_id,
            status=record.status,
            versions=record.committed_versions,
            detail=record.detail,
        )

    def _reconcile(self, record: TransactionRecord) -> MutationResult:
        with self.locks.acquire_many(self._lock_specs_for_set(record.mutation_set)):
            return self._resolve_prepared(record.mutation_set)

    def load_recovery_record(self, fence: RecoveryFence) -> TransactionRecord | None:
        return self._journal_for_fence(fence).get(fence.transaction_id)

    @staticmethod
    def recovery_status(record: object) -> TransactionStatus:
        if not isinstance(record, TransactionRecord):
            raise TypeError("mutation recovery received a non-transaction record")
        return record.status

    @staticmethod
    def recovery_paths(record: object) -> tuple[str, ...]:
        if not isinstance(record, TransactionRecord):
            raise TypeError("mutation recovery received a non-transaction record")
        return tuple(mutation.requested_path.display for mutation in record.mutation_set.mutations)

    def reconcile_recovery_record(
        self,
        fence: RecoveryFence,
        record: object,
    ) -> TransactionStatus:
        if not isinstance(record, TransactionRecord):
            raise TypeError("mutation recovery received a non-transaction record")
        journal = self._journal_for_fence(fence)
        coordinator = self._coordinator_for_journal(
            record.mutation_set.session_id,
            journal,
            fence.artifact_root,
        )
        return coordinator._reconcile(record).status

    def finalize_recovery_record(
        self,
        fence: RecoveryFence,
        record: object,
    ) -> None:
        if not isinstance(record, TransactionRecord):
            raise TypeError("mutation recovery received a non-transaction record")
        if record.status != TransactionStatus.COMMITTED:
            return
        journal = self._journal_for_fence(fence)
        coordinator = self._coordinator_for_journal(
            record.mutation_set.session_id,
            journal,
            fence.artifact_root,
        )
        for entry in record.mutation_set.mutations:
            if not isinstance(entry, DeleteMutation):
                continue
            with self.locks.acquire_many(self._lock_specs(entry)):
                coordinator._cleanup_tombstone(
                    coordinator.publisher.tombstone_for(
                        entry.target_path,
                        record.mutation_set.transaction_id,
                    )
                )

    def _journal_for_fence(
        self,
        fence: RecoveryFence,
    ) -> DurableFileOperationsJournal:
        fence_path = os.path.abspath(fence.journal_path)
        if os.path.abspath(self.journal.path) == fence_path:
            return self.journal
        return DurableFileOperationsJournal(
            fence_path,
            session_id=fence.session_id,
            locks=self.locks,
        )

    def _coordinator_for_journal(
        self,
        session_id: str,
        journal: DurableFileOperationsJournal,
        artifact_root: str,
    ) -> "MutationCoordinator":
        if journal is self.journal:
            return self
        artifacts = ArtifactRepository(
            ContentRepository(Path(artifact_root), hard_limit_bytes=ARTIFACT_HARD_LIMIT_BYTES),
            lifecycle_root=Path(journal.path).parent / "artifact-lifecycle",
            hard_limit_bytes=ARTIFACT_HARD_LIMIT_BYTES,
        )
        return MutationCoordinator(
            session_id=session_id,
            artifacts=artifacts,
            reader=SealedSnapshotReader(artifacts),
            locks=self.locks,
            publisher=AtomicPublisher(artifacts),
            journal=journal,
            control=self.control,
        )

    def _resolve_prepared(self, mutation_set: MutationSet) -> MutationResult:
        states = tuple(self._classify_live(mutation_set, mutation) for mutation in mutation_set.mutations)
        if "other" in states:
            detail = "live mutation vector contains state outside sealed B0/B1"
            self.journal.append(FileTransactionInDoubtEvent(mutation_set.transaction_id, detail))
            return MutationResult(
                mutation_set.transaction_id,
                TransactionStatus.IN_DOUBT,
                detail=detail,
            )
        if all(state == "b1" for state in states):
            versions = self._committed_versions(mutation_set)
            self.journal.append(
                FileTransactionCommittedEvent(
                    mutation_set.transaction_id,
                    tuple(mutation.target_path.display for mutation in mutation_set.mutations),
                    versions,
                )
            )
            return MutationResult(
                mutation_set.transaction_id,
                TransactionStatus.COMMITTED,
                versions=versions,
            )
        if all(state == "b0" for state in states):
            detail = "prepared mutation set had not published"
            self.journal.append(FileTransactionAbortedEvent(mutation_set.transaction_id, detail))
            return MutationResult(
                mutation_set.transaction_id,
                TransactionStatus.ABORTED,
                detail=detail,
            )
        for index in range(len(mutation_set.mutations) - 1, -1, -1):
            if states[index] == "b1":
                self._compensate_mutation(
                    mutation_set,
                    index,
                    mutation_set.mutations[index],
                )
        restored = tuple(self._classify_live(mutation_set, mutation) for mutation in mutation_set.mutations)
        if not all(state == "b0" for state in restored):
            detail = "mutation compensation did not restore the sealed B0 vector"
            self.journal.append(FileTransactionInDoubtEvent(mutation_set.transaction_id, detail))
            return MutationResult(
                mutation_set.transaction_id,
                TransactionStatus.IN_DOUBT,
                detail=detail,
            )
        detail = "incomplete mutation set was compensated to sealed B0"
        self.journal.append(FileTransactionAbortedEvent(mutation_set.transaction_id, detail))
        return MutationResult(
            mutation_set.transaction_id,
            TransactionStatus.ABORTED,
            detail=detail,
        )

    def _publish_mutation(
        self,
        mutation_set: MutationSet,
        mutation: Mutation,
    ) -> None:
        if isinstance(mutation, DeleteMutation):
            self.publisher.delete(
                mutation.target_path,
                expected=mutation.expected_version,
                transaction_id=mutation_set.transaction_id,
            )
        else:
            self.publisher.replace_from_blob(
                mutation.target_path,
                mutation.after,
                metadata=(mutation.metadata if isinstance(mutation, CreateMutation) else mutation.before.metadata),
                expected=mutation.expected_version,
            )
        if self._classify_live(mutation_set, mutation) != "b1":
            raise StaleSnapshotError(
                "published mutation does not match its sealed B1",
                transaction_id=mutation_set.transaction_id,
                path=mutation.requested_path.display,
            )

    def _compensate_mutation(
        self,
        mutation_set: MutationSet,
        index: int,
        mutation: Mutation,
    ) -> None:
        if isinstance(mutation, DeleteMutation):
            self.publisher.restore_deleted(
                mutation.target_path,
                self.publisher.tombstone_for(
                    mutation.target_path,
                    mutation_set.transaction_id,
                ),
                expected=mutation.expected_version,
            )
            return
        live = self.reader.probe(mutation.requested_path)
        if self._classify_live(mutation_set, mutation) != "b1":
            raise StaleSnapshotError(
                "mutation changed before compensation",
                transaction_id=mutation_set.transaction_id,
                path=mutation.requested_path.display,
            )
        if isinstance(mutation, CreateMutation):
            tombstone = self.publisher.delete(
                mutation.target_path,
                expected=live.version,
                transaction_id=f"{mutation_set.transaction_id}-rollback-{index}",
            )
            self.publisher.cleanup_tombstone(tombstone)
            return
        self.publisher.replace_from_blob(
            mutation.target_path,
            mutation.before.artifact,
            metadata=mutation.before.metadata,
            expected=live.version,
        )

    def _classify_live(
        self,
        mutation_set: MutationSet,
        mutation: Mutation,
    ) -> str:
        live = self._try_snapshot(mutation)
        if live is None:
            if isinstance(mutation, CreateMutation):
                return "b0"
            if isinstance(mutation, DeleteMutation):
                tombstone = self.publisher.tombstone_for(
                    mutation.target_path,
                    mutation_set.transaction_id,
                )
                return "b1" if os.path.lexists(tombstone) else "other"
            return "other"
        if live.version.name_identity != mutation.expected_version.name_identity:
            return "other"
        if isinstance(mutation, CreateMutation):
            return (
                "b1"
                if live.version.digest == mutation.after.digest
                and self._metadata_satisfies(live.metadata, mutation.metadata)
                else "other"
            )
        if isinstance(mutation, ReplaceMutation) and (
            live.version.digest == mutation.after.digest
            and live.version.metadata_digest == mutation.before.metadata.digest
        ):
            return "b1"
        if (
            live.version.digest == mutation.before.artifact.digest
            and live.version.metadata_digest == mutation.before.metadata.digest
        ):
            return "b0"
        return "other"

    def _metadata_satisfies(
        self,
        actual_metadata: PreservedMetadata,
        policy: ContentIdentity,
    ) -> bool:
        expected_metadata = decode_metadata_manifest(self.artifacts.read_bytes(policy))
        return (
            actual_metadata.mode == expected_metadata.mode
            and actual_metadata.xattrs == expected_metadata.xattrs
            and actual_metadata.xattrs_supported == expected_metadata.xattrs_supported
            and (expected_metadata.uid is None or actual_metadata.uid == expected_metadata.uid)
            and (expected_metadata.gid is None or actual_metadata.gid == expected_metadata.gid)
        )

    def _committed_versions(
        self,
        mutation_set: MutationSet,
    ) -> tuple[AbsentVersion | PresentVersion, ...]:
        versions: list[AbsentVersion | PresentVersion] = []
        for mutation in mutation_set.mutations:
            if self._classify_live(mutation_set, mutation) != "b1":
                raise StaleSnapshotError(
                    "mutation set no longer matches its sealed B1 vector",
                    transaction_id=mutation_set.transaction_id,
                    path=mutation.requested_path.display,
                )
            if isinstance(mutation, DeleteMutation):
                versions.append(AbsentVersion(mutation.expected_version.name_identity))
            else:
                versions.append(self.reader.probe(mutation.requested_path).version)
        return tuple(versions)

    def _verify_artifacts(self, mutation_set: MutationSet) -> None:
        for mutation in mutation_set.mutations:
            if isinstance(mutation, CreateMutation):
                self.artifacts.verify(mutation.after)
                self.artifacts.verify(mutation.metadata)
            else:
                self.artifacts.verify(mutation.before.artifact)
                self.artifacts.verify(mutation.before.metadata)
                if isinstance(mutation, ReplaceMutation):
                    self.artifacts.verify(mutation.after)

    def _verify_artifact_ownership(
        self,
        mutation_set: MutationSet,
        hunks: tuple[HunkRecord, ...],
        ownership: MutationArtifactOwnership,
    ) -> None:
        if isinstance(ownership, ScopedMutationArtifacts):
            if ownership.scope.state != ArtifactWriteScopeState.ACTIVE:
                raise ValueError("mutation artifact write scope is not active")
            owned = {artifact.digest for artifact in ownership.scope.artifacts}
            required: set[str] = set()
            for mutation in mutation_set.mutations:
                if isinstance(mutation, CreateMutation):
                    required.update((mutation.after.digest, mutation.metadata.digest))
                elif isinstance(mutation, ReplaceMutation):
                    required.add(mutation.after.digest)
            for hunk in hunks:
                required.update((hunk.pre_hash, hunk.post_hash))
            missing = tuple(sorted(required - owned))
            if missing:
                raise ValueError("mutation artifacts are not owned by the active write scope")
            return
        manifest = self.journal.edit_plan_manifest(ownership.plan_id)
        if manifest != ownership.manifest:
            raise ValueError("edit plan ownership does not match its durable event")
        closed = {(artifact.digest, artifact.size) for artifact in ownership.closure}
        if (ownership.manifest.digest, ownership.manifest.size) not in closed:
            raise ValueError("edit plan ownership closure omits its manifest root")
        required_refs: list[ContentIdentity] = []
        required_digests: set[str] = set()
        for mutation in mutation_set.mutations:
            if isinstance(mutation, CreateMutation):
                required_refs.extend((mutation.after, mutation.metadata))
            else:
                required_refs.extend((mutation.before.artifact, mutation.before.metadata))
                if isinstance(mutation, ReplaceMutation):
                    required_refs.append(mutation.after)
        required_digests.update(hunk.pre_hash for hunk in hunks)
        required_digests.update(hunk.post_hash for hunk in hunks)
        if any((ref.digest, ref.size) not in closed for ref in required_refs):
            raise ValueError("mutation artifact is outside the durable edit plan closure")
        closed_digests = {digest for digest, _ in closed}
        if not required_digests.issubset(closed_digests):
            raise ValueError("review artifact is outside the durable edit plan closure")

    def _build_hunks(
        self,
        transaction_id: str,
        entry: Mutation,
        *,
        mutation_index: int,
        turn_index: int | None,
        scope: ArtifactWriteScope,
    ) -> tuple[HunkRecord, ...]:
        if turn_index is None or isinstance(entry, DeleteMutation):
            return ()
        after_raw = self.artifacts.read_bytes(entry.after)
        if isinstance(entry, CreateMutation):
            before = ""
            after = decode_text(after_raw)[0]
        else:
            decision = entry.before.encoding
            if decision is None:
                raise EncodingRejectedError(
                    "review hunks require the sealed snapshot encoding",
                    path=entry.requested_path.display,
                )
            before = self._decode_hunk_text(
                self.artifacts.read_bytes(entry.before.artifact),
                decision,
                path=entry.requested_path.display,
            )
            after = self._decode_hunk_text(
                after_raw,
                decision,
                path=entry.requested_path.display,
            )
        before = before.replace("\r\n", "\n").replace("\r", "\n")
        after = after.replace("\r\n", "\n").replace("\r", "\n")
        pre_hash = scope.put_bytes(before.encode("utf-8")).digest
        post_hash = scope.put_bytes(after.encode("utf-8")).digest
        return tuple(
            HunkRecord(
                hunk_id=f"{transaction_id}:{mutation_index}:{index}",
                path=entry.requested_path.display,
                session_id=self.session_id,
                tool_call_id="",
                turn_index=turn_index,
                source="agent",
                old_range=(hunk.old_start, hunk.old_count),
                new_range=(hunk.new_start, hunk.new_count),
                pre_hash=pre_hash,
                post_hash=post_hash,
                expected_digest=entry.after.digest,
            )
            for index, hunk in enumerate(split_hunks(before, after))
        )

    @staticmethod
    def _decode_hunk_text(raw, decision, *, path: str) -> str:
        if decision.bom and not raw.startswith(decision.bom):
            raise EncodingRejectedError(
                "review content does not preserve the sealed BOM",
                encoding=decision.label,
                path=path,
            )
        return editable_text(raw, decision).text

    def build_hunks(
        self,
        mutation_set: MutationSet,
        *,
        turn_index: int | None,
        scope: ArtifactWriteScope,
    ) -> tuple[HunkRecord, ...]:
        if turn_index is None:
            return ()
        return tuple(
            hunk
            for mutation_index, mutation in enumerate(mutation_set.mutations)
            for hunk in self._build_hunks(
                mutation_set.transaction_id,
                mutation,
                mutation_index=mutation_index,
                turn_index=turn_index,
                scope=scope,
            )
        )

    def _cleanup_tombstone(self, tombstone) -> None:
        self.publisher.cleanup_tombstone(tombstone)

    def _assert_current(self, entry: Mutation) -> None:
        if isinstance(entry.expected_version, AbsentVersion):
            if name_identity(entry.requested_path) != entry.expected_version.name_identity or os.path.lexists(
                entry.requested_path.native
            ):
                raise StaleSnapshotError(
                    f"{entry.requested_path.display} appeared before creation",
                    path=entry.requested_path.display,
                )
            return
        live = self.reader.probe(entry.requested_path)
        if live.version != entry.expected_version:
            raise StaleSnapshotError(
                f"{entry.requested_path.display} changed since the supplied snapshot",
                path=entry.requested_path.display,
            )

    def _try_snapshot(self, entry: Mutation):
        try:
            return self.reader.probe(entry.requested_path)
        except FileNotFoundError:
            return None

    @staticmethod
    def _lock_specs(entry: Mutation) -> tuple[LockSpec, ...]:
        specs = [
            LockSpec(
                PROJECT_LOCK_LEVEL,
                entry.project_identity.key,
                LockMode.SHARED,
                entry.requested_path.display,
            ),
            LockSpec(
                NAME_LOCK_LEVEL,
                entry.expected_version.name_identity.key,
                LockMode.EXCLUSIVE,
                entry.requested_path.display,
            ),
            LockSpec(
                NAME_LOCK_LEVEL,
                name_identity(entry.target_path).key,
                LockMode.EXCLUSIVE,
                entry.target_path.display,
            ),
        ]
        if isinstance(entry.expected_version, PresentVersion):
            specs.append(
                LockSpec(
                    TARGET_LOCK_LEVEL,
                    entry.expected_version.target_identity.key,
                    LockMode.EXCLUSIVE,
                    entry.target_path.display,
                )
            )
        return tuple(specs)

    @classmethod
    def _lock_specs_for_set(
        cls,
        mutation_set: MutationSet,
    ) -> tuple[LockSpec, ...]:
        return cls._lock_specs_for_mutations(mutation_set.mutations)

    @staticmethod
    def _projects_for_mutations(
        mutations: tuple[Mutation, ...],
    ) -> tuple[ProjectIdentity, ...]:
        return tuple(sorted({mutation.project_identity for mutation in mutations}))

    @classmethod
    def _lock_specs_for_mutations(
        cls,
        mutations: tuple[Mutation, ...],
    ) -> tuple[LockSpec, ...]:
        projects = cls._projects_for_mutations(mutations)
        specs = [
            LockSpec(
                PROJECT_LOCK_LEVEL,
                project.key,
                LockMode.SHARED,
                project.key,
            )
            for project in projects
        ]
        resources: dict[tuple[int, str], LockSpec] = {}
        for mutation in mutations:
            for spec in cls._lock_specs(mutation):
                if spec.level == PROJECT_LOCK_LEVEL:
                    continue
                resources[(spec.level, spec.key)] = spec
        specs.extend(resources[key] for key in sorted(resources))
        return tuple(specs)


__all__ = [
    "DurableEditPlanArtifacts",
    "MutationArtifactOwnership",
    "MutationCoordinator",
    "ScopedMutationArtifacts",
]
