"""Embedded single-wire data plane for durable service commands."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, cast

from mote.contracts.inference.events import AttemptEventType, AttemptLifecycleEvent
from mote.contracts.inference.executions import BoundExecutionRequest
from mote.contracts.inference.governance import BudgetReservation
from mote.contracts.inference.receipt import AttemptReceipt, ReceiptState
from mote.contracts.inference.wire_permit import ExecutionTaxonomy, WirePermit
from mote.contracts.ports.inference.attempt_receipt import AttemptReceiptStore
from mote.contracts.ports.inference.command_transport import BoundCommandTransport, BoundCommandTransportResolver
from mote.contracts.ports.inference.credential_health import CredentialHealthAuthorityPort
from mote.contracts.ports.inference.provider_quota import ProviderQuotaAuthorityPort
from mote.contracts.ports.inference.transfer_transport import ProviderTransferPartTransportResolver
from mote.contracts.ports.inference.usage_ledger import UsageLedger
from mote.contracts.ports.inference.wire_permit import WirePermitVerifier
from mote.runtime.inference.bulkhead import BulkheadController, BulkheadIdentity, BulkheadPermit
from mote.runtime.inference.dispatcher import Dispatcher
from mote.runtime.inference.fair_queue import FairAdmissionQueue, QueueEntry
from mote.runtime.inference.generation import GatewayGenerationLease, GatewayGenerationOwner, GenerationDomain
from mote.runtime.inference.runtime import AttemptProtocolViolation


class EmbeddedServiceCommandRuntime:
    _generation_domain = GenerationDomain.SERVICE
    _execution_taxonomy = ExecutionTaxonomy.DURABLE_OPERATION
    _reservation_namespace = "command"
    _idempotency_class = "durable_command"

    def __init__(
        self,
        *,
        receipts: AttemptReceiptStore,
        usage_ledger: UsageLedger,
        reserve_units: Callable[[BoundExecutionRequest], int],
        provider_quota: ProviderQuotaAuthorityPort,
        credential_health: CredentialHealthAuthorityPort,
        permit_verifier: WirePermitVerifier,
        transports: (BoundCommandTransportResolver | ProviderTransferPartTransportResolver),
        generations: GatewayGenerationOwner,
        permit_audience: str,
        epoch_provider: Callable[[], tuple[int, int]],
        queue_capacity: int = 5000,
        event_capacity: int = 256,
        worker_count: int = 16,
        global_in_flight: int = 1000,
        provider_in_flight: int = 100,
        endpoint_in_flight: int = 100,
        clock_skew_guard_seconds: float = 0.0,
    ) -> None:
        if event_capacity <= 0 or not permit_audience:
            raise ValueError("event capacity and permit audience are required")
        self._receipts = receipts
        self._usage_ledger = usage_ledger
        self._reserve_units = reserve_units
        self._provider_quota = provider_quota
        self._credential_health = credential_health
        self._permit_verifier = permit_verifier
        self._transports = transports
        self._generations = generations
        self._permit_audience = permit_audience
        self._epoch_provider = epoch_provider
        self._event_capacity = event_capacity
        self._clock_skew_guard_seconds = clock_skew_guard_seconds
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
        self._executions: dict[str, _CommandExecution] = {}
        self._started = False
        self._draining = False
        self._idle = asyncio.Event()
        self._idle.set()

    async def start_command(self, request: BoundExecutionRequest) -> "_CommandExecution":
        return await self._start(request)

    async def _start(self, request: BoundExecutionRequest) -> "_CommandExecution":
        if self._draining:
            raise RuntimeError("command runtime is draining")
        existing = self._executions.get(request.execution_id)
        if existing is not None:
            if existing.request != request:
                raise AttemptProtocolViolation("execution id reused with a different command request")
            return existing
        if not self._started:
            self._dispatcher.start()
            self._started = True
        lease = self._generations.acquire(self._generation_domain)
        if lease.generation_id != request.generation_id or lease.artifact_digest != request.generation_artifact_digest:
            lease.release()
            raise AttemptProtocolViolation("command generation is not active")
        execution = _CommandExecution(
            request=request,
            transport=self._resolve_transport(request),
            receipts=self._receipts,
            usage_ledger=self._usage_ledger,
            reserve_units=self._reserve_units,
            provider_quota=self._provider_quota,
            credential_health=self._credential_health,
            permit_verifier=self._permit_verifier,
            event_capacity=self._event_capacity,
            generation_lease=lease,
            permit_audience=self._permit_audience,
            epoch_provider=self._epoch_provider,
            execution_taxonomy=self._execution_taxonomy,
            reservation_namespace=self._reservation_namespace,
            idempotency_class=self._idempotency_class,
            on_terminal=self._execution_terminal,
        )
        self._executions[request.execution_id] = execution
        self._idle.clear()
        try:
            await execution.accept()
            deadline = request.deadline.to_local_deadline(clock_skew_guard_seconds=self._clock_skew_guard_seconds)
            await self._queue.enqueue(
                execution,
                tenant_id=request.principal.tenant_id,
                project_id=request.principal.project_id,
                scheduling=request.scheduling,
                deadline=deadline,
            )
        except BaseException:
            lease.release()
            del self._executions[request.execution_id]
            raise
        await execution.emit_event(AttemptEventType.QUEUED)
        return execution

    def _resolve_transport(self, request: BoundExecutionRequest) -> BoundCommandTransport:
        resolver = cast(BoundCommandTransportResolver, self._transports)
        return resolver.resolve_command(request)

    async def drain(self, *, timeout_seconds: float) -> None:
        if timeout_seconds <= 0:
            raise ValueError("drain timeout must be positive")
        self._draining = True
        try:
            async with asyncio.timeout(timeout_seconds):
                await self._idle.wait()
        except TimeoutError as exc:
            raise TimeoutError("command runtime drain timed out") from exc
        if self._started:
            await self._dispatcher.drain(timeout_seconds=timeout_seconds)

    async def aclose(self) -> None:
        self._draining = True
        if self._started:
            await self._dispatcher.aclose()

    def _execution_terminal(self, execution_id: str) -> None:
        self._executions.pop(execution_id, None)
        if not self._executions:
            self._idle.set()

    async def _dispatch(self, entry: QueueEntry, permit: BulkheadPermit) -> None:
        await entry.payload.dispatch(local_deadline=entry.deadline)

    async def _dispatch_timeout(self, entry: QueueEntry) -> None:
        await entry.payload.dispatch_timeout()

    @staticmethod
    def _identity(entry: QueueEntry) -> BulkheadIdentity:
        transport = entry.payload.transport
        return BulkheadIdentity(
            provider=transport.provider,
            endpoint=transport.endpoint_id,
            wire_protocol=transport.wire_protocol,
        )


class _CommandExecution:
    def __init__(
        self,
        *,
        request: BoundExecutionRequest,
        transport: BoundCommandTransport,
        receipts: AttemptReceiptStore,
        usage_ledger: UsageLedger,
        reserve_units: Callable[[BoundExecutionRequest], int],
        provider_quota: ProviderQuotaAuthorityPort,
        credential_health: CredentialHealthAuthorityPort,
        permit_verifier: WirePermitVerifier,
        event_capacity: int,
        generation_lease: GatewayGenerationLease,
        permit_audience: str,
        epoch_provider: Callable[[], tuple[int, int]],
        execution_taxonomy: ExecutionTaxonomy,
        reservation_namespace: str,
        idempotency_class: str,
        on_terminal: Callable[[str], None],
    ) -> None:
        self.request = request
        self.transport = transport
        self._receipts = receipts
        self._usage_ledger = usage_ledger
        self._reserve_units = reserve_units
        self._provider_quota = provider_quota
        self._credential_health = credential_health
        self._permit_verifier = permit_verifier
        self._events: asyncio.Queue[AttemptLifecycleEvent] = asyncio.Queue(event_capacity)
        self._authorization = asyncio.Event()
        self._permit: WirePermit | None = None
        self._receipt: AttemptReceipt | None = None
        self._reservation: BudgetReservation | None = None
        self._budget_finalized = False
        self._sequence = 0
        self._terminal = False
        self._cancel_reason: str | None = None
        self._wire_started = False
        self._response_started = False
        self._generation_lease = generation_lease
        self._lease_released = False
        self._permit_audience = permit_audience
        self._epoch_provider = epoch_provider
        self._execution_taxonomy = execution_taxonomy
        self._reservation_namespace = reservation_namespace
        self._idempotency_class = idempotency_class
        self._on_terminal = on_terminal

    def __aiter__(self) -> "_CommandExecution":
        return self

    async def __anext__(self) -> AttemptLifecycleEvent:
        if self._terminal and self._events.empty():
            raise StopAsyncIteration
        return await self._events.get()

    async def accept(self) -> None:
        digest = hashlib.sha256(self.request.model_dump_json(exclude={"principal"}).encode()).hexdigest()
        self._receipt = await self._receipts.accept(
            AttemptReceipt(
                attempt_id=self.request.execution_id,
                generation_id=self.request.generation_id,
                generation_artifact_digest=self.request.generation_artifact_digest,
                revision=1,
                state=ReceiptState.ACCEPTED,
                fencing_token=1,
                request_digest=f"sha256:{digest}",
                operation=self.request.operation,
                idempotency_class=self._idempotency_class,
            )
        )

    async def authorize_wire(self, permit: WirePermit) -> None:
        if not await self._permit_verifier.verify(permit):
            raise AttemptProtocolViolation("command permit signature rejected")
        self._validate_permit(permit)
        if self._permit is not None and self._permit != permit:
            raise AttemptProtocolViolation("conflicting command wire permit")
        self._permit = permit
        self._authorization.set()

    async def cancel(self, reason: str) -> None:
        if not reason:
            raise ValueError("cancellation reason is required")
        self._cancel_reason = reason
        self._authorization.set()

    async def dispatch(self, *, local_deadline: float) -> None:
        try:
            remaining = local_deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError("command deadline exceeded before admission")
            units = self._reserve_units(self.request)
            if units <= 0:
                raise AttemptProtocolViolation("reserved usage units must be positive")
            if not await self._credential_health.allow(
                self.request.credential_slot_id, self.request.credential_version
            ):
                raise AttemptProtocolViolation("credential health rejected command")
            if not await self._provider_quota.allow(
                self.transport.provider,
                self.transport.endpoint_id,
                self.request.credential_slot_id,
                estimated_tokens=units,
            ):
                raise AttemptProtocolViolation("provider quota rejected command")
            self._reservation = await self._usage_ledger.reserve(
                reservation_id=(f"{self._reservation_namespace}:{self.request.execution_id}"),
                attempt_id=self.request.execution_id,
                tenant_id=self.request.principal.tenant_id,
                project_id=self.request.principal.project_id,
                units=units,
                ttl_seconds=remaining,
            )
            await self.emit_event(AttemptEventType.BUDGET_RESERVED)
            await self.emit_event(AttemptEventType.DISPATCHED)
            await self.emit_event(AttemptEventType.WIRE_PREPARED)
            await self.emit_event(AttemptEventType.WIRE_AUTHORIZATION_REQUIRED)
            remaining = local_deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError("command deadline exceeded before authorization")
            await asyncio.wait_for(self._authorization.wait(), timeout=remaining)
            if self._cancel_reason is not None:
                await self._release_budget()
                await self._transition(ReceiptState.TERMINAL_CANCELLED)
                await self.emit_event(
                    AttemptEventType.CANCELLED,
                    {"reason": self._cancel_reason},
                    terminal=True,
                )
                return
            if self._permit is None:
                raise AttemptProtocolViolation("command authorization has no permit")
            await self._transition(ReceiptState.SEND_INTENT_DURABLE)
            await self._transition(
                ReceiptState.SEND_COMMITTED,
                permit_digest=self._permit_digest(self._permit),
                permit_ordinal=self._permit.ordinal,
            )
            await self.emit_event(AttemptEventType.SEND_COMMITTED)
            result = await self.transport.execute_once(
                self.request,
                local_deadline=local_deadline,
                lifecycle=self,
            )
            if not self._wire_started or not self._response_started:
                raise AttemptProtocolViolation("command transport omitted wire lifecycle")
            if result.quota_observation is not None:
                await self._provider_quota.observe(result.quota_observation)
            if result.credential_observation is not None:
                await self._credential_health.observe(result.credential_observation)
            if result.usage_units is None:
                await self._pending_budget()
            else:
                await self._settle_budget(result.usage_units)
            await self._transition(ReceiptState.TERMINAL_SUCCEEDED)
            await self.emit_event(
                AttemptEventType.SUCCEEDED,
                {"result": result.payload},
                terminal=True,
            )
        except asyncio.CancelledError:
            await self._fail(ReceiptState.TERMINAL_CANCELLED, AttemptEventType.CANCELLED)
            raise
        except Exception as exc:
            state = ReceiptState.IN_DOUBT if self._receipt_committed else ReceiptState.TERMINAL_FAILED
            event = AttemptEventType.IN_DOUBT if state is ReceiptState.IN_DOUBT else AttemptEventType.FAILED
            await self._fail(state, event, type(exc).__name__)

    async def dispatch_timeout(self) -> None:
        await self._fail(
            ReceiptState.TERMINAL_FAILED,
            AttemptEventType.FAILED,
            "bulkhead deadline exceeded",
        )

    async def wire_started(self) -> None:
        if self._wire_started or not self._receipt_committed:
            raise AttemptProtocolViolation("command wire_started is out of order")
        self._wire_started = True
        await self._transition(ReceiptState.WIRE_STARTED_OBSERVED)
        await self.emit_event(AttemptEventType.WIRE_STARTED)

    async def response_started(self) -> None:
        if not self._wire_started or self._response_started:
            raise AttemptProtocolViolation("command response_started is out of order")
        self._response_started = True
        await self._transition(ReceiptState.PROVIDER_ACK)
        await self.emit_event(AttemptEventType.RESPONSE_STARTED)

    async def emit_event(
        self,
        event_type: AttemptEventType,
        payload: dict[str, Any] | None = None,
        *,
        terminal: bool = False,
    ) -> None:
        if self._terminal:
            raise AttemptProtocolViolation("command event emitted after terminal")
        self._sequence += 1
        await self._events.put(
            AttemptLifecycleEvent(
                attempt_id=self.request.execution_id,
                sequence=self._sequence,
                receipt_revision=self._receipt.revision if self._receipt else 1,
                generation_id=self.request.generation_id,
                event_type=event_type,
                payload=payload or {},
            )
        )
        if terminal:
            self._terminal = True
            self._release_generation()
            self._on_terminal(self.request.execution_id)

    async def _fail(
        self,
        state: ReceiptState,
        event: AttemptEventType,
        reason: str = "cancelled",
    ) -> None:
        if self._terminal:
            return
        try:
            if self._receipt_committed:
                await self._pending_budget()
            else:
                await self._release_budget()
        except Exception as budget_error:
            reason += f"; budget:{type(budget_error).__name__}"
            if self._receipt_committed:
                state = ReceiptState.IN_DOUBT
                event = AttemptEventType.IN_DOUBT
        try:
            await self._transition(state)
        except ValueError:
            if self._receipt_committed and state is not ReceiptState.IN_DOUBT:
                await self._transition(ReceiptState.IN_DOUBT)
                event = AttemptEventType.IN_DOUBT
        await self.emit_event(event, {"reason": reason}, terminal=True)

    async def _settle_budget(self, actual_units: int) -> None:
        reservation = self._require_reservation()
        await self._usage_ledger.settle(
            reservation,
            settlement_id=(f"{self._reservation_namespace}:{self.request.execution_id}:settled"),
            actual_units=actual_units,
        )
        self._budget_finalized = True

    async def _release_budget(self) -> None:
        if self._reservation is None or self._budget_finalized:
            return
        await self._usage_ledger.release(
            self._reservation,
            settlement_id=(f"{self._reservation_namespace}:{self.request.execution_id}:released"),
        )
        self._budget_finalized = True

    async def _pending_budget(self) -> None:
        if self._reservation is None or self._budget_finalized:
            return
        await self._usage_ledger.pending_reconciliation(
            self._reservation,
            settlement_id=(f"{self._reservation_namespace}:{self.request.execution_id}:pending"),
        )
        self._budget_finalized = True

    def _require_reservation(self) -> BudgetReservation:
        if self._reservation is None:
            raise AttemptProtocolViolation("command budget reservation is missing")
        return self._reservation

    async def _transition(self, state: ReceiptState, **changes: Any) -> None:
        if self._receipt is None:
            raise AttemptProtocolViolation("command receipt is not accepted")
        current = self._receipt
        updated = current.model_copy(
            update={
                "revision": current.revision + 1,
                "state": state,
                "updated_at": datetime.now(timezone.utc),
                **changes,
            }
        )
        self._receipt = await self._receipts.compare_and_swap(
            updated,
            expected_revision=current.revision,
            fencing_token=current.fencing_token,
        )

    def _validate_permit(self, permit: WirePermit) -> None:
        if permit.attempt_id != self.request.execution_id:
            raise AttemptProtocolViolation("command permit execution mismatch")
        if permit.owner_journal_id != self.request.owner_journal_id:
            raise AttemptProtocolViolation("command permit owner journal mismatch")
        if permit.wire_unit != self.request.operation:
            raise AttemptProtocolViolation("command permit wire unit mismatch")
        if permit.execution_taxonomy is not self._execution_taxonomy:
            raise AttemptProtocolViolation("command permit taxonomy mismatch")
        if (
            permit.generation_id != self.request.generation_id
            or permit.generation_artifact_digest != self.request.generation_artifact_digest
        ):
            raise AttemptProtocolViolation("command permit generation mismatch")
        if permit.audience != self._permit_audience:
            raise AttemptProtocolViolation("command permit audience mismatch")
        now = datetime.now(timezone.utc)
        if now < permit.not_before or now >= permit.expires_at:
            raise AttemptProtocolViolation("command permit validity window rejected")
        backup_epoch, admission_epoch = self._epoch_provider()
        if permit.backup_epoch != backup_epoch or permit.admission_epoch != admission_epoch:
            raise AttemptProtocolViolation("command permit epoch is stale")

    @staticmethod
    def _permit_digest(permit: WirePermit) -> str:
        return "sha256:" + hashlib.sha256(permit.model_dump_json().encode()).hexdigest()

    @property
    def _receipt_committed(self) -> bool:
        return self._receipt is not None and self._receipt.state not in {
            ReceiptState.ACCEPTED,
            ReceiptState.SEND_INTENT_DURABLE,
        }

    def _release_generation(self) -> None:
        if not self._lease_released:
            self._lease_released = True
            self._generation_lease.release()
