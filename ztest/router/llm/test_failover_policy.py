from mote.contracts.model.failover import (
    CredentialVerdict,
    DecisionKind,
    FailureReason,
    HealthVerdict,
    QuotaObservation,
    RequestTransform,
)
from mote.runtime.errors import (
    LLMAuthenticationError,
    LLMContentPolicyError,
    LLMInvalidRequestStateError,
    LLMRateLimitError,
)
from mote.runtime.resilience.failover.classification import classify_failure
from mote.runtime.resilience.failover.policy import DefaultFailoverPolicy


def test_rate_limit_is_quota_not_availability_failure() -> None:
    disposition = classify_failure(LLMRateLimitError("slow down", retry_after=3.0))
    decision = DefaultFailoverPolicy().decide(disposition)

    assert disposition.reason is FailureReason.RATE_LIMITED
    assert disposition.health_verdict is HealthVerdict.NEUTRAL
    assert disposition.quota_observation is QuotaObservation.RETRY_AFTER
    assert decision.kind is DecisionKind.RETRY_SAME_ENDPOINT
    assert decision.delay_seconds == 0.0


def test_credential_failure_produces_typed_rotation_decision() -> None:
    disposition = classify_failure(LLMAuthenticationError("rejected"))
    decision = DefaultFailoverPolicy().decide(disposition)

    assert disposition.reason is FailureReason.AUTH_REJECTED
    assert disposition.health_verdict is HealthVerdict.NEUTRAL
    assert disposition.credential_verdict is CredentialVerdict.QUARANTINE
    assert decision.kind is DecisionKind.ROTATE_CREDENTIAL


def test_request_state_failure_selects_exact_transform() -> None:
    disposition = classify_failure(LLMInvalidRequestStateError("bad state"))
    decision = DefaultFailoverPolicy().decide(disposition)

    assert decision.kind is DecisionKind.TRANSFORM_REQUEST
    assert decision.transform is RequestTransform.STRIP_REQUEST_STATE


def test_content_policy_fails_closed_without_cross_provider_fallback() -> None:
    disposition = classify_failure(LLMContentPolicyError("blocked"))
    decision = DefaultFailoverPolicy().decide(disposition)

    assert disposition.reason is FailureReason.CONTENT_POLICY
    assert decision.kind is DecisionKind.ABORT
