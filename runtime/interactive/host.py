"""ManagedRuntimeHost — identity, lifecycle, access, revision and fencing."""
from __future__ import annotations

import asyncio
import hashlib
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from typing import AsyncIterator, Awaitable, Callable
from uuid import uuid4

from mote.contracts.events.session import RuntimeDurabilityChangedEvent
from mote.contracts.interaction.handoff import HandoffRequest, HumanHandoffOutcome
from mote.contracts.ports.runtime.checkpoint import RuntimeCheckpointPayloadStore, RuntimeCheckpointSink
from mote.contracts.ports.runtime.driver import JournaledRuntimeDriver, ManagedRuntimeDriver
from mote.contracts.ports.runtime.handoff import RuntimeHandoffJournal
from mote.contracts.ports.runtime.lease import LeaseCoordinator
from mote.contracts.ports.runtime.operation import RuntimeOperationJournal
from mote.contracts.ports.runtime.projection import RuntimeProjectionJournal
from mote.contracts.runtime import (
    DriverCheckpoint,
    RuntimeAccessMode,
    RuntimeCheckpoint,
    RuntimeCommitFact,
    RuntimeDescriptor,
    RuntimeDurabilityState,
    RuntimeHealth,
    RuntimeOperationIntent,
    RuntimeOperationReceipt,
    RuntimeProjectionAck,
    RuntimeProjectionIntent,
    RuntimeRef,
    RuntimeState,
)
from mote.contracts.runtime.errors import (
    ManagedRuntimeAliasConflictError,
    ManagedRuntimeDurabilityError,
    ManagedRuntimeNotFoundError,
    ManagedRuntimeRevisionConflictError,
    ManagedRuntimeStateError,
)
from mote.contracts.runtime.handoff import RuntimeHandoffIntent, RuntimeHandoffRecovery, RuntimeHandoffResolution
from mote.contracts.runtime.lease import RuntimeLeasePolicy
from mote.runtime.control.leases import InMemoryLeaseCoordinator, LeaseHandle
from mote.runtime.telemetry.logging import log_call, logger


@log_call(
    level="DEBUG",
    log_args=False,
    log_result=False,
    name="RuntimeCheckpointSink.persist",
)
async def _persist_through_sink(
    sink: RuntimeCheckpointSink,
    checkpoint: RuntimeCheckpoint,
    reason: str,
) -> None:
    await sink.persist(checkpoint, reason=reason)


@log_call(
    level="DEBUG",
    log_args=False,
    log_result=False,
    name="ManagedRuntimeDriver.checkpoint",
)
async def _capture_driver_checkpoint(
    driver: ManagedRuntimeDriver,
    reason: str,
) -> DriverCheckpoint:
    return await driver.checkpoint(reason)


@dataclass
class _RuntimeRecord:
    ref: RuntimeRef
    driver: ManagedRuntimeDriver
    state: RuntimeState = RuntimeState.DECLARED
    epoch: int = 0
    revision: int = 0
    recoverable_revision: int = 0
    durability_error: str = ""
    durability_retry_task: asyncio.Task[None] | None = None
    pending_projection_intents: tuple[RuntimeProjectionIntent, ...] = ()
    pending_projection_operation: RuntimeOperationIntent | None = None
    durability_configured: bool = False
    last_durability_report: tuple[str, int, int, str] | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def descriptor(self) -> RuntimeDescriptor:
        durability = (
            RuntimeDurabilityState.LAGGING
            if self.durability_configured and (self.durability_error or self.recoverable_revision < self.revision)
            else (
                RuntimeDurabilityState.CURRENT if self.durability_configured else RuntimeDurabilityState.NOT_CONFIGURED
            )
        )
        return RuntimeDescriptor(
            ref=self.ref,
            state=self.state,
            epoch=self.epoch,
            revision=self.revision,
            capabilities=self.driver.capabilities,
            durability=durability,
            recoverable_revision=self.recoverable_revision,
            durability_detail=self.durability_error,
        )


class RuntimeAccess:
    """One serialized access window; writes finalize only on clean context exit."""

    def __init__(
        self,
        record: _RuntimeRecord,
        mode: RuntimeAccessMode,
        lease: LeaseHandle | None,
        host: "RuntimeHost",
    ) -> None:
        self._record = record
        self.mode = mode
        self._lease = lease
        self._host = host
        self._commit_requested = False
        self._changed = False
        self._closed = False
        self._projections: tuple[RuntimeProjectionIntent, ...] = ()
        self._operation: RuntimeOperationIntent | None = None
        self._operation_receipt: RuntimeOperationReceipt | None = None
        self.result_revision = record.revision
        self.result_commit_id: str | None = None

    @property
    def driver(self) -> ManagedRuntimeDriver:
        return self._record.driver

    @property
    def descriptor(self) -> RuntimeDescriptor:
        return self._record.descriptor()

    @property
    def fencing_token(self) -> int | None:
        return self._lease.fencing_token if self._lease is not None else None

    def commit(
        self,
        *,
        changed: bool = True,
        projections: tuple[RuntimeProjectionIntent, ...] = (),
    ) -> int:
        """Request commit after the context exits without an exception.

        The returned revision is projected; it becomes authoritative only after
        the surrounding ``async with`` exits successfully.
        """
        if self._closed or self._commit_requested:
            raise ManagedRuntimeStateError("runtime access is closed or already committed")
        if self.mode is RuntimeAccessMode.READ and changed:
            raise ManagedRuntimeStateError("read access cannot commit a mutation")
        projections = tuple(projections)
        if any(not isinstance(item, RuntimeProjectionIntent) for item in projections):
            raise TypeError("runtime commit projections must be RuntimeProjectionIntent values")
        if projections and not changed:
            raise ManagedRuntimeStateError("runtime projections require a changed write commit")
        if self._operation is not None:
            if projections and projections != self._operation.projections:
                raise ManagedRuntimeStateError("runtime commit projections differ from the prepared operation")
            projections = self._operation.projections
        self._commit_requested = True
        self._changed = changed
        self._projections = projections
        return self._record.revision + (1 if changed else 0)

    async def prepare_operation(
        self,
        *,
        operation_id: str,
        codec: str,
        schema_version: int,
        payload: str,
        projections: tuple[RuntimeProjectionIntent, ...] = (),
    ) -> bool:
        """Durably record one deterministic mutation before driver application."""
        if self.mode is not RuntimeAccessMode.WRITE or self._closed:
            raise ManagedRuntimeStateError("runtime operation preparation requires open write access")
        if self._commit_requested or self._operation is not None:
            raise ManagedRuntimeStateError("runtime access already prepared an operation")
        self._projections = tuple(projections)
        prepared = await self._host._prepare_operation(
            self._record,
            operation_id=operation_id,
            codec=codec,
            schema_version=schema_version,
            payload=payload,
            projections=self._projections,
        )
        if isinstance(prepared, RuntimeOperationReceipt):
            self._operation_receipt = prepared
            self.result_revision = prepared.revision
            self.result_commit_id = prepared.commit_id or None
            return False
        self._operation = prepared
        return True

    def _finalize(self) -> None:
        if self._commit_requested and self._changed:
            assert self._lease is not None
            self._lease.assert_current()
            with self._lease.guard():
                self._record.revision += 1
        self.result_revision = self._record.revision
        self._closed = True

    def _abort(self) -> None:
        self.result_revision = self._record.revision
        self._closed = True


class RuntimeHandoffAccess:
    """One exclusive Agent-to-human ownership window for a Runtime."""

    def __init__(
        self,
        record: _RuntimeRecord,
        request: HandoffRequest,
        lease: LeaseHandle,
        persist_checkpoint: Callable[[RuntimeCheckpoint, str], Awaitable[RuntimeCheckpoint]],
        journal: RuntimeHandoffJournal | None,
    ) -> None:
        self._record = record
        self.request = request
        self._lease = lease
        self._persist_checkpoint = persist_checkpoint
        self._journal = journal
        self._intent: RuntimeHandoffIntent | None = None
        self._outcome: HumanHandoffOutcome | None = None
        self._after_checkpoint: RuntimeCheckpoint | None = None
        self._commit_requested = False
        self._changed = False
        self._closed = False
        self.result_revision = record.revision

    @property
    def driver(self) -> ManagedRuntimeDriver:
        return self._record.driver

    @property
    def descriptor(self) -> RuntimeDescriptor:
        return self._record.descriptor()

    @property
    def fencing_token(self) -> int:
        return self._lease.fencing_token

    async def checkpoint(self, reason: str) -> RuntimeCheckpoint:
        payload = await self._record.driver.checkpoint(reason)
        checkpoint = RuntimeHost._build_checkpoint(self._record, payload)
        return await self._persist_checkpoint(checkpoint, reason)

    async def prepare(self, base_checkpoint: RuntimeCheckpoint | None) -> None:
        """Durably prepare before a driver creates human write authority."""
        if self._closed or self._intent is not None:
            raise ManagedRuntimeStateError("runtime handoff access is closed or already prepared")
        journal = self._journal
        if journal is None:
            return
        intent = RuntimeHandoffIntent(
            handoff_id=uuid4().hex,
            runtime_id=self._record.ref.runtime_id,
            kind=self._record.ref.kind,
            alias=self._record.ref.alias,
            epoch=self._record.epoch,
            base_revision=self._record.revision,
            target_revision=self._record.revision + 1,
            owner_id=self._lease.owner_id,
            fencing_token=self._lease.fencing_token,
            mode=self.request.mode,
            message=self.request.message,
            selection=self.request.selection,
            base_checkpoint=base_checkpoint,
        )
        try:
            await journal.prepare(intent)
        except BaseException as exc:
            raise ManagedRuntimeDurabilityError(
                "runtime handoff could not be durably prepared",
                cause=exc,
                runtime_id=self._record.ref.runtime_id,
                revision=self._record.revision,
            ) from exc
        self._intent = intent

    async def activate(self) -> None:
        """Record that the driver surface is ready to accept human input."""
        journal = self._journal
        if journal is None:
            return
        if self._closed or self._intent is None:
            raise ManagedRuntimeStateError("runtime handoff has not been prepared")
        try:
            await journal.activate(self._intent.handoff_id)
        except BaseException as exc:
            raise ManagedRuntimeDurabilityError(
                "runtime handoff activation did not persist",
                cause=exc,
                runtime_id=self._record.ref.runtime_id,
                revision=self._record.revision,
            ) from exc

    def commit(
        self,
        *,
        changed: bool = True,
        outcome: HumanHandoffOutcome | None = None,
        checkpoint: RuntimeCheckpoint | None = None,
    ) -> int:
        if self._closed or self._commit_requested:
            raise ManagedRuntimeStateError("runtime handoff access is closed or already committed")
        self._commit_requested = True
        self._changed = changed
        self._outcome = outcome
        self._after_checkpoint = checkpoint
        return self._record.revision + (1 if changed else 0)

    def _finalize(self) -> None:
        if self._commit_requested and self._changed:
            self._lease.assert_current()
            with self._lease.guard():
                self._record.revision += 1
        self.result_revision = self._record.revision
        self._closed = True

    def _abort(self) -> None:
        self.result_revision = self._record.revision
        self._closed = True


class RuntimeHost:
    """Own every live RuntimeDriver for one agent/session composition root."""

    def __init__(
        self,
        *,
        lease_coordinator: LeaseCoordinator | None = None,
        lease_policy: RuntimeLeasePolicy = RuntimeLeasePolicy(),
        checkpoint_sink: RuntimeCheckpointSink | None = None,
        projection_journal: RuntimeProjectionJournal | None = None,
        operation_journal: RuntimeOperationJournal | None = None,
        handoff_journal: RuntimeHandoffJournal | None = None,
        checkpoint_payload_store: RuntimeCheckpointPayloadStore | None = None,
        durability_observer: Callable[[RuntimeDurabilityChangedEvent], None] | None = None,
    ) -> None:
        self._lease_coordinator = lease_coordinator or InMemoryLeaseCoordinator()
        self._lease_policy = lease_policy
        self._checkpoint_sink = checkpoint_sink
        self._projection_journal = projection_journal
        self._operation_journal = operation_journal
        self._handoff_journal = handoff_journal
        self._checkpoint_payload_store = checkpoint_payload_store
        self._durability_observer = durability_observer
        self._by_id: dict[str, _RuntimeRecord] = {}
        self._by_alias: dict[str, str] = {}
        self._staged_checkpoints: dict[str, RuntimeCheckpoint] = {}
        self._registry_lock = asyncio.Lock()

    def stage_checkpoint(self, checkpoint: RuntimeCheckpoint, *, alias: str | None = None) -> None:
        """Stage one durable checkpoint for lazy restoration by ``ensure``."""
        readable = RuntimeRef(
            runtime_id=checkpoint.runtime_id,
            kind=checkpoint.kind,
            alias=alias or checkpoint.alias,
        ).readable
        if readable in self._by_alias:
            raise ManagedRuntimeStateError(
                "cannot stage a checkpoint for a running runtime",
                runtime=readable,
            )
        self._staged_checkpoints[readable] = checkpoint

    async def create(
        self,
        driver: ManagedRuntimeDriver,
        *,
        alias: str = "default",
        runtime_id: str | None = None,
        checkpoint: RuntimeCheckpoint | None = None,
    ) -> RuntimeDescriptor:
        async with self._registry_lock:
            return await self._create(driver, alias=alias, runtime_id=runtime_id, checkpoint=checkpoint)

    async def ensure(
        self,
        driver: ManagedRuntimeDriver,
        *,
        alias: str = "default",
        runtime_id: str | None = None,
        checkpoint: RuntimeCheckpoint | None = None,
    ) -> RuntimeDescriptor:
        """Return the named runtime, atomically creating it when absent."""
        readable = f"{driver.kind.strip()}:{alias}"
        async with self._registry_lock:
            existing_id = self._by_alias.get(readable)
            if existing_id is not None:
                return self._by_id[existing_id].descriptor()
            staged = self._staged_checkpoints.get(readable) if checkpoint is None else None
            effective_checkpoint = checkpoint or staged
            effective_runtime_id = runtime_id
            if effective_checkpoint is not None and effective_runtime_id is None:
                effective_runtime_id = effective_checkpoint.runtime_id
            descriptor = await self._create(
                driver,
                alias=alias,
                runtime_id=effective_runtime_id,
                checkpoint=effective_checkpoint,
            )
            if staged is not None and self._staged_checkpoints.get(readable) is staged:
                self._staged_checkpoints.pop(readable, None)
            return descriptor

    async def _create(
        self,
        driver: ManagedRuntimeDriver,
        *,
        alias: str,
        runtime_id: str | None,
        checkpoint: RuntimeCheckpoint | None,
    ) -> RuntimeDescriptor:
        kind = driver.kind.strip()
        if not driver.capabilities.multi_instance:
            existing = next(
                (item for item in self._by_id.values() if item.ref.kind == kind),
                None,
            )
            if existing is not None:
                raise ManagedRuntimeAliasConflictError(
                    "runtime kind does not support multiple instances",
                    kind=kind,
                    existing_runtime_id=existing.ref.runtime_id,
                    existing_alias=existing.ref.alias,
                )
        handoff_recovery = None
        if self._handoff_journal is not None:
            handoff_recovery = await self._handoff_journal.recovery(
                kind=kind,
                alias=alias,
                checkpoint=checkpoint,
            )
            if handoff_recovery.checkpoint is not None:
                checkpoint = handoff_recovery.checkpoint
        recovery = None
        if self._operation_journal is not None:
            recovery = await self._operation_journal.recovery(
                kind=kind,
                alias=alias,
                checkpoint=checkpoint,
            )
            if checkpoint is None and recovery.checkpoint is not None:
                checkpoint = recovery.checkpoint
        runtime_id = runtime_id or (
            checkpoint.runtime_id
            if checkpoint is not None
            else (
                handoff_recovery.runtime_id
                if handoff_recovery is not None and handoff_recovery.runtime_id
                else uuid4().hex
            )
        )
        ref = RuntimeRef(runtime_id=runtime_id, kind=kind, alias=alias)
        if runtime_id in self._by_id:
            raise ManagedRuntimeAliasConflictError("runtime_id is already registered", runtime_id=runtime_id)
        if ref.readable in self._by_alias:
            raise ManagedRuntimeAliasConflictError("runtime alias is already registered", runtime=ref.readable)
        if checkpoint is not None and (checkpoint.runtime_id != runtime_id or checkpoint.kind != kind):
            raise ManagedRuntimeStateError(
                "checkpoint identity does not match the runtime being created",
                runtime_id=runtime_id,
                checkpoint_runtime_id=checkpoint.runtime_id,
                kind=kind,
                checkpoint_kind=checkpoint.kind,
            )

        record = _RuntimeRecord(
            ref=ref,
            driver=driver,
            state=RuntimeState.STARTING,
            epoch=(
                checkpoint.epoch + 1
                if checkpoint is not None
                else (
                    handoff_recovery.epoch + 1
                    if handoff_recovery is not None and handoff_recovery.epoch is not None
                    else 1
                )
            ),
            revision=(
                checkpoint.revision
                if checkpoint is not None
                else (
                    handoff_recovery.revision
                    if handoff_recovery is not None and handoff_recovery.revision is not None
                    else 0
                )
            ),
            recoverable_revision=(checkpoint.revision if checkpoint is not None else 0),
            durability_configured=(self._checkpoint_sink is not None or self._projection_journal is not None),
        )
        self._by_id[runtime_id] = record
        self._by_alias[ref.readable] = runtime_id
        try:
            driver_checkpoint = await self._open_checkpoint(checkpoint)
            await driver.start(driver_checkpoint)
            if recovery is not None and recovery.operations:
                if not isinstance(driver, JournaledRuntimeDriver):
                    raise ManagedRuntimeStateError(
                        "runtime has pending operations but its driver is not replayable",
                        runtime=ref.readable,
                    )
                await self._recover_operations(record, recovery.operations)
            if handoff_recovery is not None:
                await self._complete_handoff_recovery(record, handoff_recovery)
        except BaseException:
            record.state = RuntimeState.FAILED
            try:
                await driver.aclose()
            finally:
                self._remove(record)
            raise
        record.state = RuntimeState.READY
        return record.descriptor()

    async def _complete_handoff_recovery(
        self,
        record: _RuntimeRecord,
        recovery: RuntimeHandoffRecovery,
    ) -> None:
        journal = self._handoff_journal
        if journal is None:
            return
        checkpoint = recovery.checkpoint
        if checkpoint is not None:
            checkpoint = replace(
                checkpoint,
                epoch=record.epoch,
                revision=record.revision,
            )
        try:
            for handoff_id in recovery.recovered_handoff_ids:
                await journal.resolve(
                    RuntimeHandoffResolution(
                        handoff_id=handoff_id,
                        status="recovered",
                        runtime_id=record.ref.runtime_id,
                        kind=record.ref.kind,
                        alias=record.ref.alias,
                        epoch=record.epoch,
                        revision=record.revision,
                        checkpoint=checkpoint,
                    )
                )
        except BaseException as exc:
            raise ManagedRuntimeDurabilityError(
                "runtime ownership was reclaimed but its resolution did not persist",
                cause=exc,
                runtime_id=recovery.runtime_id,
                revision=recovery.revision,
            ) from exc

    async def _recover_operations(
        self,
        record: _RuntimeRecord,
        operations: tuple[RuntimeOperationIntent, ...],
    ) -> None:
        driver = record.driver
        assert isinstance(driver, JournaledRuntimeDriver)
        projection_journal = self._projection_journal
        operation_journal = self._operation_journal
        if operation_journal is None:
            raise RuntimeError("runtime operation journal is unavailable")
        for operation in operations:
            if operation.base_revision != record.revision:
                raise ManagedRuntimeStateError(
                    "runtime operation recovery revisions are not contiguous",
                    runtime_id=record.ref.runtime_id,
                    current_revision=record.revision,
                    operation_revision=operation.target_revision,
                )
            await driver.replay_operation(operation)
            record.revision = operation.target_revision
            if operation.projections:
                if projection_journal is None:
                    raise ManagedRuntimeDurabilityError(
                        "runtime projection journal is unavailable during recovery",
                        runtime_id=record.ref.runtime_id,
                        revision=record.revision,
                    )
                payload = await _capture_driver_checkpoint(
                    driver,
                    "operation-recovery",
                )
                checkpoint = self._build_checkpoint(record, payload)
                checkpoint = await self._seal_checkpoint(checkpoint)
                await projection_journal.record_commit(
                    RuntimeCommitFact(
                        commit_id=self._commit_id(checkpoint),
                        checkpoint=checkpoint,
                        projections=operation.projections,
                        reason="operation-recovery",
                    )
                )
                self._record_durability_success(record, checkpoint.revision)
            await operation_journal.complete(
                RuntimeOperationReceipt.from_intent(
                    operation,
                    revision=record.revision,
                    commit_id=(self._commit_id(checkpoint) if operation.projections else ""),
                )
            )

    def descriptor(self, runtime: RuntimeRef | str) -> RuntimeDescriptor:
        return self._resolve(runtime).descriptor()

    def list(self) -> list[RuntimeDescriptor]:
        return [record.descriptor() for record in self._by_id.values()]

    async def health(self, runtime: RuntimeRef | str) -> RuntimeHealth:
        record = self._resolve(runtime)
        async with record.lock:
            driver_health = await record.driver.health()
            configured = self._checkpoint_sink is not None or self._projection_journal is not None
            durability = (
                RuntimeDurabilityState.LAGGING
                if record.durability_error or (configured and record.recoverable_revision < record.revision)
                else (
                    RuntimeDurabilityState.CURRENT
                    if configured and record.recoverable_revision >= record.revision
                    else RuntimeDurabilityState.NOT_CONFIGURED
                )
            )
            return replace(
                driver_health,
                durability=durability,
                current_revision=record.revision,
                recoverable_revision=record.recoverable_revision,
                durability_detail=record.durability_error,
            )

    async def checkpoint(self, runtime: RuntimeRef | str, *, reason: str) -> RuntimeCheckpoint:
        record = self._resolve(runtime)
        async with record.lock:
            if record.state not in (RuntimeState.READY, RuntimeState.DEGRADED):
                raise self._state_error(record, "checkpoint")
            previous = record.state
            record.state = RuntimeState.BUSY
            try:
                payload = await record.driver.checkpoint(reason)
            finally:
                record.state = previous
            checkpoint = self._build_checkpoint(record, payload)
            return await self._persist_checkpoint(
                checkpoint,
                reason,
                best_effort=True,
            )

    @asynccontextmanager
    async def handoff_access(
        self,
        request: HandoffRequest,
        *,
        owner_id: str,
        expected_revision: int | None = None,
    ) -> AsyncIterator[RuntimeHandoffAccess]:
        """Fence Agent writers and yield one exclusive human ownership window."""
        record = self._resolve(request.runtime_ref)
        if not owner_id:
            raise ValueError("runtime handoff owner_id must be non-empty")
        await record.lock.acquire()
        lease: LeaseHandle | None = None
        access: RuntimeHandoffAccess | None = None
        try:
            if record.state is not RuntimeState.READY:
                raise self._state_error(record, "handoff")
            if request.mode not in record.driver.capabilities.handoff_modes:
                raise ManagedRuntimeStateError(
                    "runtime does not support the requested handoff mode",
                    runtime_id=record.ref.runtime_id,
                    mode=request.mode,
                )
            if expected_revision is not None and expected_revision != record.revision:
                raise ManagedRuntimeRevisionConflictError(
                    "runtime revision changed",
                    runtime_id=record.ref.runtime_id,
                    expected_revision=expected_revision,
                    current_revision=record.revision,
                )
            lease = LeaseHandle(
                self._lease_coordinator,
                subject=f"runtime:{record.ref.runtime_id}",
                owner_id=owner_id,
                policy=self._lease_policy,
            )
            await lease.start()
            lease.assert_current()
            record.state = RuntimeState.HANDED_OFF
            access = RuntimeHandoffAccess(
                record,
                request,
                lease,
                self._persist_checkpoint,
                self._handoff_journal,
            )
            try:
                yield access
            except BaseException:
                record.state = RuntimeState.DEGRADED
                access._abort()
                await self._abort_handoff(access)
                raise
            try:
                access._finalize()
            except BaseException:
                record.state = RuntimeState.DEGRADED
                raise
            else:
                try:
                    await self._resolve_handoff(access)
                except BaseException:
                    record.state = RuntimeState.DEGRADED
                    raise
                else:
                    record.state = RuntimeState.READY
        finally:
            if lease is not None:
                await lease.close()
            record.lock.release()

    @asynccontextmanager
    async def access(
        self,
        runtime: RuntimeRef | str,
        *,
        mode: RuntimeAccessMode | str,
        owner_id: str,
        expected_revision: int | None = None,
    ) -> AsyncIterator[RuntimeAccess]:
        record = self._resolve(runtime)
        access_mode = RuntimeAccessMode(mode)
        if not owner_id:
            raise ValueError("runtime access owner_id must be non-empty")
        await record.lock.acquire()
        lease: LeaseHandle | None = None
        access: RuntimeAccess | None = None
        try:
            allowed = (
                (RuntimeState.READY, RuntimeState.DEGRADED)
                if access_mode is RuntimeAccessMode.READ
                else (RuntimeState.READY,)
            )
            if record.state not in allowed:
                raise self._state_error(record, f"{access_mode.value} access")
            if expected_revision is not None and expected_revision != record.revision:
                raise ManagedRuntimeRevisionConflictError(
                    "runtime revision changed",
                    runtime_id=record.ref.runtime_id,
                    expected_revision=expected_revision,
                    current_revision=record.revision,
                )
            previous = record.state
            record.state = RuntimeState.BUSY
            if access_mode is RuntimeAccessMode.WRITE:
                lease = LeaseHandle(
                    self._lease_coordinator,
                    subject=f"runtime:{record.ref.runtime_id}",
                    owner_id=owner_id,
                    policy=self._lease_policy,
                )
                await lease.start()
                lease.assert_current()
            access = RuntimeAccess(record, access_mode, lease, self)
            try:
                yield access
            except BaseException:
                if access._commit_requested and access._changed:
                    record.state = RuntimeState.DEGRADED
                else:
                    record.state = previous
                await self._abort_operation(access)
                access._abort()
                raise
            try:
                access._finalize()
            except BaseException:
                record.state = RuntimeState.DEGRADED
                raise
            else:
                if access_mode is RuntimeAccessMode.WRITE and access._commit_requested and access._changed:
                    try:
                        await self._checkpoint_after_write(record, access)
                    except BaseException:
                        record.state = RuntimeState.DEGRADED
                        raise
                    else:
                        record.state = previous
                else:
                    await self._abort_operation(access)
                    record.state = previous
        finally:
            if lease is not None:
                await lease.close()
            record.lock.release()

    @staticmethod
    def _build_checkpoint(
        record: _RuntimeRecord,
        payload: DriverCheckpoint,
    ) -> RuntimeCheckpoint:
        return RuntimeCheckpoint(
            runtime_id=record.ref.runtime_id,
            kind=record.ref.kind,
            epoch=record.epoch,
            revision=record.revision,
            codec=payload.codec,
            schema_version=payload.schema_version,
            payload_ref=payload.payload_ref,
            alias=record.ref.alias,
            digest=payload.digest,
            sensitivity=payload.sensitivity,
            fidelity=payload.fidelity or record.driver.capabilities.checkpoint_fidelity,
        )

    async def _persist_checkpoint(
        self,
        checkpoint: RuntimeCheckpoint,
        reason: str,
        *,
        best_effort: bool = False,
        schedule_retry: bool = True,
    ) -> RuntimeCheckpoint:
        try:
            checkpoint = await self._seal_checkpoint(checkpoint)
            sink = self._checkpoint_sink
            if sink is None:
                return checkpoint
            await _persist_through_sink(sink, checkpoint, reason)
        except Exception as exc:
            self._record_durability_failure(checkpoint, exc)
            record = self._by_id.get(checkpoint.runtime_id)
            if record is not None and schedule_retry:
                self._schedule_durability_retry(record)
            if best_effort:
                return checkpoint
            raise ManagedRuntimeDurabilityError(
                "runtime checkpoint did not persist",
                cause=exc,
                runtime_id=checkpoint.runtime_id,
                revision=checkpoint.revision,
                reason=reason,
            ) from exc
        self._record_durability_success_for_checkpoint(checkpoint)
        return checkpoint

    async def _seal_checkpoint(self, checkpoint: RuntimeCheckpoint) -> RuntimeCheckpoint:
        store = self._checkpoint_payload_store
        return checkpoint if store is None else await store.seal(checkpoint)

    async def _open_checkpoint(self, checkpoint: RuntimeCheckpoint | None) -> RuntimeCheckpoint | None:
        if checkpoint is None:
            return None
        store = self._checkpoint_payload_store
        return checkpoint if store is None else await store.open(checkpoint)

    async def _checkpoint_after_write(
        self,
        record: _RuntimeRecord,
        access: RuntimeAccess,
    ) -> None:
        journal = self._projection_journal
        if access._projections and journal is not None:
            record.pending_projection_intents = access._projections
            record.pending_projection_operation = access._operation
            try:
                payload = await _capture_driver_checkpoint(
                    record.driver,
                    "write-commit",
                )
                checkpoint = self._build_checkpoint(record, payload)
                checkpoint = await self._seal_checkpoint(checkpoint)
                commit_id = self._commit_id(checkpoint)
                access.result_commit_id = commit_id
                await journal.record_commit(
                    RuntimeCommitFact(
                        commit_id=commit_id,
                        checkpoint=checkpoint,
                        projections=access._projections,
                        reason="write-commit",
                    )
                )
                self._record_durability_success(record, checkpoint.revision)
            except BaseException as exc:
                self._record_durability_failure_for_record(record, exc)
                self._schedule_durability_retry(record)
                raise ManagedRuntimeDurabilityError(
                    "runtime mutation committed but its projection fact did not persist",
                    cause=exc,
                    runtime_id=record.ref.runtime_id,
                    epoch=record.epoch,
                    revision=record.revision,
                ) from exc
            await self._complete_operation(access)
            record.pending_projection_intents = ()
            record.pending_projection_operation = None
            return
        if self._checkpoint_sink is None:
            return
        try:
            payload = await _capture_driver_checkpoint(
                record.driver,
                "write-commit",
            )
        except Exception as exc:
            self._record_durability_failure_for_record(record, exc)
            self._schedule_durability_retry(record)
            return
        await self._persist_checkpoint(
            self._build_checkpoint(record, payload),
            "write-commit",
            best_effort=True,
        )
        await self._complete_operation(access)

    def _record_durability_success_for_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None:
        record = self._by_id.get(checkpoint.runtime_id)
        if record is not None:
            self._record_durability_success(record, checkpoint.revision)

    def _record_durability_failure(self, checkpoint: RuntimeCheckpoint, exc: BaseException) -> None:
        record = self._by_id.get(checkpoint.runtime_id)
        if record is not None:
            self._record_durability_failure_for_record(record, exc)

    def _record_durability_success(self, record: _RuntimeRecord, revision: int) -> None:
        record.recoverable_revision = max(record.recoverable_revision, revision)
        record.durability_error = ""
        self._notify_durability(record)

    def _record_durability_failure_for_record(self, record: _RuntimeRecord, exc: BaseException) -> None:
        record.durability_error = f"{type(exc).__name__}: {exc}"[:4096]
        self._notify_durability(record)

    def _notify_durability(self, record: _RuntimeRecord) -> None:
        observer = self._durability_observer
        if observer is None:
            return
        descriptor = record.descriptor()
        signature = (
            descriptor.durability.value,
            descriptor.revision,
            descriptor.recoverable_revision or 0,
            descriptor.durability_detail,
        )
        prior = record.last_durability_report
        if descriptor.durability is RuntimeDurabilityState.LAGGING:
            if signature == prior:
                return
        elif not (
            descriptor.durability is RuntimeDurabilityState.CURRENT
            and prior is not None
            and prior[0] == RuntimeDurabilityState.LAGGING.value
        ):
            return
        record.last_durability_report = signature
        try:
            observer(
                RuntimeDurabilityChangedEvent(
                    runtime_id=record.ref.runtime_id,
                    runtime_kind=record.ref.kind,
                    alias=record.ref.alias,
                    state=descriptor.durability.value,
                    current_revision=descriptor.revision,
                    recoverable_revision=descriptor.recoverable_revision or 0,
                    detail=descriptor.durability_detail,
                )
            )
        except Exception as exc:
            logger.warning("RuntimeHost: durability observer failed for " f"{record.ref.readable}: {exc}")

    def _schedule_durability_retry(self, record: _RuntimeRecord) -> None:
        task = record.durability_retry_task
        if task is not None and not task.done():
            return
        record.durability_retry_task = asyncio.create_task(
            self._retry_durability(record),
            name=f"mote-runtime-durability-{record.ref.runtime_id[:12]}",
        )

    async def _retry_durability(self, record: _RuntimeRecord) -> None:
        delay = 0.05
        try:
            for _ in range(8):
                await asyncio.sleep(delay)
                async with record.lock:
                    if record.state in {
                        RuntimeState.CLOSING,
                        RuntimeState.CLOSED,
                        RuntimeState.FAILED,
                    }:
                        return
                    if record.pending_projection_intents:
                        try:
                            await self._retry_projection_fact(record)
                        except Exception as exc:
                            self._record_durability_failure_for_record(record, exc)
                        else:
                            if record.state is RuntimeState.DEGRADED:
                                record.state = RuntimeState.READY
                            return
                    try:
                        payload = await _capture_driver_checkpoint(
                            record.driver,
                            "durability-retry",
                        )
                    except Exception as exc:
                        self._record_durability_failure_for_record(record, exc)
                    else:
                        await self._persist_checkpoint(
                            self._build_checkpoint(record, payload),
                            "durability-retry",
                            best_effort=True,
                            schedule_retry=False,
                        )
                        if not record.durability_error and record.recoverable_revision >= record.revision:
                            return
                delay = min(delay * 2, 5.0)
        finally:
            if record.durability_retry_task is asyncio.current_task():
                record.durability_retry_task = None

    async def _retry_projection_fact(self, record: _RuntimeRecord) -> None:
        journal = self._projection_journal
        if journal is None:
            raise ManagedRuntimeDurabilityError(
                "runtime projection journal is unavailable during retry",
                runtime_id=record.ref.runtime_id,
                revision=record.revision,
            )
        payload = await _capture_driver_checkpoint(
            record.driver,
            "projection-durability-retry",
        )
        checkpoint = await self._seal_checkpoint(self._build_checkpoint(record, payload))
        commit_id = self._commit_id(checkpoint)
        await journal.record_commit(
            RuntimeCommitFact(
                commit_id=commit_id,
                checkpoint=checkpoint,
                projections=record.pending_projection_intents,
                reason="write-commit",
            )
        )
        operation = record.pending_projection_operation
        if operation is not None and self._operation_journal is not None:
            await self._operation_journal.complete(
                RuntimeOperationReceipt.from_intent(
                    operation,
                    revision=checkpoint.revision,
                    commit_id=commit_id,
                )
            )
        record.pending_projection_intents = ()
        record.pending_projection_operation = None
        self._record_durability_success(record, checkpoint.revision)

    async def _prepare_operation(
        self,
        record: _RuntimeRecord,
        *,
        operation_id: str,
        codec: str,
        schema_version: int,
        payload: str,
        projections: tuple[RuntimeProjectionIntent, ...],
    ) -> RuntimeOperationIntent | RuntimeOperationReceipt | None:
        journal = self._operation_journal
        if journal is None:
            return None
        try:
            driver_checkpoint = await _capture_driver_checkpoint(
                record.driver,
                "operation-prepare",
            )
            intent = RuntimeOperationIntent(
                operation_id=operation_id,
                runtime_id=record.ref.runtime_id,
                kind=record.ref.kind,
                alias=record.ref.alias,
                epoch=record.epoch,
                base_revision=record.revision,
                target_revision=record.revision + 1,
                codec=codec,
                schema_version=schema_version,
                payload=payload,
                base_checkpoint=await self._seal_checkpoint(self._build_checkpoint(record, driver_checkpoint)),
                projections=projections,
            )
            receipt = await journal.prepare(intent)
            if receipt is not None:
                if (
                    receipt.runtime_id != record.ref.runtime_id
                    or receipt.kind != record.ref.kind
                    or receipt.alias != record.ref.alias
                    or record.revision < receipt.revision
                ):
                    raise ManagedRuntimeStateError(
                        "completed runtime operation does not match live state",
                        operation_id=operation_id,
                        runtime_id=record.ref.runtime_id,
                        current_revision=record.revision,
                        completed_revision=receipt.revision,
                    )
                return receipt
            return intent
        except BaseException as exc:
            raise ManagedRuntimeDurabilityError(
                "runtime operation could not be durably prepared",
                cause=exc,
                runtime_id=record.ref.runtime_id,
                revision=record.revision,
            ) from exc

    async def _complete_operation(self, access: RuntimeAccess) -> None:
        operation = access._operation
        journal = self._operation_journal
        if operation is None or journal is None:
            return
        try:
            await journal.complete(
                RuntimeOperationReceipt.from_intent(
                    operation,
                    revision=access.result_revision,
                    changed=access._changed,
                    commit_id=access.result_commit_id or "",
                )
            )
        except Exception:
            return

    async def _abort_operation(self, access: RuntimeAccess) -> None:
        operation = access._operation
        journal = self._operation_journal
        if operation is None or journal is None:
            return
        try:
            await journal.abort(operation.operation_id)
        except Exception:
            return

    async def _resolve_handoff(self, access: RuntimeHandoffAccess) -> None:
        journal = self._handoff_journal
        intent = access._intent
        if journal is None or intent is None:
            return
        checkpoint = access._after_checkpoint
        if checkpoint is not None:
            checkpoint = replace(
                checkpoint,
                epoch=access._record.epoch,
                revision=access._record.revision,
            )
        outcome = access._outcome
        status = outcome.status.value if outcome is not None else "failed"
        try:
            await journal.resolve(
                RuntimeHandoffResolution(
                    handoff_id=intent.handoff_id,
                    status=status,
                    runtime_id=access._record.ref.runtime_id,
                    kind=access._record.ref.kind,
                    alias=access._record.ref.alias,
                    epoch=access._record.epoch,
                    revision=access._record.revision,
                    checkpoint=checkpoint,
                )
            )
        except BaseException as exc:
            raise ManagedRuntimeDurabilityError(
                "runtime handoff completed but its resolution did not persist",
                cause=exc,
                runtime_id=access._record.ref.runtime_id,
                revision=access._record.revision,
            ) from exc

    async def _abort_handoff(self, access: RuntimeHandoffAccess) -> None:
        journal = self._handoff_journal
        intent = access._intent
        if journal is None or intent is None:
            return
        try:
            await journal.resolve(
                RuntimeHandoffResolution(
                    handoff_id=intent.handoff_id,
                    status="failed",
                    runtime_id=access._record.ref.runtime_id,
                    kind=access._record.ref.kind,
                    alias=access._record.ref.alias,
                    epoch=access._record.epoch,
                    revision=access._record.revision,
                    checkpoint=intent.base_checkpoint,
                )
            )
        except Exception:
            return

    async def acknowledge_projection(self, commit_id: str, intent_id: str) -> bool:
        """Best-effort durable ack; a failed ack remains replayable from its fact."""
        journal = self._projection_journal
        if journal is None:
            return False
        try:
            await journal.acknowledge(RuntimeProjectionAck(commit_id=commit_id, intent_id=intent_id))
        except Exception:
            return False
        return True

    @staticmethod
    def _commit_id(checkpoint: RuntimeCheckpoint) -> str:
        identity = (f"{checkpoint.runtime_id}\0{checkpoint.epoch}\0{checkpoint.revision}").encode("utf-8")
        return f"runtime-{hashlib.sha256(identity).hexdigest()}"

    async def close(self, runtime: RuntimeRef | str) -> None:
        record = self._resolve(runtime)
        async with record.lock:
            if record.state is RuntimeState.CLOSED:
                self._remove(record)
                return
            if record.state is RuntimeState.CLOSING:
                raise self._state_error(record, "close")
            record.state = RuntimeState.CLOSING
            retry_task, record.durability_retry_task = (
                record.durability_retry_task,
                None,
            )
            if retry_task is not None and retry_task is not asyncio.current_task():
                retry_task.cancel()
                await asyncio.gather(retry_task, return_exceptions=True)
            try:
                await record.driver.aclose()
            except BaseException:
                record.state = RuntimeState.FAILED
                raise
            record.state = RuntimeState.CLOSED
            self._remove(record)

    async def close_all(self) -> dict[str, BaseException]:
        self._staged_checkpoints.clear()
        failures: dict[str, BaseException] = {}
        for record in reversed(list(self._by_id.values())):
            try:
                await self.close(record.ref)
            except BaseException as exc:
                failures[record.ref.readable] = exc
        return failures

    def _resolve(self, runtime: RuntimeRef | str) -> _RuntimeRecord:
        if isinstance(runtime, RuntimeRef):
            record = self._by_id.get(runtime.runtime_id)
            if record is not None and record.ref == runtime:
                return record
            label = runtime.readable
        else:
            label = runtime
            runtime_id = self._by_alias.get(runtime, runtime)
            record = self._by_id.get(runtime_id)
            if record is not None:
                return record
        raise ManagedRuntimeNotFoundError("runtime does not exist", runtime=str(label))

    def _remove(self, record: _RuntimeRecord) -> None:
        self._by_id.pop(record.ref.runtime_id, None)
        self._by_alias.pop(record.ref.readable, None)

    @staticmethod
    def _state_error(record: _RuntimeRecord, operation: str) -> ManagedRuntimeStateError:
        return ManagedRuntimeStateError(
            "operation is invalid for the runtime state",
            runtime_id=record.ref.runtime_id,
            state=record.state.value,
            operation=operation,
        )


__all__ = ["RuntimeAccess", "RuntimeHandoffAccess", "RuntimeHost"]
