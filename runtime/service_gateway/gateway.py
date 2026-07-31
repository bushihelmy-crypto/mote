"""Durable failover gateway for externally hosted Tool capabilities."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from mote.contracts.model.failover import (
    AttemptState,
    DecisionKind,
    FailureDisposition,
    FailureDomain,
    FailureReason,
    HealthVerdict,
    ResourceIdentity,
    Retryability,
)
from mote.contracts.ports.service.call_journal import ServiceCallJournal
from mote.contracts.ports.service.endpoint import ServiceEndpointAdapter, ServiceEndpointResolver
from mote.contracts.service import (
    ResolvedServiceResponse,
    ServiceAcceptance,
    ServiceAccepted,
    ServiceAttemptFinishedRecord,
    ServiceAttemptStartedRecord,
    ServiceCallFinishedRecord,
    ServiceCallPlannedRecord,
    ServiceCallRecovery,
    ServiceCallState,
    ServiceCompleted,
    ServiceDecisionRecord,
    ServiceEndpointDescriptor,
    ServiceEndpointFailure,
    ServiceExecutionSemantics,
    ServiceFailed,
    ServiceInvocation,
    ServicePlan,
    ServiceReceipt,
    ServiceReceiptAcceptedRecord,
    ServiceResponse,
)
from mote.contracts.service.errors import (
    ServiceCallDeadlineExceededError,
    ServiceCallExhaustedError,
    ServiceCallInDoubtError,
    ServiceRouteUnavailableError,
)
from mote.runtime.resilience.admission import AdmissionPermit, AdmissionRejectedError, ResourceAdmissionController
from mote.runtime.resilience.failover.policy import DefaultFailoverPolicy, FailoverPolicy
from mote.runtime.service_gateway.planner import ServiceFailoverPlanner
from mote.runtime.telemetry.logging import log_class


@dataclass(frozen=True)
class _Target:
    endpoint: ServiceEndpointDescriptor
    credential_slot_id: str
    adapter: ServiceEndpointAdapter
    endpoint_fingerprint: str


@log_class(level="DEBUG", exclude={"supports_route"})
class RuntimeServiceGateway:
    """Own submit, receipt persistence, polling and bounded failover."""

    def __init__(
        self,
        planner: ServiceFailoverPlanner,
        endpoint_resolver: ServiceEndpointResolver,
        *,
        service_call_journal: ServiceCallJournal | None = None,
        admission_controller: ResourceAdmissionController | None = None,
        policy: FailoverPolicy | None = None,
    ) -> None:
        self._planner = planner
        self._resolver = endpoint_resolver
        if service_call_journal is None:
            raise ValueError("RuntimeServiceGateway requires a service call journal")
        self._journal = service_call_journal
        self._admission = admission_controller or ResourceAdmissionController()
        self._policy = policy or DefaultFailoverPolicy()
        self._locks: dict[str, asyncio.Lock] = {}

    def supports_route(self, route_id: str, capability: str) -> bool:
        try:
            group = self._planner.snapshot.group(route_id)
            if group is None:
                group = self._planner.snapshot.group_for_route(route_id)
            if group is None:
                return False
            return any(
                endpoint is not None and endpoint.capability == capability
                for endpoint_id in group.endpoint_ids
                for endpoint in (self._planner.snapshot.endpoint(endpoint_id),)
            )
        except (TypeError, ValueError):
            return False

    async def execute(
        self,
        invocation: ServiceInvocation,
    ) -> ResolvedServiceResponse:
        return await self._serialized(invocation, resume_only=False)

    async def resume(
        self,
        invocation: ServiceInvocation,
    ) -> ResolvedServiceResponse:
        return await self._serialized(invocation, resume_only=True)

    async def _serialized(
        self,
        invocation: ServiceInvocation,
        *,
        resume_only: bool,
    ) -> ResolvedServiceResponse:
        lock = self._locks.setdefault(invocation.service_call_id, asyncio.Lock())
        try:
            async with lock:
                records = self._journal.records(invocation.service_call_id)
                if records:
                    recovery = self._journal.recover(invocation.service_call_id)
                    self._validate_invocation(invocation, recovery)
                    return await self._resume(invocation, recovery)
                if resume_only:
                    raise ServiceRouteUnavailableError(
                        "service call has no durable journal",
                        service_call_id=invocation.service_call_id,
                    )
                plan = self._planner.plan(invocation)
                await self._journal.append(self._planned(invocation, plan))
                return await self._run_new(invocation, plan, started_at=time.monotonic())
        finally:
            if not lock.locked() and self._locks.get(invocation.service_call_id) is lock:
                self._locks.pop(invocation.service_call_id, None)

    async def _resume(
        self,
        invocation: ServiceInvocation,
        recovery: ServiceCallRecovery,
    ) -> ResolvedServiceResponse:
        terminal = recovery.terminal
        if terminal is not None:
            return self._resolve_terminal(terminal)
        plan = self._plan_for_recovery(invocation, recovery)
        started_at = _monotonic_start(recovery.plan.root_started_at)
        finished_ids = {record.attempt_id for record in recovery.attempt_finishes}
        open_attempt = next(
            (record for record in reversed(recovery.attempt_starts) if record.attempt_id not in finished_ids),
            None,
        )
        if open_attempt is not None:
            target = self._target_for_started(plan, open_attempt)
            receipts = tuple(record for record in recovery.receipts if record.attempt_id == open_attempt.attempt_id)
            if receipts:
                return await self._poll_receipt(
                    invocation,
                    plan,
                    target,
                    open_attempt,
                    receipts[-1].receipt,
                    poll_ordinal=receipts[-1].poll_ordinal,
                    started_at=started_at,
                )
            reconciled = await self._reconcile_open_attempt(
                invocation,
                plan,
                target,
                open_attempt,
                started_at,
            )
            if reconciled is not None:
                return reconciled
        if recovery.attempt_finishes:
            latest_finish = recovery.attempt_finishes[-1]
            latest_start = next(item for item in recovery.attempt_starts if item.attempt_id == latest_finish.attempt_id)
            target = self._target_for_started(plan, latest_start)
            if latest_finish.state is AttemptState.SUCCEEDED:
                assert latest_finish.response is not None
                return await self._checkpoint_success(
                    invocation,
                    target,
                    latest_start,
                    latest_finish.response,
                )
            had_receipt = any(item.attempt_id == latest_finish.attempt_id for item in recovery.receipts)
            if latest_finish.state is AttemptState.CANCELLED:
                terminal = ServiceCallFinishedRecord(
                    service_call_id=invocation.service_call_id,
                    state=ServiceCallState.CANCELLED,
                )
                await self._journal.append(terminal)
                return self._resolve_terminal(terminal)
            if had_receipt:
                failure = latest_finish.failure or _unknown_failure(
                    "accepted service operation ended without a response"
                )
                terminal = ServiceCallFinishedRecord(
                    service_call_id=invocation.service_call_id,
                    state=(
                        ServiceCallState.IN_DOUBT
                        if latest_finish.state is AttemptState.IN_DOUBT
                        else ServiceCallState.FAILED
                    ),
                    failure=failure,
                )
                await self._journal.append(terminal)
                return self._resolve_terminal(terminal)
            if latest_finish.state is AttemptState.IN_DOUBT and not self._may_repeat_after_unknown(
                invocation.semantics
            ):
                terminal = ServiceCallFinishedRecord(
                    service_call_id=invocation.service_call_id,
                    state=ServiceCallState.IN_DOUBT,
                    failure=latest_finish.failure or _unknown_failure("service attempt outcome remains unknown"),
                )
                await self._journal.append(terminal)
                return self._resolve_terminal(terminal)
        return await self._run_new(invocation, plan, started_at=started_at)

    async def _run_new(
        self,
        invocation: ServiceInvocation,
        plan: ServicePlan,
        *,
        started_at: float,
    ) -> ResolvedServiceResponse:
        targets = self._targets(plan)
        if not targets:
            raise ServiceRouteUnavailableError(
                "service plan has no resolvable endpoint credentials",
                service_call_id=invocation.service_call_id,
                plan_id=plan.plan_id,
            )
        recovery = self._journal.recover(invocation.service_call_id)
        ordinal = len(recovery.attempt_starts)
        attempts_by_endpoint: dict[str, int] = {}
        for start in recovery.attempt_starts:
            attempts_by_endpoint[start.endpoint_id] = attempts_by_endpoint.get(start.endpoint_id, 0) + 1
        target_index = 0
        switches = 0
        rotations = 0
        last_failure: FailureDisposition | None = None
        cross_endpoint_safe = invocation.semantics is ServiceExecutionSemantics.PURE
        admission_rejections = 0
        max_admission_rejections = plan.budget.max_credential_rotations + plan.budget.max_endpoint_switches + 1
        while ordinal < plan.budget.max_wire_attempts:
            self._ensure_deadline(plan, started_at, invocation.service_call_id)
            target = targets[target_index]
            if attempts_by_endpoint.get(target.endpoint.endpoint_id, 0) >= plan.budget.max_attempts_per_endpoint:
                next_index = self._next_endpoint_index(targets, target_index)
                if not cross_endpoint_safe or next_index is None or switches >= plan.budget.max_endpoint_switches:
                    break
                target_index = next_index
                switches += 1
                continue
            try:
                permit = self._acquire(target, plan, started_at)
            except AdmissionRejectedError as exc:
                admission_rejections += 1
                last_failure = exc.disposition
                decision = self._policy.decide(exc.disposition)
                if decision.kind is DecisionKind.ABORT or admission_rejections >= max_admission_rejections:
                    break
                next_index, switches, rotations, recovered = await self._apply_decision(
                    invocation,
                    plan,
                    targets,
                    target_index,
                    switches,
                    rotations,
                    ordinal,
                    decision,
                    ServiceAcceptance.REJECTED,
                )
                if not recovered:
                    break
                target_index = next_index
                await self._backoff(
                    decision.delay_seconds or 1.0,
                    plan,
                    started_at,
                )
                continue
            ordinal += 1
            attempts_by_endpoint[target.endpoint.endpoint_id] = (
                attempts_by_endpoint.get(target.endpoint.endpoint_id, 0) + 1
            )
            attempt = self._attempt_started(
                invocation,
                plan,
                target,
                ordinal,
                started_at,
            )
            await self._journal.append(attempt)
            try:
                outcome = await self._start_once(
                    invocation,
                    plan,
                    target,
                    started_at,
                    permit,
                )
            except Exception as exc:  # noqa: BLE001 - adapter boundary
                failure = target.adapter.classify_start(exc)
                last_failure = failure.disposition
                cross_endpoint_safe = (
                    failure.acceptance is ServiceAcceptance.REJECTED
                    or invocation.semantics is ServiceExecutionSemantics.PURE
                )
                if self._unsafe_start_uncertainty(invocation, failure):
                    await self._finish_in_doubt(invocation, attempt, failure.disposition)
                    raise ServiceCallInDoubtError(
                        "service submit outcome is unknown and cannot be repeated safely",
                        service_call_id=invocation.service_call_id,
                        attempt_id=attempt.attempt_id,
                    ) from exc
                await self._finish_failed(attempt, failure.disposition)
                decision = self._policy.decide(failure.disposition)
                next_index, switches, rotations, recovered = await self._apply_decision(
                    invocation,
                    plan,
                    targets,
                    target_index,
                    switches,
                    rotations,
                    ordinal,
                    decision,
                    failure.acceptance,
                )
                if not recovered:
                    break
                target_index = next_index
                await self._backoff(decision.delay_seconds, plan, started_at)
                continue
            if isinstance(outcome, ServiceCompleted):
                return await self._finish_success(invocation, target, attempt, outcome.response)
            if isinstance(outcome, ServiceAccepted):
                await self._journal.append(
                    ServiceReceiptAcceptedRecord(
                        service_call_id=invocation.service_call_id,
                        attempt_id=attempt.attempt_id,
                        receipt=outcome.receipt,
                    )
                )
                return await self._poll_receipt(
                    invocation,
                    plan,
                    target,
                    attempt,
                    outcome.receipt,
                    poll_ordinal=0,
                    started_at=started_at,
                )
            if isinstance(outcome, ServiceFailed):
                last_failure = outcome.failure
                cross_endpoint_safe = True
                await self._finish_failed(attempt, outcome.failure)
                decision = self._policy.decide(outcome.failure)
                next_index, switches, rotations, recovered = await self._apply_decision(
                    invocation,
                    plan,
                    targets,
                    target_index,
                    switches,
                    rotations,
                    ordinal,
                    decision,
                    ServiceAcceptance.REJECTED,
                )
                if not recovered:
                    break
                target_index = next_index
                await self._backoff(decision.delay_seconds, plan, started_at)
        failure = last_failure or _unknown_failure("service attempt budget exhausted")
        await self._journal.append(
            ServiceCallFinishedRecord(
                service_call_id=invocation.service_call_id,
                state=ServiceCallState.FAILED,
                failure=failure,
            )
        )
        raise ServiceCallExhaustedError(
            "service call exhausted its failover budget",
            service_call_id=invocation.service_call_id,
            attempts=ordinal,
        )

    async def _poll_receipt(
        self,
        invocation: ServiceInvocation,
        plan: ServicePlan,
        target: _Target,
        attempt: ServiceAttemptStartedRecord,
        receipt: ServiceReceipt,
        *,
        poll_ordinal: int,
        started_at: float,
    ) -> ResolvedServiceResponse:
        current = receipt
        while True:
            self._ensure_deadline(plan, started_at, invocation.service_call_id)
            if current.poll_after_seconds:
                await self._backoff(current.poll_after_seconds, plan, started_at)
            permit = await self._wait_for_admission(
                target,
                plan,
                started_at,
            )
            try:
                async with asyncio.timeout(self._attempt_timeout(plan, started_at)):
                    outcome = await target.adapter.poll_once(
                        current,
                        target.endpoint,
                        timeout_seconds=self._attempt_timeout(plan, started_at),
                    )
            except asyncio.CancelledError:
                permit.abandon()
                raise
            except Exception as exc:  # noqa: BLE001 - adapter boundary
                failure = target.adapter.classify_poll(exc)
                permit.fail(failure.disposition)
                decision = self._policy.decide(failure.disposition)
                if decision.kind is DecisionKind.ABORT:
                    await self._finish_failed(attempt, failure.disposition)
                    await self._journal.append(
                        ServiceCallFinishedRecord(
                            service_call_id=invocation.service_call_id,
                            state=ServiceCallState.FAILED,
                            failure=failure.disposition,
                        )
                    )
                    raise ServiceCallExhaustedError(
                        "accepted service operation failed while polling",
                        service_call_id=invocation.service_call_id,
                        attempt_id=attempt.attempt_id,
                    ) from exc
                await self._backoff(
                    decision.delay_seconds or 1.0,
                    plan,
                    started_at,
                )
                continue
            permit.succeed()
            if isinstance(outcome, ServiceCompleted):
                return await self._finish_success(invocation, target, attempt, outcome.response)
            if isinstance(outcome, ServiceFailed):
                await self._finish_failed(attempt, outcome.failure)
                await self._journal.append(
                    ServiceCallFinishedRecord(
                        service_call_id=invocation.service_call_id,
                        state=ServiceCallState.FAILED,
                        failure=outcome.failure,
                    )
                )
                raise ServiceCallExhaustedError(
                    "accepted service operation reached a failed terminal state",
                    service_call_id=invocation.service_call_id,
                    attempt_id=attempt.attempt_id,
                )
            if outcome.receipt.provider_operation_id != current.provider_operation_id:
                failure = _unknown_failure("poll changed provider operation identity")
                await self._finish_in_doubt(invocation, attempt, failure)
                raise ServiceCallInDoubtError(
                    "service poll changed the durable receipt identity",
                    service_call_id=invocation.service_call_id,
                )
            poll_ordinal += 1
            current = outcome.receipt
            await self._journal.append(
                ServiceReceiptAcceptedRecord(
                    service_call_id=invocation.service_call_id,
                    attempt_id=attempt.attempt_id,
                    receipt=current,
                    poll_ordinal=poll_ordinal,
                )
            )

    async def _reconcile_open_attempt(
        self,
        invocation: ServiceInvocation,
        plan: ServicePlan,
        target: _Target,
        attempt: ServiceAttemptStartedRecord,
        started_at: float,
    ) -> ResolvedServiceResponse | None:
        permit = await self._wait_for_admission(target, plan, started_at)
        try:
            async with asyncio.timeout(self._attempt_timeout(plan, started_at)):
                outcome = await target.adapter.reconcile_once(
                    invocation,
                    target.endpoint,
                    timeout_seconds=self._attempt_timeout(plan, started_at),
                )
        except asyncio.CancelledError:
            permit.abandon()
            raise
        except Exception as exc:  # noqa: BLE001 - adapter boundary
            failure = target.adapter.classify_poll(exc)
            permit.fail(failure.disposition)
            if not self._may_repeat_after_unknown(invocation.semantics):
                await self._finish_in_doubt(invocation, attempt, failure.disposition)
                raise ServiceCallInDoubtError(
                    "service reconciliation failed for a non-repeatable submit",
                    service_call_id=invocation.service_call_id,
                ) from exc
            await self._finish_failed(attempt, failure.disposition, in_doubt=True)
            return None
        permit.succeed()
        if outcome is None:
            failure = _unknown_failure("service submit was not found during reconciliation")
            if not self._may_repeat_after_unknown(invocation.semantics):
                await self._finish_in_doubt(invocation, attempt, failure)
                raise ServiceCallInDoubtError(
                    "service submit could not be reconciled safely",
                    service_call_id=invocation.service_call_id,
                )
            await self._finish_failed(attempt, failure, in_doubt=True)
            return None
        if isinstance(outcome, ServiceCompleted):
            return await self._finish_success(invocation, target, attempt, outcome.response)
        if isinstance(outcome, ServiceAccepted):
            await self._journal.append(
                ServiceReceiptAcceptedRecord(
                    service_call_id=invocation.service_call_id,
                    attempt_id=attempt.attempt_id,
                    receipt=outcome.receipt,
                )
            )
            return await self._poll_receipt(
                invocation,
                plan,
                target,
                attempt,
                outcome.receipt,
                poll_ordinal=0,
                started_at=started_at,
            )
        await self._finish_failed(attempt, outcome.failure)
        await self._journal.append(
            ServiceCallFinishedRecord(
                service_call_id=invocation.service_call_id,
                state=ServiceCallState.FAILED,
                failure=outcome.failure,
            )
        )
        raise ServiceCallExhaustedError(
            "reconciled service operation reached a failed terminal state",
            service_call_id=invocation.service_call_id,
        )

    async def cancel(self, service_call_id: str) -> bool:
        lock = self._locks.setdefault(service_call_id, asyncio.Lock())
        try:
            async with lock:
                records = self._journal.records(service_call_id)
                if not records:
                    return False
                recovery = self._journal.recover(service_call_id)
                if recovery.terminal is not None:
                    return recovery.terminal.state is ServiceCallState.CANCELLED
                finished_ids = {item.attempt_id for item in recovery.attempt_finishes}
                attempt = next(
                    (item for item in reversed(recovery.attempt_starts) if item.attempt_id not in finished_ids),
                    None,
                )
                if attempt is None:
                    return False
                receipt_record = next(
                    (item for item in reversed(recovery.receipts) if item.attempt_id == attempt.attempt_id),
                    None,
                )
                if receipt_record is None:
                    return False
                plan = self._plan_from_record(recovery.plan)
                target = self._target_for_started(plan, attempt)
                await target.adapter.cancel_once(
                    receipt_record.receipt,
                    target.endpoint,
                    timeout_seconds=plan.budget.single_attempt_timeout_seconds,
                )
                await self._journal.append(
                    ServiceAttemptFinishedRecord(
                        service_call_id=service_call_id,
                        attempt_id=attempt.attempt_id,
                        ordinal=attempt.ordinal,
                        resume_generation=attempt.resume_generation,
                        state=AttemptState.CANCELLED,
                    )
                )
                await self._journal.append(
                    ServiceCallFinishedRecord(
                        service_call_id=service_call_id,
                        state=ServiceCallState.CANCELLED,
                    )
                )
                return True
        finally:
            if not lock.locked() and self._locks.get(service_call_id) is lock:
                self._locks.pop(service_call_id, None)

    async def aclose(self) -> None:
        close = getattr(self._resolver, "aclose", None)
        if close is None:
            return
        result = close()
        if inspect.isawaitable(result):
            await result

    def _targets(self, plan: ServicePlan) -> tuple[_Target, ...]:
        targets: list[_Target] = []
        for endpoint in plan.endpoints:
            slots = self._planner.snapshot.slots_for_endpoint(endpoint.endpoint_id)
            for slot in slots:
                adapter = self._resolver.resolve(endpoint, slot)
                if adapter is not None:
                    if adapter.endpoint_id != endpoint.endpoint_id or adapter.credential_slot_id != slot:
                        raise ServiceRouteUnavailableError(
                            "service endpoint resolver returned a mismatched binding",
                            endpoint_id=endpoint.endpoint_id,
                            credential_slot_id=slot,
                        )
                    targets.append(
                        _Target(
                            endpoint=endpoint,
                            credential_slot_id=slot,
                            adapter=adapter,
                            endpoint_fingerprint=_endpoint_fingerprint(endpoint),
                        )
                    )
        return tuple(targets)

    def _target_for_started(
        self,
        plan: ServicePlan,
        attempt: ServiceAttemptStartedRecord,
    ) -> _Target:
        for target in self._targets(plan):
            if (
                target.endpoint.endpoint_id == attempt.endpoint_id
                and target.credential_slot_id == attempt.credential_slot_id
                and target.endpoint_fingerprint == attempt.endpoint_fingerprint
            ):
                return target
        raise ServiceRouteUnavailableError(
            "durable service attempt cannot resolve its original endpoint",
            service_call_id=attempt.service_call_id,
            endpoint_id=attempt.endpoint_id,
            credential_slot_id=attempt.credential_slot_id,
        )

    async def _start_once(
        self,
        invocation: ServiceInvocation,
        plan: ServicePlan,
        target: _Target,
        started_at: float,
        permit: AdmissionPermit,
    ):
        timeout = self._attempt_timeout(plan, started_at)
        try:
            async with asyncio.timeout(timeout):
                result = await target.adapter.start_once(
                    invocation,
                    target.endpoint,
                    timeout_seconds=timeout,
                )
        except asyncio.CancelledError:
            permit.abandon()
            raise
        except Exception as exc:
            permit.fail(target.adapter.classify_start(exc).disposition)
            raise
        permit.succeed()
        return result

    def _acquire(self, target: _Target, plan: ServicePlan, started_at: float):
        result = self._admission.acquire(
            _resource_identity(target),
            remaining_seconds=max(
                plan.budget.total_deadline_seconds - (time.monotonic() - started_at),
                0.0,
            ),
        )
        if result.rejection is not None:
            raise AdmissionRejectedError(result.rejection)
        if result.permit is None:
            raise RuntimeError("service admission returned no permit")
        return result.permit

    async def _wait_for_admission(
        self,
        target: _Target,
        plan: ServicePlan,
        started_at: float,
    ) -> AdmissionPermit:
        while True:
            self._ensure_deadline(plan, started_at, plan.service_call_id)
            try:
                return self._acquire(target, plan, started_at)
            except AdmissionRejectedError as exc:
                await self._backoff(
                    1.0,
                    plan,
                    started_at,
                )

    async def _finish_success(
        self,
        invocation: ServiceInvocation,
        target: _Target,
        attempt: ServiceAttemptStartedRecord,
        response: ServiceResponse,
    ) -> ResolvedServiceResponse:
        await self._journal.append(
            ServiceAttemptFinishedRecord(
                service_call_id=invocation.service_call_id,
                attempt_id=attempt.attempt_id,
                ordinal=attempt.ordinal,
                resume_generation=attempt.resume_generation,
                state=AttemptState.SUCCEEDED,
                response=response,
            )
        )
        return await self._checkpoint_success(
            invocation,
            target,
            attempt,
            response,
        )

    async def _checkpoint_success(
        self,
        invocation: ServiceInvocation,
        target: _Target,
        attempt: ServiceAttemptStartedRecord,
        response: ServiceResponse,
    ) -> ResolvedServiceResponse:
        terminal = ServiceCallFinishedRecord(
            service_call_id=invocation.service_call_id,
            state=ServiceCallState.SUCCEEDED,
            selected_endpoint_id=target.endpoint.endpoint_id,
            successful_attempt_id=attempt.attempt_id,
            endpoint_fingerprint=target.endpoint_fingerprint,
            credential_slot_id=target.credential_slot_id,
            tenant_fingerprint=target.adapter.tenant_fingerprint,
            provider=target.endpoint.provider,
            transport=target.endpoint.transport,
            response=response,
        )
        await self._journal.append(terminal)
        return self._resolve_terminal(terminal)

    async def _finish_failed(
        self,
        attempt: ServiceAttemptStartedRecord,
        failure: FailureDisposition,
        *,
        in_doubt: bool = False,
    ) -> None:
        await self._journal.append(
            ServiceAttemptFinishedRecord(
                service_call_id=attempt.service_call_id,
                attempt_id=attempt.attempt_id,
                ordinal=attempt.ordinal,
                resume_generation=attempt.resume_generation,
                state=AttemptState.IN_DOUBT if in_doubt else AttemptState.FAILED,
                failure=failure,
            )
        )

    async def _finish_in_doubt(
        self,
        invocation: ServiceInvocation,
        attempt: ServiceAttemptStartedRecord,
        failure: FailureDisposition,
    ) -> None:
        await self._finish_failed(attempt, failure, in_doubt=True)
        await self._journal.append(
            ServiceCallFinishedRecord(
                service_call_id=invocation.service_call_id,
                state=ServiceCallState.IN_DOUBT,
                failure=failure,
            )
        )

    async def _apply_decision(
        self,
        invocation: ServiceInvocation,
        plan: ServicePlan,
        targets: tuple[_Target, ...],
        current_index: int,
        switches: int,
        rotations: int,
        ordinal: int,
        decision,
        acceptance: ServiceAcceptance,
    ) -> tuple[int, int, int, bool]:
        target_index = current_index
        recovered = False
        if decision.kind is DecisionKind.RETRY_SAME_ENDPOINT:
            recovered = True
        elif decision.kind is DecisionKind.ROTATE_CREDENTIAL:
            candidate = self._next_credential_index(targets, current_index)
            if candidate is not None and rotations < plan.budget.max_credential_rotations:
                target_index = candidate
                rotations += 1
                recovered = True
        elif decision.kind in {
            DecisionKind.SWITCH_ENDPOINT,
            DecisionKind.TRANSFORM_REQUEST,
        }:
            candidate = self._next_endpoint_index(targets, current_index)
            may_switch = (
                acceptance is ServiceAcceptance.REJECTED or invocation.semantics is ServiceExecutionSemantics.PURE
            )
            if may_switch and candidate is not None and switches < plan.budget.max_endpoint_switches:
                target_index = candidate
                switches += 1
                recovered = True
        if recovered:
            await self._journal.append(
                ServiceDecisionRecord(
                    service_call_id=invocation.service_call_id,
                    after_attempt_ordinal=ordinal,
                    decision=decision,
                    from_endpoint_id=targets[current_index].endpoint.endpoint_id,
                    to_endpoint_id=targets[target_index].endpoint.endpoint_id,
                )
            )
        return target_index, switches, rotations, recovered

    @staticmethod
    def _next_credential_index(targets: tuple[_Target, ...], current: int) -> int | None:
        endpoint_id = targets[current].endpoint.endpoint_id
        return next(
            (index for index in range(current + 1, len(targets)) if targets[index].endpoint.endpoint_id == endpoint_id),
            None,
        )

    @staticmethod
    def _next_endpoint_index(targets: tuple[_Target, ...], current: int) -> int | None:
        endpoint_id = targets[current].endpoint.endpoint_id
        return next(
            (index for index in range(current + 1, len(targets)) if targets[index].endpoint.endpoint_id != endpoint_id),
            None,
        )

    @staticmethod
    def _unsafe_start_uncertainty(
        invocation: ServiceInvocation,
        failure: ServiceEndpointFailure,
    ) -> bool:
        return failure.acceptance is ServiceAcceptance.UNKNOWN and not RuntimeServiceGateway._may_repeat_after_unknown(
            invocation.semantics
        )

    @staticmethod
    def _may_repeat_after_unknown(semantics: ServiceExecutionSemantics) -> bool:
        return semantics in {
            ServiceExecutionSemantics.PURE,
            ServiceExecutionSemantics.IDEMPOTENT,
        }

    @staticmethod
    async def _backoff(
        requested: float,
        plan: ServicePlan,
        started_at: float,
    ) -> None:
        delay = min(max(requested, 0.0), plan.budget.max_backoff_seconds)
        remaining = plan.budget.total_deadline_seconds - (time.monotonic() - started_at)
        if delay >= remaining:
            raise ServiceCallDeadlineExceededError(
                "service call deadline would expire during backoff",
                service_call_id=plan.service_call_id,
            )
        if delay:
            await asyncio.sleep(delay)

    @staticmethod
    def _attempt_timeout(plan: ServicePlan, started_at: float) -> float:
        remaining = plan.budget.total_deadline_seconds - (time.monotonic() - started_at)
        return max(min(plan.budget.single_attempt_timeout_seconds, remaining), 0.001)

    @staticmethod
    def _ensure_deadline(
        plan: ServicePlan,
        started_at: float,
        service_call_id: str,
    ) -> None:
        if time.monotonic() - started_at >= plan.budget.total_deadline_seconds:
            raise ServiceCallDeadlineExceededError(
                "service call deadline exceeded",
                service_call_id=service_call_id,
            )

    @staticmethod
    def _planned(
        invocation: ServiceInvocation,
        plan: ServicePlan,
    ) -> ServiceCallPlannedRecord:
        return ServiceCallPlannedRecord(
            service_call_id=invocation.service_call_id,
            plan_id=plan.plan_id,
            route_id=invocation.route_id,
            capability=invocation.capability,
            config_revision=plan.config_revision,
            endpoint_ids=tuple(item.endpoint_id for item in plan.endpoints),
            budget=plan.budget,
            policy_id=plan.policy_id,
            semantics=invocation.semantics,
            idempotency_key=invocation.idempotency_key,
            root_started_at=plan.created_at,
            occurred_at=plan.created_at,
        )

    @staticmethod
    def _attempt_started(
        invocation: ServiceInvocation,
        plan: ServicePlan,
        target: _Target,
        ordinal: int,
        started_at: float,
    ) -> ServiceAttemptStartedRecord:
        attempt_id = hashlib.sha256(
            (
                f"{invocation.service_call_id}\0{ordinal}\0"
                f"{target.endpoint.endpoint_id}\0{target.credential_slot_id}"
            ).encode("utf-8")
        ).hexdigest()[:32]
        return ServiceAttemptStartedRecord(
            service_call_id=invocation.service_call_id,
            attempt_id=attempt_id,
            ordinal=ordinal,
            endpoint_id=target.endpoint.endpoint_id,
            endpoint_fingerprint=target.endpoint_fingerprint,
            credential_slot_id=target.credential_slot_id,
            timeout_seconds=RuntimeServiceGateway._attempt_timeout(plan, started_at),
        )

    def _plan_for_recovery(
        self,
        invocation: ServiceInvocation,
        recovery: ServiceCallRecovery,
    ) -> ServicePlan:
        current = self._planner.plan(invocation)
        endpoint_by_id = {item.endpoint_id: item for item in current.endpoints}
        endpoints = tuple(
            endpoint_by_id[endpoint_id] for endpoint_id in recovery.plan.endpoint_ids if endpoint_id in endpoint_by_id
        )
        if len(endpoints) != len(recovery.plan.endpoint_ids):
            raise ServiceRouteUnavailableError(
                "service recovery configuration no longer contains its endpoints",
                service_call_id=invocation.service_call_id,
                planned_revision=recovery.plan.config_revision,
            )
        return ServicePlan(
            plan_id=recovery.plan.plan_id,
            service_call_id=invocation.service_call_id,
            config_revision=recovery.plan.config_revision,
            policy_id=recovery.plan.policy_id,
            endpoints=endpoints,
            budget=recovery.plan.budget,
            created_at=recovery.plan.root_started_at,
        )

    def _plan_from_record(self, record: ServiceCallPlannedRecord) -> ServicePlan:
        endpoints = tuple(
            endpoint
            for endpoint_id in record.endpoint_ids
            if (endpoint := self._planner.snapshot.endpoint(endpoint_id)) is not None
        )
        if len(endpoints) != len(record.endpoint_ids):
            raise ServiceRouteUnavailableError(
                "service recovery configuration no longer contains its endpoints",
                service_call_id=record.service_call_id,
            )
        return ServicePlan(
            plan_id=record.plan_id,
            service_call_id=record.service_call_id,
            config_revision=record.config_revision,
            policy_id=record.policy_id,
            endpoints=endpoints,
            budget=record.budget,
            created_at=record.root_started_at,
        )

    @staticmethod
    def _validate_invocation(
        invocation: ServiceInvocation,
        recovery: ServiceCallRecovery,
    ) -> None:
        plan = recovery.plans[0]
        if (
            invocation.route_id != plan.route_id
            or invocation.capability != plan.capability
            or invocation.semantics is not plan.semantics
            or invocation.idempotency_key != plan.idempotency_key
        ):
            raise ValueError("service_call_id was reused with a different invocation")

    @staticmethod
    def _resolve_terminal(
        terminal: ServiceCallFinishedRecord,
    ) -> ResolvedServiceResponse:
        if terminal.state is ServiceCallState.IN_DOUBT:
            raise ServiceCallInDoubtError(
                "service call is durably in doubt",
                service_call_id=terminal.service_call_id,
            )
        if terminal.state is not ServiceCallState.SUCCEEDED:
            raise ServiceCallExhaustedError(
                f"service call is already {terminal.state.value}",
                service_call_id=terminal.service_call_id,
            )
        assert terminal.response is not None
        assert terminal.selected_endpoint_id is not None
        assert terminal.successful_attempt_id is not None
        assert terminal.endpoint_fingerprint is not None
        assert terminal.credential_slot_id is not None
        assert terminal.tenant_fingerprint is not None
        return ResolvedServiceResponse(
            response=terminal.response,
            endpoint_id=terminal.selected_endpoint_id,
            endpoint_fingerprint=terminal.endpoint_fingerprint,
            credential_slot_id=terminal.credential_slot_id,
            tenant_fingerprint=terminal.tenant_fingerprint,
            provider=terminal.provider or "unknown",
            transport=terminal.transport or "unknown",
            service_call_id=terminal.service_call_id,
            successful_attempt_id=terminal.successful_attempt_id,
        )


def _endpoint_fingerprint(endpoint: ServiceEndpointDescriptor) -> str:
    return hashlib.sha256(
        (
            f"{endpoint.transport}\0{endpoint.provider}\0"
            f"{endpoint.base_url_identity}\0{endpoint.lifecycle_revision}"
        ).encode("utf-8")
    ).hexdigest()[:32]


def _resource_identity(target: _Target) -> ResourceIdentity:
    return ResourceIdentity(
        endpoint_id=target.endpoint.endpoint_id,
        transport=target.endpoint.transport,
        endpoint_fingerprint=target.endpoint_fingerprint,
        model_or_deployment=target.endpoint.capability,
        tenant_fingerprint=target.adapter.tenant_fingerprint,
        credential_slot_id=target.credential_slot_id,
    )


def _unknown_failure(detail: str) -> FailureDisposition:
    return FailureDisposition(
        reason=FailureReason.UNKNOWN,
        domain=FailureDomain.INTERNAL,
        retryability=Retryability.NEVER,
        health_verdict=HealthVerdict.NEUTRAL,
        safe_message="internal service failure",
    )


def _monotonic_start(root_started_at: datetime) -> float:
    elapsed = max((datetime.now(timezone.utc) - root_started_at).total_seconds(), 0.0)
    return time.monotonic() - elapsed


__all__ = ["RuntimeServiceGateway"]
