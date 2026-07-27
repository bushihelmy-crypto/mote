"""Single admission and recovery control plane for project file operations."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Protocol

from mote.contracts.fileops.errors import RecoveryInDoubtError
from mote.contracts.fileops.models import FileOperationKind, LockMode, LockSpec, ProjectIdentity, TransactionStatus
from mote.runtime.fileops.fences import ProjectRecoveryFenceStore, RecoveryFence
from mote.runtime.fileops.locking import PROJECT_LOCK_LEVEL, TIMELINE_LOCK_LEVEL, HierarchicalLockManager


class OperationRecoveryHandler(Protocol):
    operation_kind: FileOperationKind

    def load_recovery_record(self, fence: RecoveryFence) -> object | None:
        ...

    def recovery_status(self, record: object) -> TransactionStatus:
        ...

    def recovery_paths(self, record: object) -> tuple[str, ...]:
        ...

    def finalize_recovery_record(
        self,
        fence: RecoveryFence,
        record: object,
    ) -> None:
        ...

    def reconcile_recovery_record(
        self,
        fence: RecoveryFence,
        record: object,
    ) -> TransactionStatus:
        ...


@dataclass(frozen=True)
class RecoveryProjection:
    backlog: int
    in_doubt_transactions: tuple[str, ...]
    affected_paths: tuple[str, ...]


class ProjectOperationControl:
    """Owns project locks, durable reservations, and recovery dispatch."""

    def __init__(self, locks: HierarchicalLockManager) -> None:
        self.locks = locks
        self._fences = ProjectRecoveryFenceStore(locks.root)
        self._handlers: dict[FileOperationKind, OperationRecoveryHandler] = {}

    def register(self, handler: OperationRecoveryHandler) -> None:
        operation = handler.operation_kind
        if operation in self._handlers:
            raise ValueError(f"duplicate file operation recovery handler: {operation}")
        self._handlers[operation] = handler

    @contextmanager
    def mutation(
        self,
        *,
        projects: tuple[ProjectIdentity, ...],
        specs: tuple[LockSpec, ...],
        transaction_id: str,
        session_id: str,
        journal_path: Path,
        artifact_root: Path,
    ) -> Iterator[None]:
        self._validate_mutation_scope(projects, specs)
        resources = self._resource_keys(specs)
        fence = RecoveryFence.create(
            transaction_id=transaction_id,
            session_id=session_id,
            journal_path=str(journal_path.absolute()),
            operation=FileOperationKind.MUTATION,
            artifact_root=str(artifact_root.absolute()),
            project_mode=LockMode.SHARED,
            projects=projects,
            resource_keys=resources,
        )
        with self._scoped_lease(projects, specs, resources, fence=fence):
            yield

    @contextmanager
    def mutation_lease(
        self,
        *,
        projects: tuple[ProjectIdentity, ...],
        specs: tuple[LockSpec, ...],
    ) -> Iterator[None]:
        self._validate_mutation_scope(projects, specs)
        resources = self._resource_keys(specs)
        with self._scoped_lease(projects, specs, resources, fence=None):
            yield

    @contextmanager
    def capture_lease(
        self,
        *,
        project: ProjectIdentity,
        label: str,
    ) -> Iterator[None]:
        project_lock = self._project_lock(project, LockMode.SHARED, label)
        while True:
            conflicts: tuple[str, ...]
            with self.locks.acquire_many((project_lock,)):
                conflicts = tuple(
                    fence.transaction_id
                    for fence in self._fences.list(project)
                    if fence.project_mode == LockMode.EXCLUSIVE
                )
                if not conflicts:
                    yield
                    return
            self.reconcile(project, label=label, transaction_ids=conflicts)

    @contextmanager
    def project_operation(
        self,
        *,
        project: ProjectIdentity,
        label: str,
        transaction_id: str,
        session_id: str,
        journal_path: Path,
        operation: FileOperationKind,
        artifact_root: Path | None = None,
    ) -> Iterator[None]:
        if operation not in self._handlers:
            raise ValueError(f"unregistered project operation: {operation}")
        self.reconcile(project, label=label)
        specs = (
            self._timeline_lock(session_id, LockMode.EXCLUSIVE),
            self._project_lock(project, LockMode.EXCLUSIVE, label),
        )
        with self.locks.acquire_many(specs):
            fence = RecoveryFence.create(
                transaction_id=transaction_id,
                session_id=session_id,
                journal_path=str(journal_path.absolute()),
                operation=operation,
                artifact_root=("" if artifact_root is None else str(artifact_root.absolute())),
                project_mode=LockMode.EXCLUSIVE,
                projects=(project,),
            )
            self._fences.put(project, fence)
            try:
                yield
            finally:
                self._clear_if_resolved(fence)

    @contextmanager
    def project_lease(
        self,
        *,
        project: ProjectIdentity,
        label: str,
    ) -> Iterator[None]:
        self.reconcile(project, label=label)
        project_lock = self._project_lock(project, LockMode.EXCLUSIVE, label)
        with self.locks.acquire_many((project_lock,)):
            yield

    def reconcile(
        self,
        project: ProjectIdentity,
        *,
        label: str,
        transaction_ids: tuple[str, ...] | None = None,
    ) -> None:
        project_lock = self._project_lock(project, LockMode.EXCLUSIVE, label)
        with self.locks.acquire_many((project_lock,)):
            selected = None if transaction_ids is None else frozenset(transaction_ids)
            fences = tuple(
                fence for fence in self._fences.list(project) if selected is None or fence.transaction_id in selected
            )
        in_doubt = tuple(
            fence.transaction_id for fence in fences if self._reconcile_fence(fence) == TransactionStatus.IN_DOUBT
        )
        if in_doubt:
            raise RecoveryInDoubtError(
                "project operation recovery is in doubt",
                project_identity=project.key,
                transaction_ids=in_doubt,
            )

    def records(
        self,
        project: ProjectIdentity,
    ) -> tuple[tuple[RecoveryFence, object | None], ...]:
        records: list[tuple[RecoveryFence, object | None]] = []
        for fence in self._fences.list(project):
            handler = self._handler(fence)
            records.append((fence, handler.load_recovery_record(fence)))
        return tuple(records)

    def reservations(self, project: ProjectIdentity) -> tuple[RecoveryFence, ...]:
        return self._fences.list(project)

    def recovery_projection(
        self,
        project: ProjectIdentity,
        *,
        project_path: str,
    ) -> RecoveryProjection:
        backlog = 0
        in_doubt: list[str] = []
        affected: set[str] = set()
        for fence, record in self.records(project):
            if record is None:
                backlog += 1
                affected.add(project_path)
                continue
            handler = self._handler(fence)
            status = handler.recovery_status(record)
            if status == TransactionStatus.PREPARED:
                backlog += 1
            elif status == TransactionStatus.IN_DOUBT:
                in_doubt.append(fence.transaction_id)
            if status in {TransactionStatus.PREPARED, TransactionStatus.IN_DOUBT}:
                affected.update(handler.recovery_paths(record))
        return RecoveryProjection(
            backlog=backlog,
            in_doubt_transactions=tuple(in_doubt),
            affected_paths=tuple(sorted(affected)),
        )

    @contextmanager
    def _scoped_lease(
        self,
        projects: tuple[ProjectIdentity, ...],
        specs: tuple[LockSpec, ...],
        resources: tuple[str, ...],
        *,
        fence: RecoveryFence | None,
    ) -> Iterator[None]:
        label = next((spec.label for spec in specs if spec.label), projects[0].key)
        while True:
            conflicts: tuple[str, ...]
            with self.locks.acquire_many(specs):
                conflicts = tuple(
                    sorted(
                        {
                            transaction_id
                            for project in projects
                            for transaction_id in self._conflicts(project, resources)
                        }
                    )
                )
                if not conflicts:
                    if fence is not None:
                        for project in projects:
                            self._fences.put(project, fence)
                    try:
                        yield
                    finally:
                        if fence is not None:
                            self._clear_if_resolved(fence)
                    return
            for project in projects:
                self.reconcile(
                    project,
                    label=label,
                    transaction_ids=conflicts,
                )

    def _reconcile_fence(self, fence: RecoveryFence) -> TransactionStatus:
        project_locks = tuple(
            self._project_lock(project, LockMode.EXCLUSIVE, project.key) for project in fence.projects
        )
        coordination_locks = (
            (self._timeline_lock(fence.session_id, LockMode.EXCLUSIVE),)
            if fence.operation == FileOperationKind.REWIND
            else ()
        )
        with self.locks.acquire_many(coordination_locks + project_locks):
            replicas = tuple(self._fences.get(project, fence.transaction_id) for project in fence.projects)
            present = tuple(replica for replica in replicas if replica is not None)
            if not present:
                return TransactionStatus.ABORTED
            if any(replica != fence for replica in present):
                raise RecoveryInDoubtError(
                    "multi-project recovery fence replicas disagree",
                    transaction_id=fence.transaction_id,
                )
            handler = self._handler(fence)
            record = handler.load_recovery_record(fence)
            if record is None:
                for project in fence.projects:
                    self._fences.clear(project, fence.transaction_id)
                return TransactionStatus.ABORTED
            if len(present) != len(fence.projects):
                raise RecoveryInDoubtError(
                    "prepared multi-project recovery fence is incomplete",
                    transaction_id=fence.transaction_id,
                )
            status = handler.recovery_status(record)
            if status == TransactionStatus.PREPARED:
                status = handler.reconcile_recovery_record(fence, record)
                durable_record = handler.load_recovery_record(fence)
                if durable_record is None:
                    raise RecoveryInDoubtError(
                        "reconciled project operation disappeared from its journal",
                        transaction_id=fence.transaction_id,
                    )
                durable_status = handler.recovery_status(durable_record)
                if durable_status != status:
                    raise RecoveryInDoubtError(
                        "reconciled project operation status is not durable",
                        transaction_id=fence.transaction_id,
                        reported_status=status.value,
                        durable_status=durable_status.value,
                    )
                record = durable_record
            if status in {TransactionStatus.COMMITTED, TransactionStatus.ABORTED}:
                handler.finalize_recovery_record(fence, record)
                for project in fence.projects:
                    self._fences.clear(project, fence.transaction_id)
            return status

    def _clear_if_resolved(self, fence: RecoveryFence) -> None:
        handler = self._handler(fence)
        record = handler.load_recovery_record(fence)
        if record is None:
            for project in fence.projects:
                self._fences.clear(project, fence.transaction_id)
            return
        if handler.recovery_status(record) in {
            TransactionStatus.COMMITTED,
            TransactionStatus.ABORTED,
        }:
            handler.finalize_recovery_record(fence, record)
            for project in fence.projects:
                self._fences.clear(project, fence.transaction_id)

    def _conflicts(
        self,
        project: ProjectIdentity,
        resources: tuple[str, ...],
    ) -> tuple[str, ...]:
        requested = frozenset(resources)
        return tuple(
            fence.transaction_id
            for fence in self._fences.list(project)
            if fence.project_mode == LockMode.EXCLUSIVE or not requested.isdisjoint(fence.resource_keys)
        )

    def _handler(self, fence: RecoveryFence) -> OperationRecoveryHandler:
        handler = self._handlers.get(fence.operation)
        if handler is None:
            raise RecoveryInDoubtError(
                "project recovery fence has no registered operation handler",
                operation=fence.operation,
                transaction_id=fence.transaction_id,
            )
        return handler

    @staticmethod
    def _resource_keys(specs: tuple[LockSpec, ...]) -> tuple[str, ...]:
        return tuple(sorted({f"{spec.level}:{spec.key}" for spec in specs if spec.level != PROJECT_LOCK_LEVEL}))

    @staticmethod
    def _validate_mutation_scope(
        projects: tuple[ProjectIdentity, ...],
        specs: tuple[LockSpec, ...],
    ) -> None:
        if not projects or tuple(sorted(set(projects))) != projects:
            raise ValueError("mutation projects must be non-empty and canonical")
        project_specs = tuple(spec for spec in specs if spec.level == PROJECT_LOCK_LEVEL)
        expected_keys = tuple(project.key for project in projects)
        if tuple(spec.key for spec in project_specs) != expected_keys or any(
            spec.mode != LockMode.SHARED for spec in project_specs
        ):
            raise ValueError("mutation scope must contain one matching shared lock per project")
        if not any(spec.level != PROJECT_LOCK_LEVEL for spec in specs):
            raise ValueError("mutation scope must contain at least one resource lock")

    @staticmethod
    def _project_lock(
        project: ProjectIdentity,
        mode: LockMode,
        label: str,
    ) -> LockSpec:
        return LockSpec(PROJECT_LOCK_LEVEL, project.key, mode, label)

    @staticmethod
    def _timeline_lock(session_id: str, mode: LockMode) -> LockSpec:
        return LockSpec(
            TIMELINE_LOCK_LEVEL,
            session_id,
            mode,
            f"session timeline {session_id}",
        )


__all__ = [
    "OperationRecoveryHandler",
    "ProjectOperationControl",
    "RecoveryProjection",
]
