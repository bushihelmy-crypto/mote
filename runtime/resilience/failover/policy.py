"""Deterministic provider-neutral failover decisions."""

from __future__ import annotations

from typing import Protocol

from mote.contracts.foundation.errors.codes import ErrorCode
from mote.contracts.model.failover import (
    DecisionKind,
    FailoverDecision,
    FailureDisposition,
    FailureDomain,
    FailureReason,
    RequestTransform,
    Retryability,
)


class FailoverPolicy(Protocol):
    policy_id: str

    def decide(self, disposition: FailureDisposition) -> FailoverDecision:
        ...


class DefaultFailoverPolicy:
    policy_id = "default-v1"

    def decide(self, disposition: FailureDisposition) -> FailoverDecision:
        reason = disposition.reason
        if disposition.retryability is Retryability.NEVER:
            return FailoverDecision(kind=DecisionKind.ABORT, reason=f"non-retryable:{reason.value}")
        if disposition.domain is FailureDomain.CREDENTIAL:
            return FailoverDecision(kind=DecisionKind.ROTATE_CREDENTIAL, reason=reason.value)
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
        if (
            disposition.retryability
            in {
                Retryability.NEW_ATTEMPT,
                Retryability.AFTER_HINT,
            }
            and disposition.domain is FailureDomain.TRANSPORT
        ):
            return FailoverDecision(
                kind=DecisionKind.RETRY_SAME_ENDPOINT,
                reason=reason.value,
            )
        return FailoverDecision(kind=DecisionKind.SWITCH_ENDPOINT, reason=reason.value)


__all__ = ["DefaultFailoverPolicy", "FailoverPolicy"]
