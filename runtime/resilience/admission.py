"""Provider-neutral resource admission with exactly-once permit settlement."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Protocol

from mote.contracts.config.model.breaker import BreakerConfig
from mote.contracts.model.failover import (
    AdmissionGate,
    AdmissionVerdict,
    CredentialVerdict,
    FailureDisposition,
    FailureDomain,
    FailureReason,
    HealthVerdict,
    OperatorState,
    OperatorStatus,
    OperatorTransition,
    QuotaObservation,
    ResourceIdentity,
    Retryability,
)
from mote.contracts.model.invocation import ModelQuotaObservation
from mote.contracts.ports.model.operator import ModelOperatorAuditStore
from mote.runtime.resilience import BreakerState
from mote.runtime.resilience.failover.availability import AvailabilityBreaker, AvailabilityPermit
from mote.runtime.resilience.failover.operator import (
    OperatorAuditIntegrityError,
    OperatorAuditRequiredError,
    OperatorDrainIncompleteError,
    OperatorRevisionConflict,
)
from mote.runtime.telemetry.logging import log_class


class AdmissionPermit(Protocol):
    def succeed(self) -> None:
        ...

    def fail(self, disposition: FailureDisposition) -> None:
        ...

    def abandon(self) -> None:
        ...


@dataclass(frozen=True)
class AdmissionResult:
    permit: AdmissionPermit | None = None
    rejection: AdmissionVerdict | None = None

    def __post_init__(self) -> None:
        if (self.permit is None) == (self.rejection is None):
            raise ValueError("admission result requires exactly one outcome")


class AdmissionRejectedError(Exception):
    def __init__(self, verdict: AdmissionVerdict) -> None:
        self.verdict = verdict
        self.disposition = verdict.disposition
        super().__init__(f"{verdict.gate.value}:{verdict.reason}")


@dataclass(frozen=True)
class _AvailabilityKey:
    endpoint_fingerprint: str
    model_or_deployment: str


@dataclass(frozen=True)
class _CredentialKey:
    tenant_fingerprint: str
    credential_slot_id: str


@dataclass(frozen=True)
class _QuotaKey:
    endpoint_fingerprint: str
    tenant_fingerprint: str


@dataclass
class _QuotaState:
    remaining_requests: int | None = None
    reset_requests_at: float | None = None
    remaining_tokens: int | None = None
    reset_tokens_at: float | None = None
    request_reservations: int = 0


class _ResourceAdmissionPermit:
    def __init__(
        self,
        controller: "ResourceAdmissionController",
        availability_key: _AvailabilityKey,
        credential_key: _CredentialKey,
        quota_key: _QuotaKey,
        availability_permit: AvailabilityPermit,
        quota_reserved: bool,
    ) -> None:
        self._controller = controller
        self._availability_key = availability_key
        self._credential_key = credential_key
        self._quota_key = quota_key
        self._availability_permit = availability_permit
        self._quota_reserved = quota_reserved
        self._settled = False

    def succeed(self) -> None:
        self._settle(None, abandoned=False)

    def fail(self, disposition: FailureDisposition) -> None:
        self._settle(disposition, abandoned=False)

    def abandon(self) -> None:
        self._settle(None, abandoned=True)

    def _settle(
        self,
        disposition: FailureDisposition | None,
        *,
        abandoned: bool,
    ) -> None:
        if self._settled:
            raise RuntimeError("resource admission permit already settled")
        self._settled = True
        self._controller._settle(
            self._availability_key,
            self._credential_key,
            self._quota_key,
            self._availability_permit,
            self._quota_reserved,
            disposition,
            abandoned=abandoned,
        )


@log_class(level="DEBUG", exclude={"acquire", "operator_state", "observe_quota"})
class ResourceAdmissionController:
    """Share resource facts while keeping every logical-call ledger independent."""

    def __init__(
        self,
        *,
        breaker_config: BreakerConfig | None = None,
        max_in_flight_per_endpoint: int = 8,
        credential_quarantine_seconds: float = 300.0,
        default_quota_cooldown_seconds: float = 1.0,
        operator_audit: ModelOperatorAuditStore | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if max_in_flight_per_endpoint <= 0:
            raise ValueError("max_in_flight_per_endpoint must be positive")
        self._breaker_config = breaker_config or BreakerConfig()
        self._max_in_flight = max_in_flight_per_endpoint
        self._credential_quarantine_seconds = credential_quarantine_seconds
        self._default_quota_cooldown_seconds = default_quota_cooldown_seconds
        self._clock = clock or time.monotonic
        self._operator_audit = operator_audit
        self._operator_states: dict[_AvailabilityKey, OperatorState] = {}
        self._operator_revisions: dict[_AvailabilityKey, int] = {}
        self._drain_events: dict[_AvailabilityKey, asyncio.Event] = {}
        self._breakers: dict[_AvailabilityKey, AvailabilityBreaker] = {}
        self._credential_quarantine: dict[_CredentialKey, float] = {}
        self._quota_cooldown: dict[_QuotaKey, float] = {}
        self._quota_states: dict[_QuotaKey, _QuotaState] = {}
        self._in_flight: dict[_AvailabilityKey, int] = {}
        self._restore_operator_audit()

    def transition_operator_state(
        self,
        resource: ResourceIdentity,
        state: OperatorState,
        *,
        expected_revision: int,
        config_revision: str,
        actor: str,
        reason: str,
        force: bool = False,
    ) -> OperatorTransition:
        audit = self._operator_audit
        if audit is None:
            raise OperatorAuditRequiredError("operator controls require a durable audit store")
        key = self._availability_key(resource)
        previous = self._operator_states.get(key, OperatorState.ENABLED)
        revision = self._operator_revisions.get(key, 0)
        if expected_revision != revision:
            raise OperatorRevisionConflict(f"operator control expected revision {expected_revision}, actual {revision}")
        if state is previous:
            raise ValueError("operator transition must change state")
        if previous is OperatorState.DISABLED and state is OperatorState.DRAINING:
            raise ValueError("disabled endpoint must be enabled before draining")
        in_flight = self._in_flight.get(key, 0)
        if state is OperatorState.DISABLED and in_flight > 0 and not force:
            raise OperatorDrainIncompleteError(f"endpoint still has {in_flight} in-flight model calls")
        transition = OperatorTransition(
            resource=resource,
            previous_state=previous,
            state=state,
            control_revision=revision + 1,
            config_revision=config_revision,
            actor=actor,
            reason=reason,
            force=force,
            in_flight=in_flight,
            occurred_at=datetime.now(timezone.utc),
        )
        audit.append(transition)
        self._operator_states[key] = state
        self._operator_revisions[key] = transition.control_revision
        self._update_drain_event(key)
        return transition

    def operator_state(self, resource: ResourceIdentity) -> OperatorState:
        return self._operator_states.get(
            self._availability_key(resource),
            OperatorState.ENABLED,
        )

    def operator_status(self, resource: ResourceIdentity) -> OperatorStatus:
        key = self._availability_key(resource)
        state = self._operator_states.get(key, OperatorState.ENABLED)
        in_flight = self._in_flight.get(key, 0)
        return OperatorStatus(
            resource=resource,
            state=state,
            control_revision=self._operator_revisions.get(key, 0),
            in_flight=in_flight,
            drained=state is not OperatorState.ENABLED and in_flight == 0,
        )

    async def wait_drained(
        self,
        resource: ResourceIdentity,
        *,
        timeout_seconds: float | None = None,
    ) -> OperatorStatus:
        key = self._availability_key(resource)
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self._operator_states.get(key, OperatorState.ENABLED) is OperatorState.ENABLED:
            raise ValueError("endpoint must be draining or disabled before waiting")
        event = self._drain_events.setdefault(key, asyncio.Event())
        self._update_drain_event(key)
        if timeout_seconds is None:
            await event.wait()
        else:
            async with asyncio.timeout(timeout_seconds):
                await event.wait()
        return self.operator_status(resource)

    def observe_quota(
        self,
        resource: ResourceIdentity,
        observation: ModelQuotaObservation,
    ) -> None:
        """Merge one successful wire response into its shared quota domain."""

        now = self._clock()
        quota_key = self._quota_key(resource)
        state = self._quota_states.setdefault(quota_key, _QuotaState())
        if observation.remaining_requests is not None:
            state.remaining_requests = observation.remaining_requests
        if observation.reset_requests_after_seconds is not None:
            state.reset_requests_at = now + observation.reset_requests_after_seconds
        elif observation.remaining_requests == 0:
            state.reset_requests_at = now + self._default_quota_cooldown_seconds
        if observation.remaining_tokens is not None:
            state.remaining_tokens = observation.remaining_tokens
        if observation.reset_tokens_after_seconds is not None:
            state.reset_tokens_at = now + observation.reset_tokens_after_seconds
        elif observation.remaining_tokens == 0:
            state.reset_tokens_at = now + self._default_quota_cooldown_seconds
        if observation.retry_after_seconds is not None:
            self._quota_cooldown[quota_key] = now + observation.retry_after_seconds
        elif (observation.remaining_requests is not None and observation.remaining_requests > 0) or (
            observation.remaining_tokens is not None and observation.remaining_tokens > 0
        ):
            self._quota_cooldown.pop(quota_key, None)

    def acquire(
        self,
        resource: ResourceIdentity,
        *,
        remaining_seconds: float,
    ) -> AdmissionResult:
        now = self._clock()
        availability_key = self._availability_key(resource)
        credential_key = self._credential_key(resource)
        quota_key = self._quota_key(resource)

        state = self._operator_states.get(
            availability_key,
            OperatorState.ENABLED,
        )
        if state is not OperatorState.ENABLED:
            return self._reject(
                AdmissionGate.OPERATOR,
                state.value,
                resource,
                FailureReason.MODEL_UNAVAILABLE,
                FailureDomain.PROVIDER,
                Retryability.NEW_ATTEMPT,
            )
        if remaining_seconds <= 0:
            return self._reject(
                AdmissionGate.DEADLINE,
                "logical deadline exhausted",
                resource,
                FailureReason.TIMEOUT,
                FailureDomain.TRANSPORT,
                Retryability.NEVER,
            )

        credential_until = self._credential_quarantine.get(credential_key, 0.0)
        if credential_until > now:
            return self._reject(
                AdmissionGate.CREDENTIAL,
                "credential quarantined",
                resource,
                FailureReason.AUTH_REJECTED,
                FailureDomain.CREDENTIAL,
                Retryability.AFTER_HINT,
                credential_verdict=CredentialVerdict.QUARANTINE,
            )

        quota_until = self._quota_cooldown.get(quota_key, 0.0)
        if quota_until > now:
            return self._reject(
                AdmissionGate.QUOTA,
                "quota cooldown active",
                resource,
                FailureReason.RATE_LIMITED,
                FailureDomain.QUOTA,
                Retryability.AFTER_HINT,
                quota_observation=QuotaObservation.RETRY_AFTER,
            )

        quota_state = self._quota_states.get(quota_key)
        if quota_state is not None:
            self._refresh_quota_state(quota_state, now)
            request_remaining = quota_state.remaining_requests
            if request_remaining is not None and request_remaining - quota_state.request_reservations <= 0:
                return self._reject_quota_exhausted(
                    resource,
                    "request quota exhausted",
                    quota_state.reset_requests_at,
                    now,
                )
            if quota_state.remaining_tokens == 0:
                return self._reject_quota_exhausted(
                    resource,
                    "token quota exhausted",
                    quota_state.reset_tokens_at,
                    now,
                )

        breaker = self._breakers.get(availability_key)
        if breaker is None:
            breaker = AvailabilityBreaker(
                self._breaker_config,
                clock=self._clock,
            )
            self._breakers[availability_key] = breaker
        availability_permit = breaker.acquire(
            attempt_deadline=now + remaining_seconds,
        )
        if availability_permit is None:
            reason = (
                "availability recovery probes saturated"
                if breaker.state is BreakerState.HALF_OPEN
                else "availability breaker open"
            )
            return self._reject(
                AdmissionGate.AVAILABILITY,
                reason,
                resource,
                FailureReason.MODEL_UNAVAILABLE,
                FailureDomain.PROVIDER,
                Retryability.NEW_ATTEMPT,
                health_verdict=HealthVerdict.OPEN_BREAKER,
            )

        in_flight = self._in_flight.get(availability_key, 0)
        if in_flight >= self._max_in_flight:
            availability_permit.abandon()
            return self._reject(
                AdmissionGate.BULKHEAD,
                "endpoint concurrency limit reached",
                resource,
                FailureReason.OVERLOADED,
                FailureDomain.PROVIDER,
                Retryability.NEW_ATTEMPT,
            )
        self._in_flight[availability_key] = in_flight + 1
        quota_reserved = self._reserve_quota(quota_key)
        return AdmissionResult(
            permit=_ResourceAdmissionPermit(
                self,
                availability_key,
                credential_key,
                quota_key,
                availability_permit,
                quota_reserved,
            )
        )

    def _settle(
        self,
        availability_key: _AvailabilityKey,
        credential_key: _CredentialKey,
        quota_key: _QuotaKey,
        availability_permit: AvailabilityPermit,
        quota_reserved: bool,
        disposition: FailureDisposition | None,
        *,
        abandoned: bool,
    ) -> None:
        in_flight = self._in_flight.get(availability_key, 0)
        if in_flight <= 1:
            self._in_flight.pop(availability_key, None)
        else:
            self._in_flight[availability_key] = in_flight - 1
        self._update_drain_event(availability_key)
        if quota_reserved:
            quota_state = self._quota_states.get(quota_key)
            if quota_state is not None:
                quota_state.request_reservations = max(
                    quota_state.request_reservations - 1,
                    0,
                )

        if abandoned:
            availability_permit.abandon()
            return
        if disposition is None:
            availability_permit.succeed()
            self._credential_quarantine.pop(credential_key, None)
            return

        verdict = disposition.health_verdict
        now = self._clock()
        if verdict in {HealthVerdict.DEGRADE, HealthVerdict.OPEN_BREAKER}:
            availability_permit.fail()
        else:
            availability_permit.abandon()
        if disposition.credential_verdict in {
            CredentialVerdict.QUARANTINE,
            CredentialVerdict.REVOKE,
        }:
            self._credential_quarantine[credential_key] = now + self._credential_quarantine_seconds
        elif disposition.quota_observation in {
            QuotaObservation.RETRY_AFTER,
            QuotaObservation.EXHAUSTED,
        }:
            self._quota_cooldown[quota_key] = now + self._default_quota_cooldown_seconds

    def _reserve_quota(self, quota_key: _QuotaKey) -> bool:
        state = self._quota_states.get(quota_key)
        if state is None or state.remaining_requests is None:
            return False
        state.request_reservations += 1
        return True

    @staticmethod
    def _refresh_quota_state(state: _QuotaState, now: float) -> None:
        if state.reset_requests_at is not None and now >= state.reset_requests_at:
            state.remaining_requests = None
            state.reset_requests_at = None
        if state.reset_tokens_at is not None and now >= state.reset_tokens_at:
            state.remaining_tokens = None
            state.reset_tokens_at = None

    def _reject_quota_exhausted(
        self,
        resource: ResourceIdentity,
        reason: str,
        reset_at: float | None,
        now: float,
    ) -> AdmissionResult:
        return self._reject(
            AdmissionGate.QUOTA,
            reason,
            resource,
            FailureReason.RATE_LIMITED,
            FailureDomain.QUOTA,
            Retryability.AFTER_HINT,
            quota_observation=QuotaObservation.RETRY_AFTER,
        )

    def _restore_operator_audit(self) -> None:
        audit = self._operator_audit
        if audit is None:
            return
        for transition in audit.records():
            key = self._availability_key(transition.resource)
            previous = self._operator_states.get(key, OperatorState.ENABLED)
            revision = self._operator_revisions.get(key, 0)
            if transition.previous_state is not previous or transition.control_revision != revision + 1:
                raise OperatorAuditIntegrityError("operator audit transition sequence is inconsistent")
            self._operator_states[key] = transition.state
            self._operator_revisions[key] = transition.control_revision

    def _update_drain_event(self, key: _AvailabilityKey) -> None:
        event = self._drain_events.get(key)
        if event is None:
            return
        state = self._operator_states.get(key, OperatorState.ENABLED)
        if state is not OperatorState.ENABLED and self._in_flight.get(key, 0) == 0:
            event.set()
        else:
            event.clear()

    @staticmethod
    def _availability_key(resource: ResourceIdentity) -> _AvailabilityKey:
        return _AvailabilityKey(
            resource.endpoint_fingerprint,
            resource.model_or_deployment,
        )

    @staticmethod
    def _credential_key(resource: ResourceIdentity) -> _CredentialKey:
        return _CredentialKey(
            resource.tenant_fingerprint,
            resource.credential_slot_id,
        )

    @staticmethod
    def _quota_key(resource: ResourceIdentity) -> _QuotaKey:
        return _QuotaKey(
            resource.endpoint_fingerprint,
            resource.tenant_fingerprint,
        )

    @staticmethod
    def _reject(
        gate: AdmissionGate,
        reason: str,
        resource: ResourceIdentity,
        failure_reason: FailureReason,
        domain: FailureDomain,
        retryability: Retryability,
        *,
        health_verdict: HealthVerdict = HealthVerdict.NEUTRAL,
        credential_verdict: CredentialVerdict = CredentialVerdict.NEUTRAL,
        quota_observation: QuotaObservation = QuotaObservation.NONE,
    ) -> AdmissionResult:
        return AdmissionResult(
            rejection=AdmissionVerdict(
                gate=gate,
                reason=reason,
                resource=resource,
                disposition=FailureDisposition(
                    reason=failure_reason,
                    domain=domain,
                    retryability=retryability,
                    health_verdict=health_verdict,
                    credential_verdict=credential_verdict,
                    quota_observation=quota_observation,
                ),
            )
        )


__all__ = [
    "AdmissionPermit",
    "AdmissionRejectedError",
    "AdmissionResult",
    "ResourceAdmissionController",
]
