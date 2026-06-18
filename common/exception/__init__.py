"""MetaGPT global exception system.

A single, extensible hierarchy rooted at :class:`MetaGPTError`:

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

from metagpt.common.exception.agent import AgentError, RoleContextNotSetError
from metagpt.common.exception.base import (
    MetaGPTError,
    NonRetryableError,
    RetryableError,
)
from metagpt.common.exception.codes import ErrorCode, RecoveryAction
from metagpt.common.exception.recovery import (
    Call,
    RecoveryRunner,
    RecoveryStrategy,
)
from metagpt.common.exception.report import ErrorReport, render_error_block
from metagpt.common.exception.config import (
    ConfigError,
    ConfigValidationError,
    EnvKeyNotFoundError,
    MissingAPIKeyError,
    UnknownConfigKeysError,
)
from metagpt.common.exception.environment import (
    AgentControlError,
    AgentLimitReached,
    AgentNotFound,
    AgentNotKnown,
    AgentPathExists,
)
from metagpt.common.exception.graph import (
    GraphBatchFailureError,
    GraphError,
    GraphNodeRetryExhaustedError,
    GraphNodeTimeoutError,
    GraphParamTypeError,
    GraphRecursionError,
    GraphRouterError,
)
from metagpt.common.exception.llm import (
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
from metagpt.common.exception.oauth import (
    JWTDecodeError,
    OAuthConfigError,
    OAuthError,
    OAuthHTTPError,
    OAuthRefreshError,
)
from metagpt.common.exception.resource import (
    BudgetExceededError,
    NoMoneyException,
    ResourceError,
)
from metagpt.common.exception.router import (
    ModelNotFoundError,
    ProviderNotFoundError,
    RouterControlValidationError,
    RouterError,
)
from metagpt.common.exception.tool import (
    ApplyPatchError,
    NonRetryableToolError,
    RetryableToolError,
    ToolError,
    ToolNotFoundError,
    ToolPermissionDeniedError,
    ToolValidationError,
)
from metagpt.common.exception.task import (
    BackgroundTaskCancelledError,
    BackgroundTaskError,
    BackgroundTaskTimeoutError,
)

# NOTE: ``handlers`` is imported LAST on purpose. It pulls in
# ``common.utils.exceptions`` → utils → schema → document →
# ``common.utils.common``, whose module body imports ``MetaGPTError`` and
# ``NoMoneyException`` back from this package.
#
# Unlike the tiers above (pure leaves that import only ``base``/``codes``), the
# ``handlers`` submodule re-exports ``handle_exception`` from ``common.utils`` and
# therefore drags the heavyweight ``common.utils`` package (→ ``config2`` →
# ``llm_config``, which itself imports from THIS package) into what should be a leaf
# exception package. Importing it eagerly at the top of ``__init__`` makes any module
# that does ``from metagpt.common.exception import <leaf-error>`` (e.g.
# ``llm_config.py`` for ``MissingAPIKeyError``) re-enter a half-initialised
# ``common.utils``/``llm_config`` and crash with a circular ImportError.
#
# Fix: expose the three handler helpers LAZILY via PEP 562 ``__getattr__``. The
# package body now finishes with only leaf-tier imports, so importing it never pulls
# in ``common.utils``. The first actual *access* to ``is_retryable`` /
# ``classify_llm_error`` / ``handle_exception`` (always well after ``llm_config`` /
# ``config2`` have finished initialising) loads ``handlers`` on demand — no cycle.
_LAZY_HANDLER_NAMES = frozenset({"classify_llm_error", "handle_exception", "is_retryable"})


def __getattr__(name: str):  # PEP 562
    if name in _LAZY_HANDLER_NAMES:
        from metagpt.common.exception import handlers

        return getattr(handlers, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    # base + markers
    "MetaGPTError",
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
