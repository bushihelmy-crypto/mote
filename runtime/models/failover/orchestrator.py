"""The single owner of request-level LLM retry and recovery attempts."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, TypeVar

from mote.contracts.model.failover import (
    AttemptBudget,
    DecisionKind,
    FailoverDecision,
    FailureDisposition,
    RequestTransform,
)
from mote.runtime.models.clients.retry import retry_delay
from mote.runtime.resilience.admission import AdmissionPermit, AdmissionRejectedError, AdmissionResult
from mote.runtime.resilience.failover.classification import classify_failure
from mote.runtime.resilience.failover.policy import DefaultFailoverPolicy, FailoverPolicy
from mote.runtime.telemetry.logging import log_class

ResultT = TypeVar("ResultT")
RequestT = TypeVar("RequestT")
AttemptExecutor = Callable[[Any, RequestT], Awaitable[ResultT]]
CredentialSelector = Callable[[Any], Any | None]
EndpointSelector = Callable[[], Any | None]
RequestTransformer = Callable[
    [Any, RequestT, RequestTransform, FailureDisposition, Exception],
    Awaitable[RequestT | None],
]
FailureClassifier = Callable[[Exception], FailureDisposition]
ProviderKey = Callable[[Any], object]
AdmissionAcquirer = Callable[[Any, float], AdmissionResult]
DecisionObserver = Callable[
    [int, FailoverDecision, FailoverDecision, Any, Any],
    Awaitable[None],
]

DEFAULT_MAX_WIRE_ATTEMPTS = 6


def _budget_for_wire_limit(max_wire_attempts: int) -> AttemptBudget:
    max_changes = max_wire_attempts - 1
    return AttemptBudget(
        max_wire_attempts=max_wire_attempts,
        max_attempts_per_endpoint=max_wire_attempts,
        max_endpoint_switches=max_changes,
        max_credential_rotations=max_changes,
        max_request_transforms=max_changes,
    )


@dataclass
class _ModelCallState:
    """Mutable state owned by exactly one logical model call."""

    provider: Any
    request: Any
    started_at: float = field(default_factory=time.monotonic)
    attempts_by_provider: dict[object, int] = field(default_factory=dict)
    endpoint_switches: int = 0
    credential_rotations: int = 0
    request_transforms: int = 0

    def record_attempt(self, key: object) -> None:
        self.attempts_by_provider[key] = self.attempts_by_provider.get(key, 0) + 1

    def attempts_on_provider(self, key: object) -> int:
        return self.attempts_by_provider.get(key, 0)


@dataclass(frozen=True)
class AttemptResumeSeed:
    """Durable budget consumption inherited by one resume generation."""

    wire_attempts: int = 0
    attempts_by_provider: tuple[tuple[object, int], ...] = ()
    endpoint_switches: int = 0
    credential_rotations: int = 0
    request_transforms: int = 0
    elapsed_seconds: float = 0.0


@log_class(level="DEBUG")
class AttemptOrchestrator:
    """Execute one immutable budget and one mutable ledger per logical call."""

    def __init__(
        self,
        *,
        budget: AttemptBudget | None = None,
        policy: FailoverPolicy | None = None,
        classifier: FailureClassifier | None = None,
        provider_key: ProviderKey | None = None,
        max_wire_attempts: int | None = None,
    ) -> None:
        if budget is not None and max_wire_attempts is not None:
            raise ValueError("pass budget or max_wire_attempts, not both")
        if max_wire_attempts is not None:
            if max_wire_attempts <= 0:
                raise ValueError("max_wire_attempts must be positive")
            budget = _budget_for_wire_limit(max_wire_attempts)
        self._budget = budget or AttemptBudget()
        self._policy = policy or DefaultFailoverPolicy()
        self._classifier = classifier or classify_failure
        self._provider_key = provider_key or id

    async def run(
        self,
        *,
        execute_once: AttemptExecutor[RequestT, ResultT],
        primary: Any,
        request: RequestT,
        next_credential: CredentialSelector | None = None,
        endpoint_selector_factory: Callable[[], EndpointSelector] | None = None,
        request_transformer: RequestTransformer[RequestT] | None = None,
        admit: AdmissionAcquirer | None = None,
        resume_seed: AttemptResumeSeed | None = None,
        observe_decision: DecisionObserver | None = None,
    ) -> ResultT:
        seed = resume_seed or AttemptResumeSeed()
        state = _ModelCallState(
            provider=primary,
            request=request,
            started_at=time.monotonic() - seed.elapsed_seconds,
            attempts_by_provider=dict(seed.attempts_by_provider),
            endpoint_switches=seed.endpoint_switches,
            credential_rotations=seed.credential_rotations,
            request_transforms=seed.request_transforms,
        )
        endpoint_selector = endpoint_selector_factory() if endpoint_selector_factory is not None else None

        wire_attempt = seed.wire_attempts
        admission_rejections = 0
        max_admission_rejections = self._budget.max_credential_rotations + self._budget.max_endpoint_switches + 1

        while wire_attempt < self._budget.max_wire_attempts:
            permit: AdmissionPermit | None = None
            if admit is not None:
                admission = admit(state.provider, self._remaining_seconds(state))
                if admission.rejection is not None:
                    admission_rejections += 1
                    rejection = AdmissionRejectedError(admission.rejection)
                    disposition = admission.rejection.disposition
                    decision = self._policy.decide(disposition)
                    ordinal = max(wire_attempt, 1)
                    if (
                        decision.kind is DecisionKind.ABORT
                        or admission_rejections >= max_admission_rejections
                        or self._deadline_exhausted(state)
                    ):
                        raise rejection
                    before = state.provider
                    recovered, applied_decision = await self._apply_decision(
                        decision=decision,
                        attempt=ordinal,
                        exc=rejection,
                        disposition=disposition,
                        state=state,
                        next_credential=next_credential,
                        endpoint_selector=endpoint_selector,
                        request_transformer=request_transformer,
                    )
                    if not recovered:
                        raise rejection
                    if observe_decision is not None:
                        await observe_decision(
                            ordinal,
                            decision,
                            applied_decision,
                            before,
                            state.provider,
                        )
                    continue
                permit = admission.permit

            wire_attempt += 1
            attempt = wire_attempt
            state.record_attempt(self._provider_key(state.provider))
            try:
                result = await execute_once(state.provider, state.request)
            except asyncio.CancelledError:
                if permit is not None:
                    permit.abandon()
                raise
            except Exception as exc:  # noqa: BLE001 — classify at the model boundary
                disposition = self._classifier(exc)
                if permit is not None:
                    permit.fail(disposition)
                decision = self._policy.decide(disposition)
                if (
                    decision.kind is DecisionKind.ABORT
                    or attempt >= self._budget.max_wire_attempts
                    or self._deadline_exhausted(state)
                ):
                    raise
                before = state.provider
                recovered, applied_decision = await self._apply_decision(
                    decision=decision,
                    attempt=attempt,
                    exc=exc,
                    disposition=disposition,
                    state=state,
                    next_credential=next_credential,
                    endpoint_selector=endpoint_selector,
                    request_transformer=request_transformer,
                )
                if not recovered:
                    raise
                if observe_decision is not None:
                    await observe_decision(
                        attempt,
                        decision,
                        applied_decision,
                        before,
                        state.provider,
                    )
            else:
                if permit is not None:
                    permit.succeed()
                return result

        raise AssertionError("unreachable model attempt loop exit")

    async def _apply_decision(
        self,
        *,
        decision: FailoverDecision,
        attempt: int,
        exc: Exception,
        disposition: FailureDisposition,
        state: _ModelCallState,
        next_credential: CredentialSelector | None,
        endpoint_selector: EndpointSelector | None,
        request_transformer: RequestTransformer[Any] | None,
    ) -> tuple[bool, FailoverDecision]:
        if decision.kind is DecisionKind.RETRY_SAME_ENDPOINT:
            if self._endpoint_attempt_limit_reached(state):
                return self._switch_endpoint(state, endpoint_selector, decision.reason)
            if not await self._retry(
                state,
                attempt,
                exc,
                requested_delay=decision.delay_seconds,
            ):
                return False, decision
            return True, decision
        if decision.kind is DecisionKind.ROTATE_CREDENTIAL:
            if self._endpoint_attempt_limit_reached(state):
                return self._switch_endpoint(state, endpoint_selector, decision.reason)
            if state.credential_rotations < self._budget.max_credential_rotations:
                state.credential_rotations += 1
                provider = next_credential(state.provider) if next_credential is not None else None
                if provider is not None:
                    state.provider = provider
                    return True, decision
            return self._switch_endpoint(state, endpoint_selector, decision.reason)
        if decision.kind is DecisionKind.SWITCH_ENDPOINT:
            return self._switch_endpoint(state, endpoint_selector, decision.reason)
        if decision.kind is DecisionKind.TRANSFORM_REQUEST:
            if self._endpoint_attempt_limit_reached(state):
                return self._switch_endpoint(state, endpoint_selector, decision.reason)
            if state.request_transforms >= self._budget.max_request_transforms:
                return self._switch_endpoint(state, endpoint_selector, decision.reason)
            state.request_transforms += 1
            transform = decision.transform
            if transform is None or request_transformer is None:
                return self._switch_endpoint(state, endpoint_selector, decision.reason)
            transformed = await request_transformer(
                state.provider,
                state.request,
                transform,
                disposition,
                exc,
            )
            if transformed is not None:
                state.request = transformed
                return True, decision
            return self._switch_endpoint(state, endpoint_selector, decision.reason)
        return False, decision

    def _endpoint_attempt_limit_reached(self, state: _ModelCallState) -> bool:
        return state.attempts_on_provider(self._provider_key(state.provider)) >= self._budget.max_attempts_per_endpoint

    def _switch_endpoint(
        self,
        state: _ModelCallState,
        endpoint_selector: EndpointSelector | None,
        reason: str,
    ) -> tuple[bool, FailoverDecision]:
        switched = FailoverDecision(kind=DecisionKind.SWITCH_ENDPOINT, reason=reason)
        if endpoint_selector is None or state.endpoint_switches >= self._budget.max_endpoint_switches:
            return False, switched
        provider = endpoint_selector()
        if provider is None:
            return False, switched
        state.endpoint_switches += 1
        state.provider = provider
        return True, switched

    async def _retry(
        self,
        state: _ModelCallState,
        attempt: int,
        exc: Exception,
        *,
        requested_delay: float = 0.0,
    ) -> bool:
        delay = min(
            requested_delay or retry_delay(exc, attempt),
            self._budget.max_backoff_seconds,
        )
        remaining = self._remaining_seconds(state)
        if delay >= remaining:
            return False
        await asyncio.sleep(delay)
        return True

    def _remaining_seconds(self, state: _ModelCallState) -> float:
        elapsed = time.monotonic() - state.started_at
        return max(self._budget.total_deadline_seconds - elapsed, 0.0)

    def _deadline_exhausted(self, state: _ModelCallState) -> bool:
        return self._remaining_seconds(state) <= 0.0
