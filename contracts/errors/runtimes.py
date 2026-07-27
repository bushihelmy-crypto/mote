"""Stable failures for generic leases and managed interactive runtimes."""
from __future__ import annotations

from typing import ClassVar

from mote.contracts.errors.base import NonRetryableError, RetryableError
from mote.contracts.errors.codes import ErrorCode


class LeaseFencedError(NonRetryableError):
    """A stale or expired owner attempted to commit a protected mutation."""

    default_code: ClassVar[ErrorCode] = ErrorCode.LEASE_FENCED


class LeaseUnavailableError(RetryableError):
    """Another live owner currently holds the requested lease."""

    default_code: ClassVar[ErrorCode] = ErrorCode.LEASE_UNAVAILABLE


class LeaseCoordinatorUnavailableError(RetryableError):
    """The lease backend cannot currently prove ownership."""

    default_code: ClassVar[ErrorCode] = ErrorCode.LEASE_COORDINATOR_UNAVAILABLE


class ManagedRuntimeNotFoundError(NonRetryableError):
    """A RuntimeRef or readable runtime alias does not resolve."""

    default_code: ClassVar[ErrorCode] = ErrorCode.RUNTIME_NOT_FOUND


class ManagedRuntimeAliasConflictError(NonRetryableError):
    """A live runtime already owns the requested ``kind:alias`` pair."""

    default_code: ClassVar[ErrorCode] = ErrorCode.RUNTIME_ALIAS_CONFLICT


class ManagedRuntimeStateError(NonRetryableError):
    """An operation is invalid for the runtime's current lifecycle state."""

    default_code: ClassVar[ErrorCode] = ErrorCode.RUNTIME_INVALID_STATE


class ManagedRuntimeRevisionConflictError(RetryableError):
    """A caller based its mutation on a stale runtime revision."""

    default_code: ClassVar[ErrorCode] = ErrorCode.RUNTIME_REVISION_CONFLICT


class ManagedRuntimeDurabilityError(NonRetryableError):
    """A mutation committed in memory but its required local fact did not persist."""

    default_code: ClassVar[ErrorCode] = ErrorCode.RUNTIME_DURABILITY_FAILED


__all__ = [
    "LeaseCoordinatorUnavailableError",
    "LeaseFencedError",
    "LeaseUnavailableError",
    "ManagedRuntimeAliasConflictError",
    "ManagedRuntimeDurabilityError",
    "ManagedRuntimeNotFoundError",
    "ManagedRuntimeRevisionConflictError",
    "ManagedRuntimeStateError",
]
