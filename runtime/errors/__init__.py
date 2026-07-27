"""Mote global exception system.

A single, extensible hierarchy rooted at :class:`MoteError`:

- ``RetryableError`` / ``NonRetryableError`` are marker mixins that flip the
  ``retryable`` ClassVar, so retry predicates decide on *semantics* rather than
  vendor exception tuples (see :func:`is_retryable`).
- Every concrete error carries a stable :class:`ErrorCode` and serializes via
  ``to_dict()``.
- ``ToolError``, ``NonRetryableToolError``, ``EnvKeyNotFoundError`` and
  ``NoMoneyException`` are rooted in this hierarchy and also re-exported from
  the modules that raise them.
"""

from __future__ import annotations

from mote.contracts.errors.base import MoteError, NonRetryableError, RetryableError
from mote.contracts.errors.codes import ErrorCode, RecoveryAction
from mote.contracts.errors.config import (
    ConfigError,
    ConfigValidationError,
    EnvKeyNotFoundError,
    MissingAPIKeyError,
    UnknownConfigKeysError,
)
from mote.contracts.errors.environment import (
    AgentControlError,
    AgentLimitReached,
    AgentNotFound,
    AgentNotKnown,
    AgentPathExists,
)
from mote.contracts.errors.graph import (
    GraphBatchFailureError,
    GraphError,
    GraphNodeRetryExhaustedError,
    GraphNodeTimeoutError,
    GraphParamTypeError,
    GraphRecursionError,
    GraphRouterError,
)
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
from mote.contracts.errors.output import (
    OutputCommitFencedError,
    OutputCommitStateError,
    OutputCorrectionExhaustedError,
    OutputResumeContractMismatchError,
    OutputValidatorError,
    OutputValidatorUnavailableError,
    RunLeaseCoordinatorUnavailableError,
    RunLeaseUnavailableError,
)
from mote.contracts.errors.report import ErrorReport, render_error_block
from mote.contracts.errors.runtimes import (
    LeaseCoordinatorUnavailableError,
    LeaseFencedError,
    LeaseUnavailableError,
    ManagedRuntimeAliasConflictError,
    ManagedRuntimeNotFoundError,
    ManagedRuntimeRevisionConflictError,
    ManagedRuntimeStateError,
)
from mote.contracts.errors.tasks import BackgroundTaskCancelledError, BackgroundTaskError, BackgroundTaskTimeoutError
from mote.contracts.errors.tools import (
    NonRetryableToolError,
    RetryableToolError,
    ToolError,
    ToolNotConfiguredError,
    ToolNotFoundError,
    ToolPermissionDeniedError,
    ToolValidationError,
)
from mote.runtime.errors.agent import AgentError, RoleContextNotSetError, SessionResumeIdentityError

# ``handlers`` is leaf-tier and imported eagerly. Its only cross-package edge is
# ``from mote.runtime.errors.handlers import handle_exception``; both
# ``common.utils`` (re-exports only ``token_counter`` → tiktoken/loguru) and
# ``common.utils.exceptions`` (imports only ``common.logs``) are pure leaves, so this
# pulls in neither ``config2`` nor ``llm_config`` — the helpers are plain top-level
# imports with no import cycle.
from mote.runtime.errors.classification import classify_llm_error, handle_exception, is_retryable
from mote.runtime.errors.llm import (
    ContextWindowExceededError,
    LLMAuthenticationError,
    LLMBadRequestError,
    LLMBillingError,
    LLMConnectionError,
    LLMContentPolicyError,
    LLMEmptyResponseError,
    LLMError,
    LLMImageTooLargeError,
    LLMInvalidRequestStateError,
    LLMMultimodalToolContentError,
    LLMOverloadedError,
    LLMPayloadTooLargeError,
    LLMRateLimitError,
    LLMResourceUnavailableError,
    LLMResponseParseError,
    LLMServerError,
    LLMTimeoutError,
    LLMUnusableResponseError,
)
from mote.runtime.errors.oauth import JWTDecodeError, OAuthConfigError, OAuthError, OAuthHTTPError, OAuthRefreshError
from mote.runtime.errors.recovery import Call, RecoveryRunner, RecoveryStrategy
from mote.runtime.errors.resource import BudgetExceededError, NoMoneyException, ResourceError
from mote.runtime.errors.router import ModelNotFoundError, ProviderNotFoundError, RouterError

__all__ = [
    # base + markers
    "MoteError",
    "RetryableError",
    "NonRetryableError",
    "ErrorCode",
    "RecoveryAction",
    # recovery / failover skeleton
    "RecoveryRunner",
    "RecoveryStrategy",
    "Call",
    # error presentation contract
    "ErrorReport",
    "render_error_block",
    # llm tier
    "LLMError",
    "LLMConnectionError",
    "LLMTimeoutError",
    "LLMRateLimitError",
    "LLMEmptyResponseError",
    "LLMOverloadedError",
    "LLMServerError",
    "LLMAuthenticationError",
    "LLMBillingError",
    "LLMBadRequestError",
    "LLMResponseParseError",
    "LLMContentPolicyError",
    "LLMResourceUnavailableError",
    "ContextWindowExceededError",
    "LLMPayloadTooLargeError",
    "LLMImageTooLargeError",
    "LLMMultimodalToolContentError",
    "LLMInvalidRequestStateError",
    "LLMUnusableResponseError",
    "ModelCallBudgetExceededError",
    "ModelCallDeadlineExceededError",
    "ModelCallError",
    "ModelCallExhaustedError",
    "ModelCallInDoubtError",
    "ModelCapabilityUnsatisfiedError",
    "ModelGovernanceViolationError",
    "ModelRouteUnavailableError",
    # router tier
    "RouterError",
    "ModelNotFoundError",
    "ProviderNotFoundError",
    # tool tier
    "ToolError",
    "ToolValidationError",
    "ToolNotFoundError",
    "ToolNotConfiguredError",
    "ToolPermissionDeniedError",
    "NonRetryableToolError",
    "RetryableToolError",
    # graph execution tier
    "GraphError",
    "GraphRouterError",
    "GraphRecursionError",
    "GraphBatchFailureError",
    "GraphNodeTimeoutError",
    "GraphNodeRetryExhaustedError",
    "GraphParamTypeError",
    # background-task tier
    "BackgroundTaskError",
    "BackgroundTaskTimeoutError",
    "BackgroundTaskCancelledError",
    # oauth / credential tier
    "OAuthError",
    "OAuthConfigError",
    "OAuthHTTPError",
    "OAuthRefreshError",
    "JWTDecodeError",
    # config tier
    "ConfigError",
    "ConfigValidationError",
    "UnknownConfigKeysError",
    "MissingAPIKeyError",
    "EnvKeyNotFoundError",
    # agent tier
    "AgentError",
    "RoleContextNotSetError",
    "SessionResumeIdentityError",
    # typed run output tier
    "OutputCorrectionExhaustedError",
    "OutputResumeContractMismatchError",
    "OutputCommitFencedError",
    "RunLeaseUnavailableError",
    "RunLeaseCoordinatorUnavailableError",
    "OutputCommitStateError",
    "OutputValidatorError",
    "OutputValidatorUnavailableError",
    # generic lease + managed runtime tier
    "LeaseCoordinatorUnavailableError",
    "LeaseFencedError",
    "LeaseUnavailableError",
    "ManagedRuntimeAliasConflictError",
    "ManagedRuntimeNotFoundError",
    "ManagedRuntimeRevisionConflictError",
    "ManagedRuntimeStateError",
    # agent control-plane tier
    "AgentControlError",
    "AgentLimitReached",
    "AgentNotFound",
    "AgentPathExists",
    "AgentNotKnown",
    # resource tier
    "ResourceError",
    "NoMoneyException",
    "BudgetExceededError",
    # helpers
    "is_retryable",
    "classify_llm_error",
    "handle_exception",
]
