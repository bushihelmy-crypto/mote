"""Stable terminal failures for externally hosted Tool service calls."""

from __future__ import annotations

from typing import ClassVar

from mote.contracts.foundation.errors.base import NonRetryableError
from mote.contracts.foundation.errors.codes import ErrorCode


class ServiceCallError(NonRetryableError):
    """Base for a logical Tool service call without an accepted response."""


class ServiceCallExhaustedError(ServiceCallError):
    default_code: ClassVar[ErrorCode] = ErrorCode.SERVICE_CALL_EXHAUSTED


class ServiceCallDeadlineExceededError(ServiceCallError):
    default_code: ClassVar[ErrorCode] = ErrorCode.SERVICE_CALL_DEADLINE_EXCEEDED


class ServiceCallInDoubtError(ServiceCallError):
    default_code: ClassVar[ErrorCode] = ErrorCode.SERVICE_CALL_IN_DOUBT


class ServiceRouteUnavailableError(ServiceCallError):
    default_code: ClassVar[ErrorCode] = ErrorCode.SERVICE_ROUTE_UNAVAILABLE


__all__ = [
    "ServiceCallDeadlineExceededError",
    "ServiceCallError",
    "ServiceCallExhaustedError",
    "ServiceCallInDoubtError",
    "ServiceRouteUnavailableError",
]
