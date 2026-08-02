"""Stable terminal failures for externally hosted Tool service calls."""

from __future__ import annotations

from typing import ClassVar

from mote.contracts.foundation.errors.base import NonRetryableError
from mote.contracts.foundation.errors.codes import ErrorCode
from mote.contracts.service.models import ServiceResumeHandle


class ServiceCallError(NonRetryableError):
    """Base for a logical Tool service call without an accepted response."""


class ServiceCallExhaustedError(ServiceCallError):
    default_code: ClassVar[ErrorCode] = ErrorCode.SERVICE_CALL_EXHAUSTED


class ServiceCallDeadlineExceededError(ServiceCallError):
    default_code: ClassVar[ErrorCode] = ErrorCode.SERVICE_CALL_DEADLINE_EXCEEDED


class ServiceCallWaitingRemoteError(ServiceCallDeadlineExceededError):
    def __init__(self, message: str, *, resume_handle: ServiceResumeHandle) -> None:
        self.resume_handle = resume_handle
        super().__init__(
            message,
            service_call_id=resume_handle.service_call_id,
            stream_revision=resume_handle.stream_revision,
        )


class ServiceCallInDoubtError(ServiceCallError):
    default_code: ClassVar[ErrorCode] = ErrorCode.SERVICE_CALL_IN_DOUBT


class ServiceRouteUnavailableError(ServiceCallError):
    default_code: ClassVar[ErrorCode] = ErrorCode.SERVICE_ROUTE_UNAVAILABLE


__all__ = [
    "ServiceCallDeadlineExceededError",
    "ServiceCallError",
    "ServiceCallExhaustedError",
    "ServiceCallInDoubtError",
    "ServiceCallWaitingRemoteError",
    "ServiceRouteUnavailableError",
]
