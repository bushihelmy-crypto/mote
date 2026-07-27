"""Stable terminal failures for provider-neutral model calls."""

from __future__ import annotations

from typing import ClassVar

from mote.contracts.errors.base import NonRetryableError
from mote.contracts.errors.codes import ErrorCode


class ModelCallError(NonRetryableError):
    """Base for a logical model call that cannot produce an accepted response."""


class ModelCallExhaustedError(ModelCallError):
    default_code: ClassVar[ErrorCode] = ErrorCode.MODEL_CALL_EXHAUSTED


class ModelCallDeadlineExceededError(ModelCallError):
    default_code: ClassVar[ErrorCode] = ErrorCode.MODEL_CALL_DEADLINE_EXCEEDED


class ModelCallBudgetExceededError(ModelCallError):
    default_code: ClassVar[ErrorCode] = ErrorCode.MODEL_CALL_BUDGET_EXCEEDED


class ModelRouteUnavailableError(ModelCallError):
    default_code: ClassVar[ErrorCode] = ErrorCode.MODEL_ROUTE_UNAVAILABLE


class ModelCapabilityUnsatisfiedError(ModelCallError):
    default_code: ClassVar[ErrorCode] = ErrorCode.MODEL_CAPABILITY_UNSATISFIED


class ModelGovernanceViolationError(ModelCallError):
    default_code: ClassVar[ErrorCode] = ErrorCode.MODEL_GOVERNANCE_VIOLATION


class ModelCallInDoubtError(ModelCallError):
    default_code: ClassVar[ErrorCode] = ErrorCode.MODEL_CALL_IN_DOUBT


__all__ = [
    "ModelCallBudgetExceededError",
    "ModelCallDeadlineExceededError",
    "ModelCallError",
    "ModelCallExhaustedError",
    "ModelCallInDoubtError",
    "ModelCapabilityUnsatisfiedError",
    "ModelGovernanceViolationError",
    "ModelRouteUnavailableError",
]
