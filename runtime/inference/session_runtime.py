"""Embedded long-lived session runtime with per-message wire authorization."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from mote.contracts.inference.events import SessionEventType, SessionLifecycleEvent
from mote.contracts.inference.executions import BoundExecutionRequest, SessionApplicationMessage
from mote.contracts.inference.governance import BudgetReservation
from mote.contracts.inference.receipt import AttemptReceipt, ReceiptState
from mote.contracts.inference.session import SessionReceipt, SessionReceiptState
from mote.contracts.inference.wire_permit import ExecutionTaxonomy, WirePermit
from mote.contracts.ports.inference.attempt_receipt import AttemptReceiptStore
from mote.contracts.ports.inference.credential_health import CredentialHealthAuthorityPort
from mote.contracts.ports.inference.provider_quota import ProviderQuotaAuthorityPort
from mote.contracts.ports.inference.session_receipt import SessionReceiptStore
from mote.contracts.ports.inference.session_transport import (
    ProviderSessionConnection,
    SessionTransport,
    SessionTransportResolver,
)
from mote.contracts.ports.inference.usage_ledger import UsageLedger
from mote.contracts.ports.inference.wire_permit import WirePermitVerifier
from mote.runtime.inference.bulkhead import BulkheadController, BulkheadIdentity, BulkheadPermit
from mote.runtime.inference.dispatcher import Dispatcher
from mote.runtime.inference.fair_queue import FairAdmissionQueue, QueueEntry
from mote.runtime.inference.generation import GatewayGenerationLease, GatewayGenerationOwner, GenerationDomain
from mote.runtime.inference.runtime import AttemptProtocolViolation


@dataclass(slots=True)
class _SessionWork:
    execution: "_SessionExecution"
    message: SessionApplicationMessage | None = None
    permit: WirePermit | None = None
    completion: asyncio.Future[None] | None = None


SubmitMessage = Callable[["_SessionExecution", SessionApplicationMessage, WirePermit], Awaitable[None]]


class EmbeddedSessionRuntime:
    def __init__(
        self,
        *,
        session_receipts: SessionReceiptStore,
        wire_receipts: AttemptReceiptStore,
        usage_ledger: UsageLedger,
        reserve_open_units: Callable[[BoundExecutionRequest], int],
        reserve_message_units: Callable[[SessionApplicationMessage], int],
        provider_quota: ProviderQuotaAuthorityPort,
        credential_health: CredentialHealthAuthorityPort,
        permit_verifier: WirePermitVerifier,
        transports: SessionTransportResolver,
        generations: GatewayGenerationOwner,
        permit_audience: str,
        epoch_provider: Callable[[], tuple[int, int]],
        message_timeout_seconds: float = 30.0,
        queue_capacity: int = 5000,
        event_capacity: int = 256,
        worker_count: int = 16,
        global_in_flight: int = 1000,
        provider_in_flight: int = 100,
        endpoint_in_flight: int = 100,
    ) -> None:
        if event_capacity <= 0 or message_timeout_seconds <= 0 or not permit_audience:
            raise ValueError("invalid session runtime limits or audience")
        self._session_receipts = session_receipts
        self._wire_receipts = wire_receipts
        self._usage_ledger = usage_ledger
        self._reserve_open_units = reserve_open_units
        self._reserve_message_units = reserve_message_units
        self._provider_quota = provider_quota
        self._credential_health = credential_health
        self._permit_verifier = permit_verifier
        self._transports = transports
        self._generations = generations
        self._permit_audience = permit_audience
        self._epoch_provider = epoch_provider
        self._message_timeout_seconds = message_timeout_seconds
        self._event_capacity = event_capacity
        self._queue = FairAdmissionQueue(capacity=queue_capacity)
        self._bulkheads = BulkheadController(
            global_limit=global_in_flight,
            provider_limit=provider_in_flight,
            endpoint_limit=endpoint_in_flight,
        )
        self._dispatcher = Dispatcher(
            queue=self._queue,
            bulkheads=self._bulkheads,
            identity_resolver=self._identity,
            handler=self._dispatch,
            timeout_handler=self._dispatch_timeout,
            worker_count=worker_count,
        )
        self._executions: dict[str, _SessionExecution] = {}
        self._started = False
        self._draining = False
        self._idle = asyncio.Event()
        self._idle.set()

    async def open(self, request: BoundExecutionRequest) -> "_SessionExecution":
        if self._draining:
            raise RuntimeError("session runtime is draining")
        existing = self._executions.get(request.execution_id)
        if existing is not None:
            if existing.request != request:
                raise AttemptProtocolViolation("session id reused with a different open request")
            return existing
        if not self._started:
            self._dispatcher.start()
            self._started = True
        lease = self._generations.acquire(GenerationDomain.SESSION)
        if lease.generation_id != request.generation_id or lease.artifact_digest != request.generation_artifact_digest:
            lease.release()
            raise AttemptProtocolViolation("session generation is not active")
        execution = _SessionExecution(
            request=request,
            transport=self._transports.resolve_session(request),
            session_receipts=self._session_receipts,
            wire_receipts=self._wire_receipts,
            usage_ledger=self._usage_ledger,
            reserve_open_units=self._reserve_open_units,
            reserve_message_units=self._reserve_message_units,
            provider_quota=self._provider_quota,
            credential_health=self._credential_health,
            permit_verifier=self._permit_verifier,
            permit_audience=self._permit_audience,
            epoch_provider=self._epoch_provider,
            generation_lease=lease,
            event_capacity=self._event_capacity,
            submit_message=self._submit_message,
            on_terminal=self._execution_terminal,
        )
        self._executions[request.execution_id] = execution
        self._idle.clear()
        try:
            await execution.accept()
            deadline = request.deadline.to_local_deadline()
            await self._queue.enqueue(
                _SessionWork(execution),
                tenant_id=request.principal.tenant_id,
                project_id=request.principal.project_id,
                scheduling=request.scheduling,
                deadline=deadline,
            )
        except BaseException:
            lease.release()
            del self._executions[request.execution_id]
            raise
        await execution.emit_event(SessionEventType.QUEUED)
        return execution

    async def drain(self, *, timeout_seconds: float) -> None:
        if timeout_seconds <= 0:
            raise ValueError("drain timeout must be positive")
        self._draining = True
        try:
            async with asyncio.timeout(timeout_seconds):
                await self._idle.wait()
        except TimeoutError as exc:
            raise TimeoutError("session runtime drain timed out") from exc
        if self._started:
            await self._dispatcher.drain(timeout_seconds=timeout_seconds)

    async def aclose(self) -> None:
        self._draining = True
        for execution in tuple(self._executions.values()):
            if not execution.terminal:
                await execution.close("runtime closing")
        if self._started:
            await self._dispatcher.aclose()

    def _execution_terminal(self, session_id: str) -> None:
        self._executions.pop(session_id, None)
        if not self._executions:
            self._idle.set()

    async def _submit_message(
        self,
        execution: "_SessionExecution",
        message: SessionApplicationMessage,
        permit: WirePermit,
    ) -> None:
        loop = asyncio.get_running_loop()
        completion: asyncio.Future[None] = loop.create_future()
        await self._queue.enqueue(
            _SessionWork(execution, message, permit, completion),
            tenant_id=execution.request.principal.tenant_id,
            project_id=execution.request.principal.project_id,
            scheduling=execution.request.scheduling,
            deadline=loop.time() + self._message_timeout_seconds,
        )
        await completion

    async def _dispatch(self, entry: QueueEntry, permit: BulkheadPermit) -> None:
        work = entry.payload
        if work.message is None:
            await work.execution.dispatch_open(local_deadline=entry.deadline)
            return
        try:
            await work.execution.dispatch_message(
                work.message,
                work.permit,
                local_deadline=entry.deadline,
            )
        except BaseException as exc:
            if work.completion is not None and not work.completion.done():
                work.completion.set_exception(exc)
            raise
        else:
            if work.completion is not None and not work.completion.done():
                work.completion.set_result(None)

    async def _dispatch_timeout(self, entry: QueueEntry) -> None:
        work = entry.payload
        if work.message is None:
            await work.execution.fail_open("bulkhead deadline exceeded")
        elif work.completion is not None and not work.completion.done():
            work.completion.set_exception(TimeoutError("session message deadline exceeded"))

    @staticmethod
    def _identity(entry: QueueEntry) -> BulkheadIdentity:
        transport = entry.payload.execution.transport
        return BulkheadIdentity(
            provider=transport.provider,
            endpoint=transport.endpoint_id,
            wire_protocol=transport.wire_protocol,
        )


class _SessionExecution:
    def __init__(
        self,
        *,
        request: BoundExecutionRequest,
        transport: SessionTransport,
        session_receipts: SessionReceiptStore,
        wire_receipts: AttemptReceiptStore,
        usage_ledger: UsageLedger,
        reserve_open_units: Callable[[BoundExecutionRequest], int],
        reserve_message_units: Callable[[SessionApplicationMessage], int],
        provider_quota: ProviderQuotaAuthorityPort,
        credential_health: CredentialHealthAuthorityPort,
        permit_verifier: WirePermitVerifier,
        permit_audience: str,
        epoch_provider: Callable[[], tuple[int, int]],
        generation_lease: GatewayGenerationLease,
        event_capacity: int,
        submit_message: SubmitMessage,
        on_terminal: Callable[[str], None],
    ) -> None:
        self.request = request
        self.transport = transport
        self._session_receipts = session_receipts
        self._wire_receipts = wire_receipts
        self._usage_ledger = usage_ledger
        self._reserve_open_units = reserve_open_units
        self._reserve_message_units = reserve_message_units
        self._provider_quota = provider_quota
        self._credential_health = credential_health
        self._permit_verifier = permit_verifier
        self._permit_audience = permit_audience
        self._epoch_provider = epoch_provider
        self._generation_lease = generation_lease
        self._submit_message = submit_message
        self._on_terminal = on_terminal
        self._events: asyncio.Queue[SessionLifecycleEvent] = asyncio.Queue(event_capacity)
        self._open_authorization = asyncio.Event()
        self._open_permit: WirePermit | None = None
        self._session_receipt: SessionReceipt | None = None
        self._active_wire_receipt: AttemptReceipt | None = None
        self._connection: ProviderSessionConnection | None = None
        self._inbound_task: asyncio.Task[None] | None = None
        self._receipt_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._sequence = 0
        self._terminal = False
        self._wire_started = False
        self._response_started = False
        self._lease_released = False

    @property
    def terminal(self) -> bool:
        return self._terminal

    def __aiter__(self) -> "_SessionExecution":
        return self

    async def __anext__(self) -> SessionLifecycleEvent:
        if self._terminal and self._events.empty():
            raise StopAsyncIteration
        return await self._events.get()

    async def accept(self) -> None:
        self._session_receipt = await self._session_receipts.accept(
            SessionReceipt(
                session_id=self.request.execution_id,
                generation_id=self.request.generation_id,
                generation_artifact_digest=self.request.generation_artifact_digest,
                endpoint_binding_id=self.request.endpoint_binding_id,
                revision=1,
                fencing_token=1,
                state=SessionReceiptState.ACCEPTED,
            )
        )
        self._active_wire_receipt = await self._accept_wire_receipt(
            self.request.execution_id,
            self.request.operation,
            "session_open",
            self.request.model_dump_json(exclude={"principal"}),
        )

    async def authorize_open(self, permit: WirePermit) -> None:
        await self._validate_permit(
            permit,
            wire_id=self.request.execution_id,
            wire_unit=self.request.operation,
        )
        if self._open_permit is not None and self._open_permit != permit:
            raise AttemptProtocolViolation("conflicting session open permit")
        self._open_permit = permit
        self._open_authorization.set()

    async def send(self, message: SessionApplicationMessage, permit: WirePermit) -> None:
        async with self._send_lock:
            receipt = self._require_session_receipt()
            if receipt.state is not SessionReceiptState.OPEN:
                raise AttemptProtocolViolation("session is not open")
            if message.session_id != self.request.execution_id:
                raise AttemptProtocolViolation("session message identity mismatch")
            if message.sequence != receipt.next_outbound_sequence:
                raise AttemptProtocolViolation("session message sequence is not next")
            await self._validate_permit(
                permit,
                wire_id=f"{message.session_id}:{message.sequence}",
                wire_unit=message.message_type,
            )
            await self._submit_message(self, message, permit)

    async def close(self, reason: str) -> None:
        if not reason:
            raise ValueError("session close reason is required")
        if self._terminal:
            return
        receipt = self._require_session_receipt()
        if receipt.state is SessionReceiptState.OPEN:
            await self._transition_session(SessionReceiptState.CLOSING)
        if self._connection is not None:
            await self._connection.close(reason)
        if self._inbound_task is not None:
            self._inbound_task.cancel()
            await asyncio.gather(self._inbound_task, return_exceptions=True)
        await self._transition_session(SessionReceiptState.CLOSED)
        await self.emit_event(
            SessionEventType.CLOSED,
            {"reason": reason},
            terminal=True,
        )

    async def dispatch_open(self, *, local_deadline: float) -> None:
        reservation: BudgetReservation | None = None
        committed = False
        try:
            remaining = local_deadline - asyncio.get_running_loop().time()
            units = self._reserve_open_units(self.request)
            if remaining <= 0 or units <= 0:
                raise AttemptProtocolViolation("session open admission rejected")
            await self._governance_allow(units)
            reservation = await self._usage_ledger.reserve(
                reservation_id=f"session:{self.request.execution_id}:open",
                attempt_id=f"{self.request.execution_id}:open",
                tenant_id=self.request.principal.tenant_id,
                project_id=self.request.principal.project_id,
                units=units,
                ttl_seconds=remaining,
            )
            await self.emit_event(SessionEventType.OPEN_AUTHORIZATION_REQUIRED)
            await asyncio.wait_for(self._open_authorization.wait(), timeout=remaining)
            if self._open_permit is None:
                raise AttemptProtocolViolation("session open authorization has no permit")
            await self._commit_wire(self._open_permit)
            committed = True
            await self._transition_session(
                SessionReceiptState.OPEN_SEND_COMMITTED,
                open_permit_digest=self._permit_digest(self._open_permit),
            )
            self._reset_wire_lifecycle()
            outcome = await self.transport.open_once(
                self.request,
                local_deadline=local_deadline,
                lifecycle=self,
            )
            if not self._wire_started or not self._response_started:
                raise AttemptProtocolViolation("session transport omitted open lifecycle")
            await self._observe_result(outcome.wire_result)
            await self._finalize_budget(
                reservation,
                outcome.wire_result.usage_units,
                "open",
            )
            reservation = None
            await self._transition_wire(ReceiptState.TERMINAL_SUCCEEDED)
            self._connection = outcome.connection
            await self._transition_session(SessionReceiptState.OPEN)
            self._inbound_task = asyncio.create_task(
                self._pump_inbound(),
                name=f"session-inbound-{self.request.execution_id}",
            )
            await self.emit_event(
                SessionEventType.OPENED,
                {"result": outcome.wire_result.payload},
            )
        except Exception as exc:
            if reservation is not None:
                if committed:
                    await self._usage_ledger.pending_reconciliation(
                        reservation,
                        settlement_id=f"session:{self.request.execution_id}:open:pending",
                    )
                else:
                    await self._usage_ledger.release(
                        reservation,
                        settlement_id=f"session:{self.request.execution_id}:open:released",
                    )
            await self._finalize_failed_wire(committed)
            await self.fail_open(type(exc).__name__, committed=committed)

    async def dispatch_message(
        self,
        message: SessionApplicationMessage,
        permit: WirePermit | None,
        *,
        local_deadline: float,
    ) -> None:
        if permit is None or self._connection is None:
            raise AttemptProtocolViolation("session message transport is unavailable")
        reservation: BudgetReservation | None = None
        committed = False
        wire_id = f"{message.session_id}:{message.sequence}"
        try:
            remaining = local_deadline - asyncio.get_running_loop().time()
            units = self._reserve_message_units(message)
            if remaining <= 0 or units <= 0:
                raise AttemptProtocolViolation("session message admission rejected")
            await self._governance_allow(units)
            reservation = await self._usage_ledger.reserve(
                reservation_id=f"session:{wire_id}",
                attempt_id=wire_id,
                tenant_id=self.request.principal.tenant_id,
                project_id=self.request.principal.project_id,
                units=units,
                ttl_seconds=remaining,
            )
            self._active_wire_receipt = await self._accept_wire_receipt(
                wire_id,
                message.message_type,
                "session_message",
                message.model_dump_json(),
            )
            await self._commit_wire(permit)
            committed = True
            self._reset_wire_lifecycle()
            result = await self._connection.send_once(
                message,
                local_deadline=local_deadline,
                lifecycle=self,
            )
            if not self._wire_started or not self._response_started:
                raise AttemptProtocolViolation("session message omitted wire lifecycle")
            await self._observe_result(result)
            await self._finalize_budget(reservation, result.usage_units, wire_id)
            reservation = None
            await self._transition_wire(ReceiptState.TERMINAL_SUCCEEDED)
            receipt = self._require_session_receipt()
            await self._transition_session(
                SessionReceiptState.OPEN,
                next_outbound_sequence=receipt.next_outbound_sequence + 1,
            )
            await self.emit_event(
                SessionEventType.MESSAGE_SENT,
                {"application_sequence": message.sequence, "result": result.payload},
            )
        except Exception:
            if reservation is not None:
                if committed:
                    await self._usage_ledger.pending_reconciliation(
                        reservation,
                        settlement_id=f"session:{wire_id}:pending",
                    )
                else:
                    await self._usage_ledger.release(
                        reservation,
                        settlement_id=f"session:{wire_id}:released",
                    )
            await self._finalize_failed_wire(committed)
            if committed:
                await self._transition_session(SessionReceiptState.IN_DOUBT)
                await self.emit_event(
                    SessionEventType.IN_DOUBT,
                    {"wire_id": wire_id},
                    terminal=True,
                )
            raise

    async def fail_open(self, reason: str, *, committed: bool = False) -> None:
        if self._terminal:
            return
        state = SessionReceiptState.IN_DOUBT if committed else SessionReceiptState.FAILED
        event = SessionEventType.IN_DOUBT if committed else SessionEventType.FAILED
        try:
            await self._transition_session(state)
        except ValueError:
            if committed:
                raise
        await self.emit_event(event, {"reason": reason}, terminal=True)

    async def wire_started(self) -> None:
        if self._wire_started or not self._wire_committed:
            raise AttemptProtocolViolation("session wire_started is out of order")
        self._wire_started = True
        await self._transition_wire(ReceiptState.WIRE_STARTED_OBSERVED)

    async def response_started(self) -> None:
        if not self._wire_started or self._response_started:
            raise AttemptProtocolViolation("session response_started is out of order")
        self._response_started = True
        await self._transition_wire(ReceiptState.PROVIDER_ACK)

    async def emit_event(
        self,
        event_type: SessionEventType,
        payload: dict[str, Any] | None = None,
        *,
        terminal: bool = False,
    ) -> None:
        if self._terminal:
            raise AttemptProtocolViolation("session event emitted after terminal")
        self._sequence += 1
        receipt = self._require_session_receipt()
        await self._events.put(
            SessionLifecycleEvent(
                session_id=self.request.execution_id,
                sequence=self._sequence,
                receipt_revision=receipt.revision,
                generation_id=self.request.generation_id,
                event_type=event_type,
                payload=payload or {},
            )
        )
        if terminal:
            self._terminal = True
            self._release_generation()
            self._on_terminal(self.request.execution_id)

    async def _pump_inbound(self) -> None:
        connection = self._connection
        if connection is None:
            return
        try:
            async for payload in connection.inbound():
                receipt = self._require_session_receipt()
                await self._transition_session(
                    SessionReceiptState.OPEN,
                    last_inbound_sequence=receipt.last_inbound_sequence + 1,
                )
                await self.emit_event(
                    SessionEventType.MESSAGE_RECEIVED,
                    {
                        "application_sequence": receipt.last_inbound_sequence + 1,
                        "message": payload,
                    },
                )
            if not self._terminal:
                await self._transition_session(SessionReceiptState.IN_DOUBT)
                await self.emit_event(
                    SessionEventType.IN_DOUBT,
                    {"reason": "provider session ended without close"},
                    terminal=True,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self._terminal:
                await self._transition_session(SessionReceiptState.IN_DOUBT)
                await self.emit_event(
                    SessionEventType.IN_DOUBT,
                    {"reason": type(exc).__name__},
                    terminal=True,
                )

    async def _governance_allow(self, units: int) -> None:
        if not await self._credential_health.allow(self.request.credential_slot_id, self.request.credential_version):
            raise AttemptProtocolViolation("credential health rejected session wire")
        if not await self._provider_quota.allow(
            self.transport.provider,
            self.transport.endpoint_id,
            self.request.credential_slot_id,
            estimated_tokens=units,
        ):
            raise AttemptProtocolViolation("provider quota rejected session wire")

    async def _observe_result(self, result) -> None:
        if result.quota_observation is not None:
            await self._provider_quota.observe(result.quota_observation)
        if result.credential_observation is not None:
            await self._credential_health.observe(result.credential_observation)

    async def _finalize_budget(
        self,
        reservation: BudgetReservation,
        actual_units: int | None,
        suffix: str,
    ) -> None:
        if actual_units is None:
            await self._usage_ledger.pending_reconciliation(
                reservation,
                settlement_id=f"session:{suffix}:pending",
            )
        else:
            await self._usage_ledger.settle(
                reservation,
                settlement_id=f"session:{suffix}:settled",
                actual_units=actual_units,
            )

    async def _accept_wire_receipt(
        self,
        wire_id: str,
        operation: str,
        idempotency_class: str,
        request_payload: str,
    ) -> AttemptReceipt:
        digest = hashlib.sha256(request_payload.encode()).hexdigest()
        return await self._wire_receipts.accept(
            AttemptReceipt(
                attempt_id=wire_id,
                generation_id=self.request.generation_id,
                generation_artifact_digest=self.request.generation_artifact_digest,
                revision=1,
                state=ReceiptState.ACCEPTED,
                fencing_token=1,
                request_digest=f"sha256:{digest}",
                operation=operation,
                idempotency_class=idempotency_class,
            )
        )

    async def _commit_wire(self, permit: WirePermit) -> None:
        await self._transition_wire(ReceiptState.SEND_INTENT_DURABLE)
        await self._transition_wire(
            ReceiptState.SEND_COMMITTED,
            permit_digest=self._permit_digest(permit),
            permit_ordinal=permit.ordinal,
        )

    async def _finalize_failed_wire(self, committed: bool) -> None:
        receipt = self._active_wire_receipt
        if receipt is None or receipt.state in {
            ReceiptState.TERMINAL_SUCCEEDED,
            ReceiptState.TERMINAL_FAILED,
            ReceiptState.TERMINAL_CANCELLED,
            ReceiptState.IN_DOUBT,
        }:
            return
        await self._transition_wire(ReceiptState.IN_DOUBT if committed else ReceiptState.TERMINAL_FAILED)

    async def _transition_wire(self, state: ReceiptState, **changes: Any) -> None:
        current = self._active_wire_receipt
        if current is None:
            raise AttemptProtocolViolation("session wire receipt is missing")
        updated = current.model_copy(
            update={
                "revision": current.revision + 1,
                "state": state,
                "updated_at": datetime.now(timezone.utc),
                **changes,
            }
        )
        self._active_wire_receipt = await self._wire_receipts.compare_and_swap(
            updated,
            expected_revision=current.revision,
            fencing_token=current.fencing_token,
        )

    async def _transition_session(self, state: SessionReceiptState, **changes: Any) -> None:
        async with self._receipt_lock:
            current = self._require_session_receipt()
            updated = current.model_copy(
                update={
                    "revision": current.revision + 1,
                    "state": state,
                    "updated_at": datetime.now(timezone.utc),
                    **changes,
                }
            )
            self._session_receipt = await self._session_receipts.compare_and_swap(
                updated,
                expected_revision=current.revision,
                fencing_token=current.fencing_token,
            )

    async def _validate_permit(
        self,
        permit: WirePermit,
        *,
        wire_id: str,
        wire_unit: str,
    ) -> None:
        if not await self._permit_verifier.verify(permit):
            raise AttemptProtocolViolation("session wire permit signature rejected")
        if (
            permit.attempt_id != wire_id
            or permit.execution_taxonomy is not ExecutionTaxonomy.LONG_LIVED_SESSION
            or permit.owner_journal_id != self.request.owner_journal_id
            or permit.wire_unit != wire_unit
        ):
            raise AttemptProtocolViolation("session wire permit binding mismatch")
        if (
            permit.generation_id != self.request.generation_id
            or permit.generation_artifact_digest != self.request.generation_artifact_digest
            or permit.audience != self._permit_audience
        ):
            raise AttemptProtocolViolation("session wire permit generation mismatch")
        now = datetime.now(timezone.utc)
        if now < permit.not_before or now >= permit.expires_at:
            raise AttemptProtocolViolation("session wire permit validity rejected")
        backup_epoch, admission_epoch = self._epoch_provider()
        if permit.backup_epoch != backup_epoch or permit.admission_epoch != admission_epoch:
            raise AttemptProtocolViolation("session wire permit epoch is stale")

    def _require_session_receipt(self) -> SessionReceipt:
        if self._session_receipt is None:
            raise AttemptProtocolViolation("session receipt is missing")
        return self._session_receipt

    def _reset_wire_lifecycle(self) -> None:
        self._wire_started = False
        self._response_started = False

    @property
    def _wire_committed(self) -> bool:
        return self._active_wire_receipt is not None and self._active_wire_receipt.state not in {
            ReceiptState.ACCEPTED,
            ReceiptState.SEND_INTENT_DURABLE,
        }

    @staticmethod
    def _permit_digest(permit: WirePermit) -> str:
        return "sha256:" + hashlib.sha256(permit.model_dump_json().encode()).hexdigest()

    def _release_generation(self) -> None:
        if not self._lease_released:
            self._lease_released = True
            self._generation_lease.release()
