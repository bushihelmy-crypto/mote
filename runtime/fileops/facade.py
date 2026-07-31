"""Session-scoped composition root for managed file operations."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

from mote.contracts.artifact import ArtifactContentRef
from mote.contracts.content.identity import ContentIdentity
from mote.contracts.events.file.facts import FileOperationsEvent
from mote.contracts.file.identity import (
    AbsentVersion,
    FileChangeAttribution,
    FileSnapshot,
    FileVersion,
    FileVersionTransition,
    LockMode,
    LockSpec,
    PathToken,
    PresentVersion,
)
from mote.contracts.file.recovery import FileOperationsHealth
from mote.contracts.file.search import SearchOutputMode, SearchResult
from mote.contracts.file.transactions import EditCommitChange, EditCommitOutcome, MutationResult, TransactionStatus
from mote.contracts.file.views import (
    ByteReadRequest,
    ByteViewMode,
    ContinueReadRequest,
    FileByteView,
    FileTextView,
    PdfReadRequest,
    PdfView,
    PdfViewMode,
    ReadCursorKind,
    ReadRequest,
    TextReadRequest,
)
from mote.runtime.artifacts.repository import ArtifactRepository as ContentRepository
from mote.runtime.fileops.byte_views import ByteViewService
from mote.runtime.fileops.candidate_discovery import CandidateDiscoveryService
from mote.runtime.fileops.capture import ManagedSnapshotCapture
from mote.runtime.fileops.control import ProjectOperationControl, RecoveryProjection
from mote.runtime.fileops.cursor_registry import DurableCursorRegistry
from mote.runtime.fileops.edit_plans import (
    AbsentEditPlanSource,
    EditPlan,
    EditPlanner,
    EditPlanRequest,
    EditPlanStore,
    ExistingEditPlanSource,
)
from mote.runtime.fileops.hunk_projection import EditPlanHunkProjector
from mote.runtime.fileops.identity import name_identity, path_token, project_identity
from mote.runtime.fileops.journal import DurableFileOperationsJournal
from mote.runtime.fileops.locking import TIMELINE_LOCK_LEVEL, HierarchicalLockManager
from mote.runtime.fileops.mutation.artifact_catalog import ArtifactObjectState
from mote.runtime.fileops.mutation.artifact_roots import ArtifactReachabilityProjector, ExternalArtifactRootSource
from mote.runtime.fileops.mutation.artifacts import ArtifactRepository, ArtifactWriteScope
from mote.runtime.fileops.mutation_factory import MutationFactory
from mote.runtime.fileops.pdf_views import PdfViewService
from mote.runtime.fileops.publisher import AtomicPublisher
from mote.runtime.fileops.read_cursors import ReadCursorStore
from mote.runtime.fileops.reservation_owners import artifact_owner
from mote.runtime.fileops.resource_limits import (
    ARTIFACT_HARD_LIMIT_BYTES,
    ARTIFACT_WRITE_TTL_SECONDS,
    MAX_MATERIALIZED_TEXT_BYTES,
    MAX_METADATA_MANIFEST_BYTES,
    MAX_READ_MANIFEST_BYTES,
    MAX_SEARCH_MANIFEST_BYTES,
    MAX_SEARCH_RESULT_BYTES,
    snapshot_budget,
)
from mote.runtime.fileops.review import ReviewService
from mote.runtime.fileops.rewind import RewindCoordinator
from mote.runtime.fileops.search import SearchEngine
from mote.runtime.fileops.snapshots import SealedSnapshotReader
from mote.runtime.fileops.text_sources import TextSourceService
from mote.runtime.fileops.text_views import TextViewService
from mote.runtime.fileops.transactions import DurableEditPlanArtifacts, MutationCoordinator, ScopedMutationArtifacts


def _directory_readable(path: Path) -> bool:
    try:
        with os.scandir(path) as entries:
            next(entries, None)
        return True
    except OSError:
        return False


def _directory_fsync_writable(path: Path) -> bool:
    fd = -1
    probe = None
    try:
        fd, raw_path = tempfile.mkstemp(prefix=".mote-health-", dir=path)
        probe = Path(raw_path)
        os.write(fd, b"health")
        os.fsync(fd)
        os.close(fd)
        fd = -1
        probe.unlink()
        probe = None
        directory_fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return True
    except OSError:
        return False
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        if probe is not None:
            try:
                probe.unlink()
            except OSError:
                pass


class FileOperations:
    """Owns one session's snapshots, observed versions, and mutation protocol."""

    def __init__(
        self,
        *,
        session_id: str,
        journal_path: Path,
        get_project_root: Callable[[], str],
        artifact_repository: ContentRepository,
        artifact_lifecycle_root: Path,
        flush_pending: Optional[Callable[[], None]] = None,
        lock_root: Optional[Path] = None,
        event_sink: Callable[[FileOperationsEvent], object] | None = None,
        event_source: Callable[[], Iterable[FileOperationsEvent]] | None = None,
    ) -> None:
        journal_path = Path(journal_path)
        self.session_id = session_id
        self._get_project_root = get_project_root
        artifacts = ArtifactRepository(
            artifact_repository,
            lifecycle_root=artifact_lifecycle_root,
            hard_limit_bytes=ARTIFACT_HARD_LIMIT_BYTES,
        )
        reader = SealedSnapshotReader(artifacts)
        locks = HierarchicalLockManager(lock_root)
        control = ProjectOperationControl(locks)
        journal = DurableFileOperationsJournal(
            journal_path,
            session_id=session_id,
            locks=locks,
            flush_pending=flush_pending,
            event_sink=event_sink,
            event_source=event_source,
        )
        self.artifacts = artifacts
        self.reader = reader
        self.locks = locks
        self.control = control
        self.journal = journal
        self.cursor_registry = DurableCursorRegistry(journal_path.parent / "cursor-registry.sqlite3")
        self.cursor_registry.synchronize(journal.timeline_epoch())
        self._timeline_spec = LockSpec(
            TIMELINE_LOCK_LEVEL,
            session_id,
            LockMode.SHARED,
            f"session timeline {session_id}",
        )
        self.review = ReviewService(
            session_id=session_id,
            journal=journal,
        )
        self.capture_service = ManagedSnapshotCapture(
            reader=reader,
            control=control,
            get_project_root=get_project_root,
        )
        self.read_cursors = ReadCursorStore(artifacts, self.cursor_registry)
        self.mutations = MutationCoordinator(
            session_id=session_id,
            artifacts=artifacts,
            reader=reader,
            locks=locks,
            publisher=AtomicPublisher(repository=artifacts),
            journal=journal,
            control=control,
        )
        self.mutation_factory = MutationFactory(
            session_id=session_id,
            artifacts=artifacts,
            get_project_root=get_project_root,
        )
        self.edit_plan_hunks = EditPlanHunkProjector(
            session_id=session_id,
            artifacts=artifacts,
        )
        self.rewinds = RewindCoordinator(
            session_id=session_id,
            git_dir=journal_path.parent / "git",
            locks=locks,
            journal=journal,
            control=control,
            timeline=self.cursor_registry,
        )
        self.text_sources = TextSourceService(
            artifacts=artifacts,
            capture=self.capture_service,
        )
        self.candidate_discovery = CandidateDiscoveryService()
        self.edit_plan_store = EditPlanStore(
            artifacts=artifacts,
            journal=journal,
            session_id=session_id,
        )
        self.artifact_reachability = ArtifactReachabilityProjector(
            repository=artifacts,
            edit_plans=self.edit_plan_store,
            journal=journal,
        )
        self.edit_planner = EditPlanner(
            artifacts=artifacts,
            sources=self.text_sources,
            discovery=self.candidate_discovery,
            store=self.edit_plan_store,
            resolve_observed=self._observed_snapshot,
            mutation_factory=self.mutation_factory,
        )
        self.search_engine = SearchEngine(
            artifacts=artifacts,
            sources=self.text_sources,
            discovery=self.candidate_discovery,
            cursors=self.cursor_registry,
        )
        self.byte_views = ByteViewService(
            artifacts=artifacts,
            capture=self.capture_service,
            cursors=self.read_cursors,
        )
        self.pdf_views = PdfViewService(
            artifacts=artifacts,
            capture=self.capture_service,
            cursors=self.read_cursors,
        )
        self.text_views = TextViewService(
            artifacts=artifacts,
            cursors=self.read_cursors,
            sources=self.text_sources,
        )
        control.register(self.mutations)
        control.register(self.rewinds)

    def register_artifact_root_source(self, source: ExternalArtifactRootSource) -> None:
        """Protect a durable shared-CAS authority from FileOps collection."""
        self.artifact_reachability.register_root_source(source)

    def artifact_roots(self) -> tuple[ArtifactContentRef, ...]:
        """Return the FileOps roots that protect objects in the shared CAS."""
        referenced = list(self._artifact_root_refs())
        referenced.extend(
            item.artifact
            for item in self.artifacts.catalog.gc_snapshot().objects
            if item.state is not ArtifactObjectState.DELETING
        )
        roots = {
            ref.digest: ArtifactContentRef(
                identity=ref,
                locator=f"sha256:{ref.digest}",
            )
            for ref in referenced
        }
        return tuple(roots[digest] for digest in sorted(roots))

    def prune_artifact_metadata(self, _reachable: Sequence[ArtifactContentRef]) -> None:
        """Reconcile FileOps lifecycle metadata with shared-CAS reachability."""
        protected_refs = self._artifact_root_refs()
        protected = {ref.digest for ref in protected_refs}
        catalog = self.artifacts.catalog
        snapshot = catalog.gc_snapshot()
        candidates = tuple(
            item.artifact
            for item in snapshot.objects
            if item.state is ArtifactObjectState.LIVE and item.artifact.digest not in protected
        )
        catalog.quarantine_unreachable(candidates, expected_generation=snapshot.generation)
        current = catalog.gc_snapshot()
        reconciled = catalog.reconcile_quarantine(
            protected_refs,
            expected_generation=current.generation,
            minimum_age_ns=0,
            maximum_deletions=max(1, len(current.objects)),
        )
        for item in reconciled.deletion_candidates:
            catalog.complete_deletion(item.artifact)

    def _artifact_root_refs(self) -> tuple[ContentIdentity, ...]:
        referenced = list(self.artifact_reachability.scan().artifacts)
        with self.cursor_registry.freeze_pins() as pins:
            referenced.extend(pins.artifacts)
        return tuple(referenced)

    def read_view(
        self,
        path: str,
        request: ReadRequest,
    ) -> FileByteView | FileTextView | PdfView:
        with self.locks.acquire_many((self._timeline_spec,)):
            epoch = self.cursor_registry.synchronize(self.journal.timeline_epoch()).epoch
            if isinstance(request, ContinueReadRequest):
                return self._read_view_at_epoch(
                    path,
                    request,
                    expected_epoch=epoch,
                    scope=None,
                )
            source_bytes = os.stat(path).st_size
            maximum_bytes = snapshot_budget(source_bytes) + MAX_READ_MANIFEST_BYTES
            if isinstance(request, TextReadRequest):
                maximum_bytes += MAX_MATERIALIZED_TEXT_BYTES
            with self.artifacts.write_scope(
                owner=self._artifact_owner("read", path),
                maximum_bytes=maximum_bytes,
                ttl_seconds=ARTIFACT_WRITE_TTL_SECONDS,
            ) as scope:
                return self._read_view_at_epoch(
                    path,
                    request,
                    expected_epoch=epoch,
                    scope=scope,
                )

    def _read_view_at_epoch(
        self,
        path: str,
        request: ReadRequest,
        *,
        expected_epoch: int,
        scope: ArtifactWriteScope | None,
    ) -> FileByteView | FileTextView | PdfView:
        if isinstance(request, TextReadRequest):
            return self.text_views.read(
                path,
                offset=request.offset,
                limit=request.limit,
                encoding=request.encoding,
                fallback_encoding=request.fallback_encoding,
                scope=scope,
                expected_epoch=expected_epoch,
            )
        if isinstance(request, ByteReadRequest):
            return self.byte_views.read(
                path,
                mode=request.mode,
                offset=request.offset,
                limit=request.limit,
                scope=scope,
                expected_epoch=expected_epoch,
            )
        if isinstance(request, PdfReadRequest):
            return self.pdf_views.read(
                path,
                mode=request.mode,
                pages=request.pages,
                dpi=request.dpi,
                limit=request.limit,
                scope=scope,
                expected_epoch=expected_epoch,
            )

        continuation = self.read_cursors.open(request.cursor)
        if continuation.kind == ReadCursorKind.TEXT:
            return self.text_views.read(
                path,
                limit=request.limit,
                continuation=continuation,
                expected_epoch=expected_epoch,
            )
        if continuation.kind in (ReadCursorKind.RAW, ReadCursorKind.HEX):
            return self.byte_views.read(
                path,
                mode=(ByteViewMode.RAW if continuation.kind == ReadCursorKind.RAW else ByteViewMode.HEX),
                limit=request.limit,
                continuation=continuation,
                expected_epoch=expected_epoch,
            )
        return self.pdf_views.read(
            path,
            mode=(PdfViewMode.TEXT if continuation.kind == ReadCursorKind.PDF_TEXT else PdfViewMode.RENDER),
            limit=request.limit,
            continuation=continuation,
            expected_epoch=expected_epoch,
        )

    def search(
        self,
        *,
        root: str,
        content: str = "",
        files: str = "",
        type_name: str = "",
        output_mode: SearchOutputMode = SearchOutputMode.FILES_WITH_MATCHES,
        case_insensitive: bool = False,
        before_context: int = 0,
        after_context: int = 0,
        multiline: bool = False,
        encoding: Optional[str] = None,
        fallback_encoding: Optional[str] = None,
        limit: Optional[int] = None,
        offset: int = 0,
        cursor: Optional[str] = None,
        timeout: float = 20.0,
    ) -> SearchResult:
        with self.locks.acquire_many((self._timeline_spec,)):
            epoch = self.cursor_registry.synchronize(self.journal.timeline_epoch()).epoch
            arguments = dict(
                root=root,
                content=content,
                files=files,
                type_name=type_name,
                output_mode=output_mode,
                case_insensitive=case_insensitive,
                before_context=before_context,
                after_context=after_context,
                multiline=multiline,
                encoding=encoding,
                fallback_encoding=fallback_encoding,
                limit=limit,
                offset=offset,
                cursor=cursor,
                timeout=timeout,
                expected_epoch=epoch,
            )
            if cursor:
                return self.search_engine.search(**arguments, scope=None)
            with self.artifacts.write_scope(
                owner=self._artifact_owner("search", root),
                maximum_bytes=(MAX_SEARCH_RESULT_BYTES + MAX_SEARCH_MANIFEST_BYTES),
                ttl_seconds=ARTIFACT_WRITE_TTL_SECONDS,
            ) as scope:
                return self.search_engine.search(**arguments, scope=scope)

    def capture(
        self,
        path: str,
        *,
        encoding: Optional[str] = None,
        fallback_encoding: Optional[str] = None,
    ) -> tuple[FileSnapshot, bytes]:
        with self.locks.acquire_many((self._timeline_spec,)):
            epoch = self.cursor_registry.synchronize(self.journal.timeline_epoch()).epoch
            source_bytes = os.stat(path).st_size
            with self.artifacts.write_scope(
                owner=self._artifact_owner("capture", path),
                maximum_bytes=snapshot_budget(source_bytes),
                ttl_seconds=ARTIFACT_WRITE_TTL_SECONDS,
            ) as scope:
                snapshot = self.capture_service.capture(
                    path,
                    scope=scope,
                    encoding=encoding,
                    fallback_encoding=fallback_encoding,
                )
                self.artifacts.verify(snapshot.artifact)
                self.artifacts.verify(snapshot.metadata)
                self.cursor_registry.observe(snapshot, expected_epoch=epoch)
                scope.complete(durability_root=self.cursor_registry.path.parent)
                return snapshot, self.artifacts.read_bytes(snapshot.artifact)

    def plan_file_edit(self, request: EditPlanRequest) -> EditPlan:
        return self.edit_planner.plan(request)

    def commit_edit_plan(
        self,
        plan_id: str,
        *,
        review_turn_index: int | None = None,
    ) -> EditCommitOutcome:
        plan = self.edit_plan_store.load(plan_id)
        result = self.mutations.commit(
            plan.mutation_set,
            DurableEditPlanArtifacts.project(
                plan_id=plan.plan_id,
                journal=self.journal,
                reachability=self.artifact_reachability,
            ),
            hunks=self.edit_plan_hunks.project(
                plan.mutation_set,
                plan.review_facts,
                turn_index=review_turn_index,
            ),
        )
        if result.status != TransactionStatus.COMMITTED:
            return EditCommitOutcome(result=result)
        self._observe_edit_plan_commit(plan, result)
        changes = tuple(
            EditCommitChange(
                path=fact.path,
                old=self.artifacts.read_bytes(fact.before_utf8).decode("utf-8", errors="strict"),
                new=self.artifacts.read_bytes(fact.after_utf8).decode("utf-8", errors="strict"),
                post_digest=version.digest,
            )
            for fact, version in zip(
                plan.review_facts,
                result.versions,
                strict=True,
            )
        )
        return EditCommitOutcome(result=result, changes=changes)

    def commit_generated_files(
        self,
        files: dict[str, bytes],
        *,
        source: str,
    ) -> MutationResult:
        """Atomically create or replace generated binary files."""
        if not files:
            raise ValueError("generated file batch must be non-empty")
        snapshots = {path: self.capture(path)[0] for path in files if os.path.lexists(path)}
        maximum_bytes = sum(len(content) for content in files.values()) + (
            (len(files) - len(snapshots)) * MAX_METADATA_MANIFEST_BYTES
        )
        with self.artifacts.write_scope(
            owner=self._artifact_owner("generated-files", source),
            maximum_bytes=maximum_bytes,
            ttl_seconds=ARTIFACT_WRITE_TTL_SECONDS,
        ) as scope:
            mutations = tuple(
                self.mutation_factory.replacement(
                    snapshots[path],
                    content,
                    scope=scope,
                )
                if path in snapshots
                else self.mutation_factory.creation(path, content, scope=scope)
                for path, content in files.items()
            )
            return self.mutations.commit(
                self.mutation_factory.mutation_set(
                    source=source,
                    mutations=mutations,
                ),
                ScopedMutationArtifacts(scope),
            )

    def _observe_edit_plan_commit(
        self,
        plan: EditPlan,
        result: MutationResult,
    ) -> None:
        for source, mutation, version in zip(
            plan.sources,
            plan.mutation_set.mutations,
            result.versions,
            strict=True,
        ):
            if not isinstance(version, PresentVersion):
                raise ValueError("edit plan committed a non-present file version")
            if isinstance(source, ExistingEditPlanSource):
                encoding = source.snapshot.encoding
            elif isinstance(source, AbsentEditPlanSource):
                encoding = source.encoding
            else:
                raise TypeError("edit plan source is invalid")
            observed, _ = self.capture(
                mutation.requested_path.display,
                encoding=encoding.label if encoding is not None else None,
            )
            if observed.version != version:
                raise ValueError("committed edit changed before its observed snapshot was sealed")

    def observe(self, snapshot: FileSnapshot) -> None:
        self.artifacts.verify(snapshot.artifact)
        self.artifacts.verify(snapshot.metadata)
        with self.locks.acquire_many((self._timeline_spec,)):
            epoch = self.cursor_registry.synchronize(self.journal.timeline_epoch()).epoch
            self.cursor_registry.observe(snapshot, expected_epoch=epoch)

    def observed(self, path: str) -> Optional[FileSnapshot]:
        return self._observed_snapshot(path_token(path))

    def _observed_snapshot(self, path: PathToken) -> Optional[FileSnapshot]:
        with self.locks.acquire_many((self._timeline_spec,)):
            epoch = self.cursor_registry.synchronize(self.journal.timeline_epoch()).epoch
            snapshot = self.cursor_registry.observed(path, expected_epoch=epoch)
            if snapshot is not None:
                self.artifacts.verify(snapshot.artifact)
                self.artifacts.verify(snapshot.metadata)
            return snapshot

    def observed_versions(self) -> dict[str, object]:
        with self.locks.acquire_many((self._timeline_spec,)):
            epoch = self.cursor_registry.synchronize(self.journal.timeline_epoch()).epoch
            return {
                snapshot.requested_path.display: snapshot.version
                for snapshot in self.cursor_registry.observations(expected_epoch=epoch)
            }

    def probe_file_version(
        self,
        path: str,
        *,
        prior: Optional[FileVersion] = None,
    ) -> FileVersion:
        """Probe a stable exact version through the managed snapshot reader."""
        try:
            return self.capture_service.probe(path).version
        except (FileNotFoundError, NotADirectoryError):
            if prior is not None:
                return AbsentVersion(prior.name_identity)
            return AbsentVersion(name_identity(path))

    def invalidate_external_change(
        self,
        path: str,
        *,
        prior: FileVersion,
        current: FileVersion,
    ) -> None:
        """Fence cursors and the stale observation for one exact transition."""
        if prior == current:
            raise ValueError("external file change must transition versions")
        if prior.name_identity != current.name_identity:
            raise ValueError("external file change crossed name identities")
        with self.locks.acquire_many((self._timeline_spec,)):
            self.cursor_registry.synchronize(self.journal.timeline_epoch())
            self.cursor_registry.invalidate_observation(path_token(path))

    def classify_transitions(
        self,
        transitions: tuple[FileVersionTransition, ...],
    ) -> tuple[FileChangeAttribution, ...]:
        """Classify a batch from one durable journal projection scan."""
        requested_paths = {path_token(transition.path) for transition in transitions}
        committed_by_path: dict[PathToken, list[tuple[FileVersion, FileVersion]]] = {
            path: [] for path in requested_paths
        }
        for record in self.journal.records():
            if record.status != TransactionStatus.COMMITTED:
                continue
            for mutation, committed in zip(
                record.mutation_set.mutations,
                record.committed_versions,
                strict=True,
            ):
                projected = committed_by_path.get(mutation.requested_path)
                if projected is not None:
                    projected.append((mutation.expected_version, committed))

        classifications = []
        for transition in transitions:
            reachable = {transition.prior}
            for expected, committed in committed_by_path[path_token(transition.path)]:
                if expected in reachable:
                    reachable.add(committed)
            classifications.append(
                FileChangeAttribution.MANAGED if transition.current in reachable else FileChangeAttribution.EXTERNAL
            )
        return tuple(classifications)

    def health(self) -> FileOperationsHealth:
        journal_readable = True
        cursor_registry_readable = True
        cursor_health = None
        artifact_health = None
        artifact_catalog_readable = True
        try:
            self.journal.records()
        except Exception:
            journal_readable = False
        try:
            cursor_health = self.cursor_registry.health()
        except Exception:
            cursor_registry_readable = False
        try:
            artifact_health = self.artifacts.catalog.health()
        except Exception:
            artifact_catalog_readable = False
        journal_parent = self.journal.path.parent
        artifact_root = self.artifacts.root
        project_root = self._get_project_root()
        projection = RecoveryProjection(0, (), ())
        try:
            projection = self.control.recovery_projection(
                project_identity(project_root),
                project_path=project_root,
            )
        except Exception:
            journal_readable = False
        return FileOperationsHealth(
            lock_backend=self.locks.backend_name,
            journal_readable=journal_readable,
            journal_writable=_directory_fsync_writable(journal_parent),
            artifact_readable=_directory_readable(artifact_root),
            artifact_writable=_directory_fsync_writable(artifact_root),
            artifact_catalog_readable=artifact_catalog_readable,
            recovery_backlog=projection.backlog,
            in_doubt_transactions=projection.in_doubt_transactions,
            affected_paths=projection.affected_paths,
            cursor_registry_readable=cursor_registry_readable,
            timeline_epoch=(cursor_health.timeline.epoch if cursor_health is not None else 0),
            active_cursor_leases=(cursor_health.active_leases if cursor_health is not None else 0),
            expired_cursor_leases=(cursor_health.expired_leases if cursor_health is not None else 0),
            pinned_artifacts=(cursor_health.pinned_artifacts if cursor_health is not None else 0),
            pinned_bytes=(cursor_health.pinned_bytes if cursor_health is not None else 0),
            nearest_cursor_expiry_ns=(cursor_health.nearest_expiry_ns if cursor_health is not None else None),
            observed_snapshots=(cursor_health.observed_snapshots if cursor_health is not None else 0),
            artifact_hard_limit_bytes=(artifact_health.hard_limit_bytes if artifact_health is not None else 0),
            artifact_physical_bytes=(artifact_health.physical_bytes if artifact_health is not None else 0),
            artifact_reserved_bytes=(artifact_health.reserved_bytes if artifact_health is not None else 0),
            artifact_staged_bytes=(artifact_health.staged_allocation_bytes if artifact_health is not None else 0),
            artifact_active_reservations=(artifact_health.active_reservations if artifact_health is not None else 0),
            artifact_open_stages=(artifact_health.open_stages if artifact_health is not None else 0),
            artifact_catalog_generation=(artifact_health.generation if artifact_health is not None else 0),
            artifact_staging_objects=(artifact_health.staging_objects if artifact_health is not None else 0),
            artifact_quarantined_objects=(artifact_health.quarantined_objects if artifact_health is not None else 0),
            artifact_deleting_objects=(artifact_health.deleting_objects if artifact_health is not None else 0),
            artifact_quota_pressure=(artifact_health.quota_pressure if artifact_health is not None else 0.0),
        )

    def rewind(
        self,
        *,
        working_dir: str,
        target_commit: str,
        parent_commit: str | None,
        prompt_index: int,
        after_commit: str = "",
    ):
        result = self.rewinds.rewind(
            working_dir=working_dir,
            target_commit=target_commit,
            parent_commit=parent_commit,
            prompt_index=prompt_index,
            after_commit=after_commit,
        )
        return result

    def capture_worktree_checkpoint(
        self,
        *,
        working_dir: str,
        parent_commit: str | None,
        message: str,
    ) -> str:
        return self.rewinds.capture_checkpoint(
            working_dir=working_dir,
            parent_commit=parent_commit,
            message=message,
        )

    def invalidate(self, path: Optional[str] = None) -> None:
        with self.locks.acquire_many((self._timeline_spec,)):
            epoch = self.cursor_registry.synchronize(self.journal.timeline_epoch()).epoch
            if path is None:
                self.cursor_registry.forget_all(expected_epoch=epoch)
            else:
                self.cursor_registry.forget(
                    path_token(path),
                    expected_epoch=epoch,
                )

    def reconcile(self) -> None:
        project_root = self._get_project_root()
        self.control.reconcile(
            project_identity(project_root),
            label=project_root,
        )

    def _artifact_owner(
        self,
        kind: str,
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
    ) -> str:
        identity = self.session_id.encode("utf-8", errors="strict") + b"\0"
        identity += os.fsencode(path)
        return artifact_owner(kind, identity)


__all__ = ["FileOperations"]
