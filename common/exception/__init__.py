"""Mote global exception system.

A single, extensible hierarchy rooted at :class:`MoteError`:

- ``RetryableError`` / ``NonRetryableError`` are marker mixins that flip the
  ``retryable`` ClassVar, so retry predicates decide on *semantics* rather than
  vendor exception tuples (see :func:`is_retryable`).
- Every concrete error carries a stable :class:`ErrorCode` and serializes via
  ``to_dict()``.
- The four legacy exceptions (``ToolError``, ``NonRetryableToolError``,
  ``EnvKeyNotFoundError``, ``NoMoneyException``) are reparented here and
  re-exported from their original modules for backward compatibility.
"""

from __future__ import annotations

from mote.common.exception.agent import AgentError, RoleContextNotSetError
from mote.common.exception.base import MoteError, NonRetryableError, RetryableError
from mote.common.exception.codes import ErrorCode, RecoveryAction
from mote.common.exception.config import (
    ConfigError,
    ConfigValidationError,
    EnvKeyNotFoundError,
    MissingAPIKeyError,
    UnknownConfigKeysError,
)
from mote.common.exception.environment import (
    AgentControlError,
    AgentLimitReached,
    AgentNotFound,
    AgentNotKnown,
    AgentPathExists,
)
from mote.common.exception.graph import (
    GraphBatchFailureError,
    GraphError,
    GraphNodeRetryExhaustedError,
    GraphNodeTimeoutError,
    GraphParamTypeError,
    GraphRecursionError,
    GraphRouterError,
)

# ``handlers`` is leaf-tier and imported eagerly. Its only cross-package edge is
# ``from mote.common.utils.exceptions import handle_exception``; both
# ``common.utils`` (re-exports only ``token_counter`` → tiktoken/loguru) and
# ``common.utils.exceptions`` (imports only ``common.logs``) are pure leaves, so this
# pulls in neither ``config2`` nor ``llm_config``. (Historically ``common.utils``
# re-exported ``Singleton``/``read_docx`` which dragged config2 in, forcing a PEP 562
# lazy ``__getattr__`` here; that re-export was dropped, so the cycle is gone and the
# helpers are now plain top-level imports.)
from mote.common.exception.handlers import classify_llm_error, handle_exception, is_retryable
from mote.common.exception.llm import (
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
    LLMResponseParseError,
    LLMServerError,
    LLMTimeoutError,
)
from mote.common.exception.oauth import JWTDecodeError, OAuthConfigError, OAuthError, OAuthHTTPError, OAuthRefreshError
from mote.common.exception.recovery import Call, RecoveryRunner, RecoveryStrategy
from mote.common.exception.report import ErrorReport, render_error_block
from mote.common.exception.resource import BudgetExceededError, NoMoneyException, ResourceError
from mote.common.exception.router import (
    ModelNotFoundError,
    ProviderNotFoundError,
    RouterControlValidationError,
    RouterError,
)
from mote.common.exception.task import BackgroundTaskCancelledError, BackgroundTaskError, BackgroundTaskTimeoutError
from mote.common.exception.tool import (
    ApplyPatchError,
    NonRetryableToolError,
    RetryableToolError,
    ToolError,
    ToolNotFoundError,
    ToolPermissionDeniedError,
    ToolValidationError,
)

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
    "ContextWindowExceededError",
    "LLMPayloadTooLargeError",
    "LLMImageTooLargeError",
    "LLMMultimodalToolContentError",
    "LLMInvalidRequestStateError",
    # router tier
    "RouterError",
    "ModelNotFoundError",
    "ProviderNotFoundError",
    "RouterControlValidationError",
    # tool tier
    "ToolError",
    "ToolValidationError",
    "ToolNotFoundError",
    "ToolPermissionDeniedError",
    "NonRetryableToolError",
    "RetryableToolError",
    "ApplyPatchError",
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
