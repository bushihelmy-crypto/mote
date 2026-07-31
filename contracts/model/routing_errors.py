"""Stable failures raised by the semantic routing decision plane."""

from __future__ import annotations

from mote.contracts.foundation.errors.base import NonRetryableError


class RoutingError(NonRetryableError):
    pass


class RoutingUnavailableError(RoutingError):
    pass


class RoutingPolicyTimeoutError(RoutingError):
    pass


class RoutingProposalInvalidError(RoutingError):
    pass


__all__ = [
    "RoutingError",
    "RoutingPolicyTimeoutError",
    "RoutingProposalInvalidError",
    "RoutingUnavailableError",
]
