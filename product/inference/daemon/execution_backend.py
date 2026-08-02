"""Shared RPC adapter over the four Embedded inference runtime facades."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Callable, TypeAlias

from mote.contracts.inference.attempt import InferenceAttemptRequest
from mote.contracts.inference.events import AttemptLifecycleEvent, SessionLifecycleEvent
from mote.contracts.inference.execution_owner import (
    ExecutionEpochBinding,
    ExecutionId,
    ExecutionObjectCommand,
    ExecutionOwnerRecord,
    ExecutionOwnerVerification,
    SharedExecutionVariant,
    epoch_binding_from_permit,
    verify_execution_owner,
    verify_execution_permit_binding,
)
from mote.contracts.inference.executions import BoundExecutionRequest, SessionApplicationMessage, TransferPartRequest
from mote.contracts.inference.persisted_event import PersistedLifecycleEvent
from mote.contracts.inference.shared import SharedSessionCredential
from mote.contracts.inference.wire_permit import WirePermit
from mote.contracts.ports.inference.attempt_receipt import AttemptReceiptStore
from mote.contracts.ports.inference.execution_owner import ExecutionOwnerRecordStore
from mote.contracts.ports.inference.lifecycle_event import LifecycleEventStore
from mote.contracts.ports.inference.session_receipt import SessionReceiptStore
from mote.product.inference.daemon.messages import (
    AuthorizeExecutionCommand,
    CancelExecutionCommand,
    EventCursor,
    ExecutionQuery,
    ExecutionReceiptView,
    FiniteExecution,
    LifecycleEvent,
    LifecycleEventView,
    SessionExecution,
    SessionMessageCommand,
    StartExecutionCommand,
    TransferExecutionCommand,
)
from mote.runtime.inference.command_runtime import EmbeddedServiceCommandRuntime
from mote.runtime.inference.runtime import EmbeddedInferenceRuntime
from mote.runtime.inference.session_runtime import EmbeddedSessionRuntime
from mote.runtime.inference.transfer_runtime import EmbeddedArtifactTransferRuntime


@dataclass(slots=True)
class _EventJournal:
    events: list[LifecycleEvent] = field(default_factory=list)
    terminal: bool = False
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)

    async def append(self, event: LifecycleEvent) -> None:
        async with self.condition:
            self.events.append(event)
            self.terminal = event.terminal
            self.condition.notify_all()


@dataclass(frozen=True, slots=True)
class _FiniteExecutionHandle:
    execution: FiniteExecution


@dataclass(frozen=True, slots=True)
class _SessionExecutionHandle:
    execution: SessionExecution


_ExecutionHandle: TypeAlias = _FiniteExecutionHandle | _SessionExecutionHandle


class SharedEmbeddedExecutionBackend:
    """Thin deployment adapter; it owns no routing, retry, budget, or wire policy."""

    def __init__(
        self,
        *,
        unary: EmbeddedInferenceRuntime,
        commands: EmbeddedServiceCommandRuntime,
        sessions: EmbeddedSessionRuntime,
        transfers: EmbeddedArtifactTransferRuntime,
        receipts: AttemptReceiptStore,
        session_receipts: SessionReceiptStore,
        events: LifecycleEventStore,
        owners: ExecutionOwnerRecordStore,
        epoch_provider: Callable[[], tuple[int, int]],
    ) -> None:
        self._unary = unary
        self._commands = commands
        self._sessions = sessions
        self._transfers = transfers
        self._receipts = receipts
        self._session_receipts = session_receipts
        self._event_store = events
        self._owners = owners
        self._epoch_provider = epoch_provider
        self._executions: dict[str, _ExecutionHandle] = {}
        self._journals: dict[str, _EventJournal] = {}
        self._pumps: set[asyncio.Task[None]] = set()

    async def start_unary(self, request: StartExecutionCommand, credential: SharedSessionCredential) -> int:
        canonical = InferenceAttemptRequest.model_validate_json(request.canonical_request)
        self._validate_start(request, canonical, credential, canonical.attempt_id)
        execution = await self._unary.start_attempt(canonical)
        owner = self._owner_record(canonical.attempt_id, canonical, credential, SharedExecutionVariant.FINITE)
        return await self._register(
            canonical.attempt_id,
            canonical.generation_id,
            _FiniteExecutionHandle(execution),
            owner,
            session=False,
        )

    async def start_durable_command(self, request: StartExecutionCommand, credential: SharedSessionCredential) -> int:
        canonical = BoundExecutionRequest.model_validate_json(request.canonical_request)
        self._validate_start(request, canonical, credential, canonical.execution_id)
        execution = await self._commands.start_command(canonical)
        owner = self._owner_record(canonical.execution_id, canonical, credential, SharedExecutionVariant.FINITE)
        return await self._register(
            canonical.execution_id,
            canonical.generation_id,
            _FiniteExecutionHandle(execution),
            owner,
            session=False,
        )

    async def open_session(self, request: StartExecutionCommand, credential: SharedSessionCredential) -> int:
        canonical = BoundExecutionRequest.model_validate_json(request.canonical_request)
        self._validate_start(request, canonical, credential, canonical.execution_id)
        execution = await self._sessions.open(canonical)
        owner = self._owner_record(canonical.execution_id, canonical, credential, SharedExecutionVariant.SESSION)
        return await self._register(
            canonical.execution_id,
            canonical.generation_id,
            _SessionExecutionHandle(execution),
            owner,
            session=True,
        )

    async def execute_transfer_part(
        self, request: TransferExecutionCommand, credential: SharedSessionCredential
    ) -> int:
        canonical = TransferPartRequest.model_validate_json(request.start.canonical_request)
        self._validate_start(request.start, canonical, credential, canonical.execution_id)
        if (
            request.part_number != canonical.part_number
            or request.offset != canonical.offset
            or request.length != canonical.length
            or request.content_digest != canonical.content_digest
        ):
            raise ValueError("transfer RPC fields disagree with canonical request")
        execution = await self._transfers.execute_part(canonical)
        owner = self._owner_record(canonical.execution_id, canonical, credential, SharedExecutionVariant.FINITE)
        return await self._register(
            canonical.execution_id,
            canonical.generation_id,
            _FiniteExecutionHandle(execution),
            owner,
            session=False,
        )

    async def authorize(self, request: AuthorizeExecutionCommand, credential: SharedSessionCredential) -> None:
        permit = request.permit
        owner = await self._verify_owner(
            request,
            credential,
            ExecutionObjectCommand.AUTHORIZE,
            epoch=epoch_binding_from_permit(permit),
        )
        permit_decision = verify_execution_permit_binding(owner, permit)
        if not permit_decision.allowed:
            raise PermissionError(f"execution permit binding denied: {permit_decision.disposition.value}")
        handle = self._require_execution(request.execution_id)
        if isinstance(handle, _SessionExecutionHandle):
            await handle.execution.authorize_open(permit)
        else:
            await handle.execution.authorize_wire(permit)

    async def cancel(self, request: CancelExecutionCommand, credential: SharedSessionCredential) -> None:
        await self._verify_owner(request, credential, ExecutionObjectCommand.CANCEL)
        handle = self._require_execution(request.execution_id)
        if isinstance(handle, _SessionExecutionHandle):
            await handle.execution.close(request.reason)
        else:
            await handle.execution.cancel(request.reason)

    async def stream_events(
        self, request: EventCursor, credential: SharedSessionCredential
    ) -> AsyncIterator[LifecycleEventView]:
        await self._verify_owner(request, credential, ExecutionObjectCommand.STREAM_EVENTS)
        async for event in self._stream_events_authorized(request.execution_id, request.after_sequence):
            yield event

    async def _stream_events_authorized(self, execution_id: str, cursor: int) -> AsyncIterator[LifecycleEventView]:
        journal = self._journals.get(execution_id)
        if journal is None:
            found = False
            while True:
                persisted = await self._event_store.read_events(execution_id, after_sequence=cursor)
                if not persisted:
                    break
                found = True
                for event in persisted:
                    cursor = event.sequence
                    yield self._stored_rpc_event(event)
                if len(persisted) < 256 or persisted[-1].terminal:
                    break
            if not found:
                raise KeyError(f"unknown execution {execution_id}")
            return
        while True:
            async with journal.condition:
                available = [event for event in journal.events if event.sequence > cursor]
                if not available and not journal.terminal:
                    await journal.condition.wait()
                    continue
            for event in available:
                cursor = max(cursor, event.sequence)
                yield self._rpc_event(event)
            if journal.terminal:
                return

    async def query_receipt(self, request: ExecutionQuery, credential: SharedSessionCredential) -> ExecutionReceiptView:
        await self._verify_owner(request, credential, ExecutionObjectCommand.QUERY_RECEIPT)
        generation_id = request.envelope.generation_id
        if not generation_id:
            raise ValueError("receipt query requires generation id")
        session = await self._session_receipts.get(request.execution_id, generation_id)
        if session is not None:
            return ExecutionReceiptView(
                execution_id=request.execution_id,
                revision=session.revision,
                state=session.state.value,
            )
        receipt = await self._receipts.get(request.execution_id, generation_id)
        if receipt is None:
            raise KeyError(f"unknown execution {request.execution_id}")
        return ExecutionReceiptView(
            execution_id=request.execution_id,
            revision=receipt.revision,
            state=receipt.state.value,
            terminal_artifact_reference=receipt.terminal_artifact_reference or "",
        )

    async def reconcile(
        self, request: ExecutionQuery, credential: SharedSessionCredential
    ) -> AsyncIterator[LifecycleEventView]:
        await self._verify_owner(request, credential, ExecutionObjectCommand.RECONCILE)
        async for event in self._stream_events_authorized(request.execution_id, 0):
            yield event

    async def session(
        self,
        requests: AsyncIterator[SessionMessageCommand],
        credential: SharedSessionCredential,
    ) -> AsyncIterator[LifecycleEventView]:
        try:
            first = await anext(requests)
        except StopAsyncIteration:
            return
        execution_id = first.execution_id
        await self._send_session_message(first, credential)

        async def consume() -> None:
            async for request in requests:
                if request.execution_id != execution_id:
                    raise ValueError("session stream changed execution id")
                await self._send_session_message(request, credential)

        consumer = asyncio.create_task(consume())
        try:
            async for event in self._stream_events_authorized(execution_id, 0):
                yield event
            await consumer
        finally:
            if not consumer.done():
                consumer.cancel()
                await asyncio.gather(consumer, return_exceptions=True)

    async def _send_session_message(
        self,
        request: SessionMessageCommand,
        credential: SharedSessionCredential,
    ) -> None:
        permit = request.permit
        owner = await self._verify_owner(
            request,
            credential,
            ExecutionObjectCommand.SEND_SESSION_MESSAGE,
            epoch=epoch_binding_from_permit(permit),
        )
        permit_decision = verify_execution_permit_binding(owner, permit)
        if not permit_decision.allowed:
            raise PermissionError(f"execution permit binding denied: {permit_decision.disposition.value}")
        handle = self._require_execution(request.execution_id)
        if not isinstance(handle, _SessionExecutionHandle):
            raise PermissionError("session message targets a finite execution")
        await handle.execution.send(request.message, permit)

    async def aclose(self) -> None:
        await self._unary.aclose()
        await self._commands.aclose()
        await self._sessions.aclose()
        await self._transfers.aclose()
        for task in tuple(self._pumps):
            task.cancel()
        await asyncio.gather(*self._pumps, return_exceptions=True)

    async def drain(self, *, timeout_seconds: float) -> None:
        if timeout_seconds <= 0:
            raise ValueError("Shared backend drain timeout must be positive")
        async with asyncio.timeout(timeout_seconds):
            await asyncio.gather(
                self._unary.drain(timeout_seconds=timeout_seconds),
                self._commands.drain(timeout_seconds=timeout_seconds),
                self._sessions.drain(timeout_seconds=timeout_seconds),
                self._transfers.drain(timeout_seconds=timeout_seconds),
            )

    async def _register(
        self,
        execution_id: str,
        generation_id: str,
        handle: _ExecutionHandle,
        owner: ExecutionOwnerRecord,
        *,
        session: bool,
    ) -> int:
        committed_owner = await self._owners.put_owner_record(owner)
        if committed_owner != owner:
            raise PermissionError("execution owner binding changed during registration")
        existing = self._executions.get(execution_id)
        if existing is not None and existing != handle:
            raise ValueError("execution id is already registered")
        self._executions[execution_id] = handle
        if execution_id not in self._journals:
            journal = _EventJournal()
            self._journals[execution_id] = journal
            task = asyncio.create_task(self._pump(handle, journal))
            self._pumps.add(task)
            task.add_done_callback(self._pumps.discard)
        if session:
            receipt = await self._session_receipts.get(execution_id, generation_id)
        else:
            receipt = await self._receipts.get(execution_id, generation_id)
        if receipt is None:
            raise RuntimeError("runtime accepted execution without durable receipt")
        return receipt.revision

    def _owner_record(
        self,
        execution_id: str,
        canonical: InferenceAttemptRequest | BoundExecutionRequest,
        credential: SharedSessionCredential,
        variant: SharedExecutionVariant,
    ) -> ExecutionOwnerRecord:
        backup_epoch, admission_epoch = self._epoch_provider()
        return ExecutionOwnerRecord(
            record_revision=1,
            execution_id=ExecutionId(execution_id),
            variant=variant,
            principal=canonical.principal,
            application_scope=credential.application_id,
            credential_scope=credential.session_id,
            generation_id=canonical.generation_id,
            generation_artifact_digest=canonical.generation_artifact_digest,
            epoch=ExecutionEpochBinding(
                backup_epoch=backup_epoch,
                admission_epoch=admission_epoch,
                permit_trust_revision=credential.permit_trust_revision,
            ),
        )

    async def _verify_owner(
        self,
        request: ExecutionQuery,
        credential: SharedSessionCredential,
        command: ExecutionObjectCommand,
        *,
        epoch: ExecutionEpochBinding | None = None,
    ) -> ExecutionOwnerRecord:
        execution_id = ExecutionId(request.execution_id)
        record = await self._owners.get_owner_record(execution_id)
        if record is None:
            raise KeyError(f"unknown execution {execution_id}")
        if epoch is None:
            backup_epoch, admission_epoch = self._epoch_provider()
            epoch = ExecutionEpochBinding(
                backup_epoch=backup_epoch,
                admission_epoch=admission_epoch,
                permit_trust_revision=credential.permit_trust_revision,
            )
        envelope = request.envelope
        decision = verify_execution_owner(
            record,
            ExecutionOwnerVerification(
                execution_id=execution_id,
                command=command,
                principal=credential.principal,
                application_scope=credential.application_id,
                credential_scope=credential.session_id,
                generation_id=envelope.generation_id,
                generation_artifact_digest=envelope.generation_artifact_digest,
                epoch=epoch,
            ),
        )
        if not decision.allowed:
            raise PermissionError(f"execution owner verification denied: {decision.disposition.value}")
        return record

    async def _pump(self, handle: _ExecutionHandle, journal: _EventJournal) -> None:
        async for event in handle.execution:
            await self._event_store.append_event(self._persisted_event(event))
            await journal.append(event)

    def _validate_start(
        self,
        request: StartExecutionCommand,
        canonical: InferenceAttemptRequest | BoundExecutionRequest | TransferPartRequest,
        credential: SharedSessionCredential,
        execution_id: str,
    ) -> None:
        envelope = request.envelope
        if request.execution_id != execution_id:
            raise ValueError("RPC execution id disagrees with canonical request")
        if canonical.principal != credential.principal:
            raise PermissionError("canonical principal is not authenticated principal")
        if (
            envelope.generation_id != canonical.generation_id
            or envelope.generation_artifact_digest != canonical.generation_artifact_digest
        ):
            raise ValueError("RPC generation binding disagrees with canonical request")

    def _require_execution(self, execution_id: str) -> _ExecutionHandle:
        try:
            return self._executions[execution_id]
        except KeyError as exc:
            raise KeyError(f"unknown execution {execution_id}") from exc

    def _require_journal(self, execution_id: str) -> _EventJournal:
        try:
            return self._journals[execution_id]
        except KeyError as exc:
            raise KeyError(f"unknown execution {execution_id}") from exc

    @staticmethod
    def _persisted_event(
        event: AttemptLifecycleEvent | SessionLifecycleEvent,
    ) -> PersistedLifecycleEvent:
        execution_id = event.attempt_id if isinstance(event, AttemptLifecycleEvent) else event.session_id
        return PersistedLifecycleEvent(
            execution_id=execution_id,
            sequence=event.sequence,
            receipt_revision=event.receipt_revision,
            event_type=event.event_type.value,
            payload=event.model_dump_json().encode(),
            terminal=event.terminal,
        )

    @staticmethod
    def _stored_rpc_event(event: PersistedLifecycleEvent) -> LifecycleEventView:
        return LifecycleEventView(
            execution_id=event.execution_id,
            sequence=event.sequence,
            receipt_revision=event.receipt_revision,
            event_type=event.event_type,
            payload=event.payload,
        )

    @staticmethod
    def _rpc_event(event: AttemptLifecycleEvent | SessionLifecycleEvent) -> LifecycleEventView:
        execution_id = event.attempt_id if isinstance(event, AttemptLifecycleEvent) else event.session_id
        return LifecycleEventView(
            execution_id=execution_id,
            sequence=event.sequence,
            receipt_revision=event.receipt_revision,
            event_type=event.event_type.value,
            payload=event.model_dump_json().encode(),
        )
