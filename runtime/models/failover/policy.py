"""Stable failure classification and deterministic failover decisions."""

from __future__ import annotations

from typing import Protocol

from mote.contracts.errors.base import MoteError
from mote.contracts.errors.codes import ErrorCode
from mote.contracts.models.failover import (
    DecisionKind,
    FailoverDecision,
    FailureDisposition,
    FailureDomain,
    FailureReason,
    HealthVerdict,
    RequestTransform,
    Retryability,
)
from mote.runtime.errors import is_retryable


class FailoverPolicy(Protocol):
    policy_id: str

    def decide(self, disposition: FailureDisposition) -> FailoverDecision:
        ...


_REASON_BY_CODE = {
    ErrorCode.LLM_CONNECTION: FailureReason.CONNECTION,
    ErrorCode.LLM_TIMEOUT: FailureReason.TIMEOUT,
    ErrorCode.LLM_RATE_LIMIT: FailureReason.RATE_LIMITED,
    ErrorCode.LLM_OVERLOADED: FailureReason.OVERLOADED,
    ErrorCode.LLM_SERVER: FailureReason.SERVER_ERROR,
    ErrorCode.LLM_AUTH: FailureReason.AUTH_REJECTED,
    ErrorCode.LLM_BILLING: FailureReason.BILLING_EXHAUSTED,
    ErrorCode.LLM_RESOURCE_UNAVAILABLE: FailureReason.MODEL_UNAVAILABLE,
    ErrorCode.LLM_CONTEXT_WINDOW: FailureReason.CONTEXT_EXCEEDED,
    ErrorCode.LLM_PAYLOAD_TOO_LARGE: FailureReason.PAYLOAD_TOO_LARGE,
    ErrorCode.LLM_IMAGE_TOO_LARGE: FailureReason.IMAGE_TOO_LARGE,
    ErrorCode.LLM_BAD_REQUEST: FailureReason.PROTOCOL_INCOMPATIBLE,
    ErrorCode.LLM_MULTIMODAL_TOOL_CONTENT: FailureReason.PROTOCOL_INCOMPATIBLE,
    ErrorCode.LLM_INVALID_REQUEST_STATE: FailureReason.PROTOCOL_INCOMPATIBLE,
    ErrorCode.LLM_EMPTY_RESPONSE: FailureReason.RESPONSE_EMPTY,
    ErrorCode.LLM_UNUSABLE_RESPONSE: FailureReason.RESPONSE_EMPTY,
    ErrorCode.LLM_PARSE: FailureReason.RESPONSE_UNPARSABLE,
    ErrorCode.LLM_CONTENT_POLICY: FailureReason.CONTENT_POLICY,
}


def classify_failure(exc: Exception) -> FailureDisposition:
    """Project an exception onto policy-safe, provider-neutral facts."""

    if isinstance(exc, MoteError):
        reason = _REASON_BY_CODE.get(exc.code, FailureReason.UNKNOWN)
        retry_after = getattr(exc, "retry_after", None)
        status_code = getattr(exc, "status_code", None)
        return FailureDisposition(
            reason=reason,
            domain=_failure_domain(reason),
            retryability=_retryability(reason, exc.retryable, exc.code),
            health_verdict=_health_verdict(reason),
            retry_after_seconds=(
                float(retry_after) if isinstance(retry_after, (int, float)) and retry_after > 0 else None
            ),
            status_code=status_code if isinstance(status_code, int) else None,
            provider_code=exc.code.value,
        )
    if isinstance(exc, TimeoutError):
        reason = FailureReason.TIMEOUT
    elif isinstance(exc, ConnectionError):
        reason = FailureReason.CONNECTION
    else:
        reason = FailureReason.UNKNOWN
    retryable = is_retryable(exc)
    return FailureDisposition(
        reason=reason,
        domain=_failure_domain(reason),
        retryability=(Retryability.SAME_ENDPOINT if retryable else Retryability.NEVER),
        health_verdict=(HealthVerdict.AVAILABILITY_FAILURE if retryable else HealthVerdict.NEUTRAL),
    )


def _failure_domain(reason: FailureReason) -> FailureDomain:
    if reason in {
        FailureReason.CONNECTION,
        FailureReason.TIMEOUT,
        FailureReason.RATE_LIMITED,
    }:
        return FailureDomain.TRANSPORT
    if reason in {
        FailureReason.OVERLOADED,
        FailureReason.SERVER_ERROR,
        FailureReason.MODEL_UNAVAILABLE,
    }:
        return FailureDomain.ENDPOINT
    if reason in {
        FailureReason.AUTH_REJECTED,
        FailureReason.BILLING_EXHAUSTED,
    }:
        return FailureDomain.CREDENTIAL
    if reason in {
        FailureReason.CONTEXT_EXCEEDED,
        FailureReason.PAYLOAD_TOO_LARGE,
        FailureReason.IMAGE_TOO_LARGE,
        FailureReason.PROTOCOL_INCOMPATIBLE,
    }:
        return FailureDomain.REQUEST
    if reason in {
        FailureReason.RESPONSE_EMPTY,
        FailureReason.RESPONSE_UNPARSABLE,
        FailureReason.CONTENT_POLICY,
    }:
        return FailureDomain.RESPONSE
    return FailureDomain.UNKNOWN


def _retryability(
    reason: FailureReason,
    retryable: bool,
    code: ErrorCode | None = None,
) -> Retryability:
    if retryable:
        return Retryability.SAME_ENDPOINT
    if reason in {
        FailureReason.AUTH_REJECTED,
        FailureReason.BILLING_EXHAUSTED,
        FailureReason.MODEL_UNAVAILABLE,
        FailureReason.CONTEXT_EXCEEDED,
        FailureReason.PAYLOAD_TOO_LARGE,
        FailureReason.IMAGE_TOO_LARGE,
        FailureReason.RESPONSE_EMPTY,
    }:
        return Retryability.AFTER_CHANGE
    if code in {
        ErrorCode.LLM_MULTIMODAL_TOOL_CONTENT,
        ErrorCode.LLM_INVALID_REQUEST_STATE,
    }:
        return Retryability.AFTER_CHANGE
    return Retryability.NEVER


def _health_verdict(reason: FailureReason) -> HealthVerdict:
    if reason is FailureReason.RATE_LIMITED:
        return HealthVerdict.QUOTA_LIMITED
    if reason in {FailureReason.AUTH_REJECTED, FailureReason.BILLING_EXHAUSTED}:
        return HealthVerdict.CREDENTIAL_REJECTED
    if reason in {
        FailureReason.CONNECTION,
        FailureReason.TIMEOUT,
        FailureReason.OVERLOADED,
        FailureReason.SERVER_ERROR,
    }:
        return HealthVerdict.AVAILABILITY_FAILURE
    return HealthVerdict.NEUTRAL


class DefaultFailoverPolicy:
    """The bounded compatibility policy used while adapters are being migrated."""

    policy_id = "default-v1"

    def decide(self, disposition: FailureDisposition) -> FailoverDecision:
        reason = disposition.reason
        if disposition.retryability is Retryability.NEVER:
            return FailoverDecision(
                kind=DecisionKind.ABORT,
                reason=f"non-retryable:{reason.value}",
            )
        if disposition.domain is FailureDomain.CREDENTIAL:
            return FailoverDecision(
                kind=DecisionKind.ROTATE_CREDENTIAL,
                reason=reason.value,
            )
        transforms = {
            FailureReason.CONTEXT_EXCEEDED: RequestTransform.COMPRESS,
            FailureReason.PAYLOAD_TOO_LARGE: RequestTransform.COMPRESS,
            FailureReason.IMAGE_TOO_LARGE: RequestTransform.SHRINK_IMAGE,
        }
        transform = transforms.get(reason)
        if disposition.provider_code == ErrorCode.LLM_MULTIMODAL_TOOL_CONTENT.value:
            transform = RequestTransform.DOWNGRADE_TOOL_CONTENT
        elif disposition.provider_code == ErrorCode.LLM_INVALID_REQUEST_STATE.value:
            transform = RequestTransform.STRIP_REQUEST_STATE
        if transform is not None:
            return FailoverDecision(
                kind=DecisionKind.TRANSFORM_REQUEST,
                reason=reason.value,
                transform=transform,
            )
        if disposition.retryability is Retryability.SAME_ENDPOINT:
            return FailoverDecision(
                kind=DecisionKind.RETRY_SAME_ENDPOINT,
                reason=reason.value,
                delay_seconds=disposition.retry_after_seconds or 0.0,
            )
        return FailoverDecision(
            kind=DecisionKind.SWITCH_ENDPOINT,
            reason=reason.value,
        )


__all__ = [
    "DefaultFailoverPolicy",
    "FailoverPolicy",
    "classify_failure",
]
