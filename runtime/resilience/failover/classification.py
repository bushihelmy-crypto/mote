"""Provider-neutral exception classification."""

from mote.contracts.foundation.errors.base import MoteError
from mote.contracts.foundation.errors.codes import ErrorCode
from mote.contracts.model.failover import (
    CredentialVerdict,
    FailureDisposition,
    FailureDomain,
    FailureReason,
    HealthVerdict,
    HttpCompatibilityClass,
    QuotaObservation,
    Retryability,
)
from mote.runtime.resilience.error_classification import is_retryable

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
    if isinstance(exc, MoteError):
        reason = _REASON_BY_CODE.get(exc.code, FailureReason.UNKNOWN)
        status_code = getattr(exc, "status_code", None)
        return FailureDisposition(
            reason=reason,
            domain=_failure_domain(reason),
            retryability=_retryability(reason, exc.retryable, exc.code),
            health_verdict=_health_verdict(reason),
            credential_verdict=_credential_verdict(reason),
            quota_observation=_quota_observation(reason, exc),
            provider_code=exc.code.value,
            safe_message=reason.value,
            http_compatibility_class=_http_class(reason, status_code),
        )
    reason = (
        FailureReason.TIMEOUT
        if isinstance(exc, TimeoutError)
        else (FailureReason.CONNECTION if isinstance(exc, ConnectionError) else FailureReason.UNKNOWN)
    )
    retryable = is_retryable(exc)
    return FailureDisposition(
        reason=reason,
        domain=_failure_domain(reason),
        retryability=Retryability.NEW_ATTEMPT if retryable else Retryability.NEVER,
        health_verdict=HealthVerdict.DEGRADE if retryable else HealthVerdict.NEUTRAL,
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
        return FailureDomain.PROVIDER
    if reason in {FailureReason.AUTH_REJECTED, FailureReason.BILLING_EXHAUSTED}:
        return FailureDomain.CREDENTIAL
    if reason in {
        FailureReason.CONTEXT_EXCEEDED,
        FailureReason.PAYLOAD_TOO_LARGE,
        FailureReason.IMAGE_TOO_LARGE,
        FailureReason.PROTOCOL_INCOMPATIBLE,
    }:
        return FailureDomain.PROTOCOL
    if reason in {
        FailureReason.RESPONSE_EMPTY,
        FailureReason.RESPONSE_UNPARSABLE,
        FailureReason.CONTENT_POLICY,
    }:
        return FailureDomain.PROTOCOL
    return FailureDomain.INTERNAL


def _retryability(reason: FailureReason, retryable: bool, code: ErrorCode | None = None) -> Retryability:
    if retryable:
        return Retryability.NEW_ATTEMPT
    if reason in {
        FailureReason.AUTH_REJECTED,
        FailureReason.BILLING_EXHAUSTED,
        FailureReason.MODEL_UNAVAILABLE,
        FailureReason.CONTEXT_EXCEEDED,
        FailureReason.PAYLOAD_TOO_LARGE,
        FailureReason.IMAGE_TOO_LARGE,
        FailureReason.RESPONSE_EMPTY,
    }:
        return Retryability.NEW_ATTEMPT
    if code in {
        ErrorCode.LLM_MULTIMODAL_TOOL_CONTENT,
        ErrorCode.LLM_INVALID_REQUEST_STATE,
    }:
        return Retryability.NEW_ATTEMPT
    return Retryability.NEVER


def _health_verdict(reason: FailureReason) -> HealthVerdict:
    if reason in {
        FailureReason.CONNECTION,
        FailureReason.TIMEOUT,
        FailureReason.OVERLOADED,
        FailureReason.SERVER_ERROR,
    }:
        return HealthVerdict.DEGRADE
    return HealthVerdict.NEUTRAL


def _credential_verdict(reason: FailureReason) -> CredentialVerdict:
    if reason is FailureReason.AUTH_REJECTED:
        return CredentialVerdict.QUARANTINE
    if reason is FailureReason.BILLING_EXHAUSTED:
        return CredentialVerdict.REVOKE
    return CredentialVerdict.NEUTRAL


def _quota_observation(reason: FailureReason, exc: MoteError) -> QuotaObservation:
    if reason is not FailureReason.RATE_LIMITED:
        return QuotaObservation.NONE
    retry_after = getattr(exc, "retry_after", None)
    return (
        QuotaObservation.RETRY_AFTER
        if isinstance(retry_after, (int, float)) and retry_after > 0
        else QuotaObservation.EXHAUSTED
    )


def _http_class(reason: FailureReason, status_code: object) -> HttpCompatibilityClass:
    if reason is FailureReason.AUTH_REJECTED:
        return HttpCompatibilityClass.AUTHENTICATION
    if reason is FailureReason.RATE_LIMITED:
        return HttpCompatibilityClass.QUOTA
    if reason in {
        FailureReason.CONTEXT_EXCEEDED,
        FailureReason.PAYLOAD_TOO_LARGE,
        FailureReason.IMAGE_TOO_LARGE,
        FailureReason.PROTOCOL_INCOMPATIBLE,
    }:
        return HttpCompatibilityClass.INVALID_REQUEST
    if isinstance(status_code, int) and status_code >= 500:
        return HttpCompatibilityClass.UNAVAILABLE
    if reason in {FailureReason.CONNECTION, FailureReason.TIMEOUT}:
        return HttpCompatibilityClass.TRANSPORT
    return HttpCompatibilityClass.INTERNAL


__all__ = ["classify_failure"]
