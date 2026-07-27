"""Stable cross-layer error identities and root exception semantics."""

from mote.contracts.errors.base import MoteError, NonRetryableError, RetryableError
from mote.contracts.errors.codes import ErrorCode, RecoveryAction
from mote.contracts.errors.models import (
    ModelCallBudgetExceededError,
    ModelCallDeadlineExceededError,
    ModelCallError,
    ModelCallExhaustedError,
    ModelCallInDoubtError,
    ModelCapabilityUnsatisfiedError,
    ModelGovernanceViolationError,
    ModelRouteUnavailableError,
)
from mote.contracts.errors.report import ErrorReport, render_error_block
from mote.contracts.errors.routing import (
    RoutingError,
    RoutingPolicyTimeoutError,
    RoutingProposalInvalidError,
    RoutingUnavailableError,
)
from mote.contracts.errors.services import (
    ServiceCallDeadlineExceededError,
    ServiceCallError,
    ServiceCallExhaustedError,
    ServiceCallInDoubtError,
    ServiceRouteUnavailableError,
)

__all__ = [
    "ErrorCode",
    "MoteError",
    "ModelCallBudgetExceededError",
    "ModelCallDeadlineExceededError",
    "ModelCallError",
    "ModelCallExhaustedError",
    "ModelCallInDoubtError",
    "ModelCapabilityUnsatisfiedError",
    "ModelGovernanceViolationError",
    "ModelRouteUnavailableError",
    "NonRetryableError",
    "RecoveryAction",
    "RetryableError",
    "ServiceCallDeadlineExceededError",
    "ServiceCallError",
    "ServiceCallExhaustedError",
    "ServiceCallInDoubtError",
    "ServiceRouteUnavailableError",
    "ErrorReport",
    "render_error_block",
    "RoutingError",
    "RoutingPolicyTimeoutError",
    "RoutingProposalInvalidError",
    "RoutingUnavailableError",
]
