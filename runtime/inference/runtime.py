"""Embedded implementation of the unary InferenceRuntime contract."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from mote.contracts.inference.attempt import InferenceAttemptRequest
from mote.contracts.inference.events import AttemptEventType, AttemptLifecycleEvent
from mote.contracts.inference.governance import (
    BudgetReservation,
    CredentialHealthObservation,
    CredentialHealthVerdict,
    ProviderQuotaObservation,
    QuotaObservationKind,
)
from mote.contracts.inference.receipt import AttemptReceipt, ReceiptState
from mote.contracts.inference.transport import ProviderTransportFailure
from mote.contracts.inference.wire_permit import ExecutionTaxonomy, WirePermit
from mote.contracts.model.failover import CredentialVerdict, QuotaObservation
from mote.contracts.ports.inference.attempt_receipt import AttemptReceiptStore
from mote.contracts.ports.inference.credential_health import CredentialHealthAuthorityPort
from mote.contracts.ports.inference.provider_quota import ProviderQuotaAuthorityPort
from mote.contracts.ports.inference.provider_transport import GenerateTransportResolver
from mote.contracts.ports.inference.usage_ledger import UsageLedger
from mote.contracts.ports.inference.wire_permit import WirePermitVerifier
from mote.runtime.inference.bulkhead import BulkheadController, BulkheadIdentity, BulkheadPermit
from mote.runtime.inference.dispatcher import Dispatcher
from mote.runtime.inference.fair_queue import FairAdmissionQueue, QueueEntry
from mote.runtime.inference.generation import GatewayGenerationLease, GatewayGenerationOwner, GenerationDomain


class AttemptProtocolViolation(RuntimeError):
    pass


class EmbeddedInferenceRuntime:
    def __init__(
        self,
        *,
        receipts: AttemptReceiptStore,
        usage_ledger: UsageLedger,
        reserve_units: Callable[[InferenceAttemptRequest], int],
        provider_quota: ProviderQuotaAuthorityPort,
        credential_health: CredentialHealthAuthorityPort,
        permit_verifier: WirePermitVerifier,
        transports: GenerateTransportResolver,
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
        if event_capacity <= 0:
            raise ValueError("event capacity must be positive")
        self._receipts = receipts
        self._usage_ledger = usage_ledger
        self._reserve_units = reserve_units
        self._provider_quota = provider_quota
        self._credential_health = credential_health
        self._permit_verifier = permit_verifier
        self._transports = transports
        if not permit_audience:
            raise ValueError("permit audience is required")
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
        self._executions: dict[str, _AttemptExecution] = {}
        self._started = False

    async def start_attempt(self, request: InferenceAttemptRequest) -> "_AttemptExecution":
        if request.attempt_id in self._executions:
            existing = self._executions[request.attempt_id]
            if existing.request != request:
                raise AttemptProtocolViolation("attempt id reused with a different request")
            return existing
        if not self._started:
            self._dispatcher.start()
            self._started = True
        generation_lease = self._generations.acquire(GenerationDomain.MODEL)
        if (
            generation_lease.generation_id != request.generation_id
            or generation_lease.artifact_digest != request.generation_artifact_digest
        ):
            generation_lease.release()
            raise AttemptProtocolViolation("request generation is not the active generation")
        execution = _AttemptExecution(
            request=request,
            receipts=self._receipts,
            usage_ledger=self._usage_ledger,
            reserve_units=self._reserve_units,
            provider_quota=self._provider_quota,
            credential_health=self._credential_health,
            permit_verifier=self._permit_verifier,
            transports=self._transports,
            event_capacity=self._event_capacity,
            clock_skew_guard_seconds=self._clock_skew_guard_seconds,
            generation_lease=generation_lease,
            permit_audience=self._permit_audience,
            epoch_provider=self._epoch_provider,
        )
        self._executions[request.attempt_id] = execution
        try:
            await execution.accept()
            local_deadline = request.deadline.to_local_deadline(clock_skew_guard_seconds=self._clock_skew_guard_seconds)
            await self._queue.enqueue(
                execution,
                tenant_id=request.principal.tenant_id,
                project_id=request.principal.project_id,
                scheduling=request.scheduling,
                deadline=local_deadline,
            )
        except BaseException:
            generation_lease.release()
            del self._executions[request.attempt_id]
            raise
        await execution._emit_event(AttemptEventType.QUEUED)
        return execution

    async def drain(self, *, timeout_seconds: float) -> None:
        await self._dispatcher.drain(timeout_seconds=timeout_seconds)

    async def aclose(self) -> None:
        if self._started:
            await self._dispatcher.aclose()
        close = getattr(self._transports, "aclose", None)
        if close is not None:
            await close()

    async def _dispatch(self, entry: QueueEntry, permit: BulkheadPermit) -> None:
        execution = entry.payload
        await execution.dispatch(local_deadline=entry.deadline)

    async def _dispatch_timeout(self, entry: QueueEntry) -> None:
        execution = entry.payload
        await execution.dispatch_timeout()

    @staticmethod
    def _identity(entry: QueueEntry) -> BulkheadIdentity:
        request = entry.payload.request
        return BulkheadIdentity(
            provider=request.endpoint.provider,
            endpoint=request.endpoint.endpoint_id,
            wire_protocol=request.endpoint.transport,
        )


class _AttemptExecution:
    def __init__(
        self,
        *,
        request: InferenceAttemptRequest,
        receipts: AttemptReceiptStore,
        usage_ledger: UsageLedger,
        reserve_units: Callable[[InferenceAttemptRequest], int],
        provider_quota: ProviderQuotaAuthorityPort,
        credential_health: CredentialHealthAuthorityPort,
        permit_verifier: WirePermitVerifier,
        transports: GenerateTransportResolver,
        event_capacity: int,
        clock_skew_guard_seconds: float,
        generation_lease: GatewayGenerationLease,
        permit_audience: str,
        epoch_provider: Callable[[], tuple[int, int]],
    ) -> None:
        self.request = request
        self._receipts = receipts
        self._usage_ledger = usage_ledger
        self._reserve_units = reserve_units
        self._provider_quota = provider_quota
        self._credential_health = credential_health
        self._permit_verifier = permit_verifier
        self._transports = transports
        self._events: asyncio.Queue[AttemptLifecycleEvent] = asyncio.Queue(event_capacity)
        self._authorization = asyncio.Event()
        self._permit: WirePermit | None = None
        self._receipt: AttemptReceipt | None = None
        self._budget_reservation: BudgetReservation | None = None
        self._budget_finalized = False
        self._sequence = 0
        self._terminal = False
        self._cancel_reason: str | None = None
        self._wire_started = False
        self._response_started = False
        self._clock_skew_guard_seconds = clock_skew_guard_seconds
        self._generation_lease = generation_lease
        self._permit_audience = permit_audience
        self._epoch_provider = epoch_provider
        self._lease_released = False

    def __aiter__(self) -> "_AttemptExecution":
        return self

    async def __anext__(self) -> AttemptLifecycleEvent:
        if self._terminal and self._events.empty():
            raise StopAsyncIteration
        event = await self._events.get()
        return event

    async def accept(self) -> None:
        digest = hashlib.sha256(self.request.model_dump_json(exclude={"principal"}).encode()).hexdigest()
        receipt = AttemptReceipt(
            attempt_id=self.request.attempt_id,
            generation_id=self.request.generation_id,
            generation_artifact_digest=self.request.generation_artifact_digest,
            revision=1,
            state=ReceiptState.ACCEPTED,
            fencing_token=1,
            request_digest=f"sha256:{digest}",
            operation=str(self.request.invocation.get("operation", "generate")),
            idempotency_class="attempt",
        )
        self._receipt = await self._receipts.accept(receipt)

    async def authorize_wire(self, permit: WirePermit) -> None:
        if not await self._permit_verifier.verify(permit):
            raise AttemptProtocolViolation("wire permit signature rejected")
        self._validate_permit(permit)
        if self._permit is not None and self._permit != permit:
            raise AttemptProtocolViolation("conflicting wire permit")
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
                raise TimeoutError("attempt deadline exceeded before budget reservation")
            units = self._reserve_units(self.request)
            if units <= 0:
                raise AttemptProtocolViolation("reserved usage units must be positive")
            if not await self._credential_health.allow(
                self.request.credential_slot_id,
                self.request.credential_version,
            ):
                raise AttemptProtocolViolation("credential health rejected admission")
            if not await self._provider_quota.allow(
                self.request.endpoint.provider,
                self.request.endpoint.endpoint_id,
                self.request.credential_slot_id,
                estimated_tokens=units,
            ):
                raise AttemptProtocolViolation("provider quota rejected admission")
            self._budget_reservation = await self._usage_ledger.reserve(
                reservation_id=f"attempt:{self.request.attempt_id}",
                attempt_id=self.request.attempt_id,
                tenant_id=self.request.principal.tenant_id,
                project_id=self.request.principal.project_id,
                units=units,
                ttl_seconds=remaining,
            )
            await self._emit_event(AttemptEventType.BUDGET_RESERVED)
            await self._emit_event(AttemptEventType.DISPATCHED)
            await self._emit_event(AttemptEventType.WIRE_PREPARED)
            await self._emit_event(AttemptEventType.WIRE_AUTHORIZATION_REQUIRED)
            remaining = local_deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError("attempt deadline exceeded before authorization")
            await asyncio.wait_for(self._authorization.wait(), timeout=remaining)
            if self._cancel_reason is not None:
                await self._cancel_before_wire()
                return
            if self._permit is None:
                raise AttemptProtocolViolation("authorization barrier opened without permit")
            await self._transition(ReceiptState.SEND_INTENT_DURABLE)
            await self._transition(
                ReceiptState.SEND_COMMITTED,
                permit_digest=self._permit_digest(self._permit),
                permit_ordinal=self._permit.ordinal,
            )
            await self._emit_event(AttemptEventType.SEND_COMMITTED)
            transport = self._transports.resolve_generate(self.request)
            result = await transport.generate_once(
                self.request,
                local_deadline=local_deadline,
                lifecycle=self,
                stream=self if self.request.stream else None,
            )
            if not self._wire_started:
                raise AttemptProtocolViolation("transport returned without wire_started")
            if result.quota_observation is not None:
                await self._provider_quota.observe(result.quota_observation)
            if result.credential_observation is not None:
                await self._credential_health.observe(result.credential_observation)
            if result.usage_units is None:
                await self._mark_budget_pending()
            else:
                await self._settle_budget(result.usage_units)
            await self._transition(ReceiptState.TERMINAL_SUCCEEDED)
            await self._emit_event(
                AttemptEventType.SUCCEEDED,
                {"result": result.payload},
                terminal=True,
            )
        except asyncio.CancelledError:
            await self._fail_after_dispatch(ReceiptState.TERMINAL_CANCELLED, AttemptEventType.CANCELLED)
            raise
        except Exception as exc:
            observation_error: Exception | None = None
            try:
                await self._observe_transport_failure(exc)
            except Exception as observed_exc:
                observation_error = observed_exc
            state = ReceiptState.IN_DOUBT if self._receipt_committed else ReceiptState.TERMINAL_FAILED
            event_type = AttemptEventType.IN_DOUBT if state is ReceiptState.IN_DOUBT else AttemptEventType.FAILED
            reason = type(exc).__name__
            if observation_error is not None:
                reason += f"; governance:{type(observation_error).__name__}"
            payload: dict[str, Any] = {"reason": reason}
            if isinstance(exc, ProviderTransportFailure):
                payload["disposition"] = exc.disposition.model_dump(mode="json")
                if exc.retry_after_seconds is not None:
                    payload["retry_after_seconds"] = exc.retry_after_seconds
            await self._fail_after_dispatch(state, event_type, reason, payload=payload)

    async def dispatch_timeout(self) -> None:
        await self._fail_after_dispatch(
            ReceiptState.TERMINAL_FAILED,
            AttemptEventType.FAILED,
            "bulkhead deadline exceeded",
        )

    async def wire_started(self) -> None:
        if self._wire_started or not self._receipt_committed:
            raise AttemptProtocolViolation("wire_started duplicated or before send commit")
        self._wire_started = True
        await self._transition(ReceiptState.WIRE_STARTED_OBSERVED)
        await self._emit_event(AttemptEventType.WIRE_STARTED)

    async def response_started(self) -> None:
        if not self._wire_started or self._response_started:
            raise AttemptProtocolViolation("response_started out of order or duplicated")
        self._response_started = True
        await self._emit_event(AttemptEventType.RESPONSE_STARTED)

    async def _emit_event(
        self,
        event_type,
        payload: dict[str, Any] | None = None,
        *,
        terminal: bool = False,
    ) -> None:
        if self._terminal:
            raise AttemptProtocolViolation("event emitted after terminal")
        self._sequence += 1
        receipt_revision = self._receipt.revision if self._receipt is not None else 1
        await self._events.put(
            AttemptLifecycleEvent(
                attempt_id=self.request.attempt_id,
                sequence=self._sequence,
                receipt_revision=receipt_revision,
                generation_id=self.request.generation_id,
                event_type=event_type,
                payload=payload or {},
            )
        )
        if terminal:
            self._terminal = True
            self._release_generation()

    async def emit(self, chunk: dict[str, Any]) -> None:
        if not self._response_started:
            await self.response_started()
        await self._emit_event(AttemptEventType.STREAM_CHUNK, {"chunk": chunk})

    async def _cancel_before_wire(self) -> None:
        await self._release_budget()
        await self._transition(ReceiptState.TERMINAL_CANCELLED)
        await self._emit_event(
            AttemptEventType.CANCELLED,
            {"reason": self._cancel_reason or "cancelled"},
            terminal=True,
        )

    async def _fail_after_dispatch(
        self,
        state,
        event_type,
        reason: str = "cancelled",
        *,
        payload: dict[str, Any] | None = None,
    ) -> None:
        if self._terminal:
            return
        try:
            if self._receipt_committed:
                await self._mark_budget_pending()
            else:
                await self._release_budget()
        except Exception as settlement_error:
            reason = f"{reason}; budget:{type(settlement_error).__name__}"
            if self._receipt_committed:
                state = ReceiptState.IN_DOUBT
                event_type = AttemptEventType.IN_DOUBT
        try:
            await self._transition(state)
        except ValueError:
            if self._receipt_committed and state is not ReceiptState.IN_DOUBT:
                await self._transition(ReceiptState.IN_DOUBT)
                event_type = AttemptEventType.IN_DOUBT
        terminal_payload = dict(payload or {})
        terminal_payload["reason"] = reason
        await self._emit_event(event_type, terminal_payload, terminal=True)

    async def _settle_budget(self, actual_units: int) -> None:
        if actual_units < 0:
            raise AttemptProtocolViolation("actual usage units cannot be negative")
        reservation = self._require_budget_reservation()
        await self._usage_ledger.settle(
            reservation,
            settlement_id=f"attempt:{self.request.attempt_id}:settled",
            actual_units=actual_units,
        )
        self._budget_finalized = True

    async def _release_budget(self) -> None:
        if self._budget_reservation is None or self._budget_finalized:
            return
        await self._usage_ledger.release(
            self._budget_reservation,
            settlement_id=f"attempt:{self.request.attempt_id}:released",
        )
        self._budget_finalized = True

    async def _mark_budget_pending(self) -> None:
        if self._budget_reservation is None or self._budget_finalized:
            return
        await self._usage_ledger.pending_reconciliation(
            self._budget_reservation,
            settlement_id=f"attempt:{self.request.attempt_id}:pending",
        )
        self._budget_finalized = True

    def _require_budget_reservation(self) -> BudgetReservation:
        if self._budget_reservation is None:
            raise AttemptProtocolViolation("budget reservation is missing")
        return self._budget_reservation

    async def _observe_transport_failure(self, exc: Exception) -> None:
        if not isinstance(exc, ProviderTransportFailure):
            return
        disposition = exc.disposition
        if disposition.credential_verdict is not CredentialVerdict.NEUTRAL:
            await self._credential_health.observe(
                CredentialHealthObservation(
                    credential_slot_id=self.request.credential_slot_id,
                    credential_version=self.request.credential_version,
                    verdict=CredentialHealthVerdict(disposition.credential_verdict.value),
                    quarantine_seconds=(
                        60.0 if disposition.credential_verdict is CredentialVerdict.QUARANTINE else None
                    ),
                    reason=disposition.reason.value,
                )
            )
        if disposition.quota_observation is not QuotaObservation.NONE:
            await self._provider_quota.observe(
                ProviderQuotaObservation(
                    provider=self.request.endpoint.provider,
                    endpoint_id=self.request.endpoint.endpoint_id,
                    credential_slot_id=self.request.credential_slot_id,
                    kind=QuotaObservationKind(disposition.quota_observation.value),
                    retry_after_seconds=exc.retry_after_seconds,
                )
            )

    async def _transition(self, state: ReceiptState, **changes) -> None:
        if self._receipt is None:
            raise AttemptProtocolViolation("receipt not accepted")
        current = self._receipt
        receipt = current.model_copy(
            update={
                "revision": current.revision + 1,
                "state": state,
                "updated_at": datetime.now(timezone.utc),
                **changes,
            }
        )
        self._receipt = await self._receipts.compare_and_swap(
            receipt,
            expected_revision=current.revision,
            fencing_token=current.fencing_token,
        )

    def _validate_permit(self, permit: WirePermit) -> None:
        if permit.attempt_id != self.request.attempt_id:
            raise AttemptProtocolViolation("permit attempt mismatch")
        if permit.owner_journal_id != self.request.owner_journal_id:
            raise AttemptProtocolViolation("permit owner journal mismatch")
        if permit.wire_unit != str(self.request.invocation.get("operation", "generate")):
            raise AttemptProtocolViolation("permit wire unit mismatch")
        if permit.execution_taxonomy is not ExecutionTaxonomy.UNARY_FINITE_ATTEMPT:
            raise AttemptProtocolViolation("permit taxonomy mismatch")
        if permit.generation_id != self.request.generation_id:
            raise AttemptProtocolViolation("permit generation mismatch")
        if permit.generation_artifact_digest != self.request.generation_artifact_digest:
            raise AttemptProtocolViolation("permit artifact mismatch")
        if permit.audience != self._permit_audience:
            raise AttemptProtocolViolation("permit audience mismatch")
        now = datetime.now(timezone.utc)
        if now < permit.not_before or now >= permit.expires_at:
            raise AttemptProtocolViolation("permit is outside its validity window")
        backup_epoch, admission_epoch = self._epoch_provider()
        if permit.backup_epoch != backup_epoch or permit.admission_epoch != admission_epoch:
            raise AttemptProtocolViolation("permit epoch is stale")

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
