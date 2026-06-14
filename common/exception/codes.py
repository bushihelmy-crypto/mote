"""Stable error codes for the MetaGPT exception hierarchy.

Each concrete exception declares a ``default_code`` from this enum so that
serialized errors (``MetaGPTError.to_dict``) carry a stable, machine-readable
identifier independent of the human-readable message or the class name.
"""

from __future__ import annotations

from enum import StrEnum


class ErrorCode(StrEnum):
    """Machine-readable error codes (stable across releases)."""

    UNKNOWN = "UNKNOWN"

    # LLM / provider tier
    LLM_CONNECTION = "LLM_CONNECTION"
    LLM_TIMEOUT = "LLM_TIMEOUT"
    LLM_RATE_LIMIT = "LLM_RATE_LIMIT"
    LLM_EMPTY_RESPONSE = "LLM_EMPTY_RESPONSE"
    LLM_AUTH = "LLM_AUTH"
    LLM_BAD_REQUEST = "LLM_BAD_REQUEST"
    LLM_PARSE = "LLM_PARSE"
    LLM_CONTEXT_WINDOW = "LLM_CONTEXT_WINDOW"
    LLM_PAYLOAD_TOO_LARGE = "LLM_PAYLOAD_TOO_LARGE"
    LLM_BILLING = "LLM_BILLING"
    LLM_OVERLOADED = "LLM_OVERLOADED"
    LLM_SERVER = "LLM_SERVER"
    LLM_CONTENT_POLICY = "LLM_CONTENT_POLICY"
    LLM_IMAGE_TOO_LARGE = "LLM_IMAGE_TOO_LARGE"
    LLM_MULTIMODAL_TOOL_CONTENT = "LLM_MULTIMODAL_TOOL_CONTENT"
    LLM_INVALID_REQUEST_STATE = "LLM_INVALID_REQUEST_STATE"

    # Router tier (model selection / provider registry)
    ROUTER_MODEL_NOT_FOUND = "ROUTER_MODEL_NOT_FOUND"
    ROUTER_PROVIDER_NOT_FOUND = "ROUTER_PROVIDER_NOT_FOUND"

    # Tool tier
    TOOL = "TOOL"
    TOOL_VALIDATION = "TOOL_VALIDATION"
    TOOL_NOT_FOUND = "TOOL_NOT_FOUND"
    TOOL_NON_RETRYABLE = "TOOL_NON_RETRYABLE"
    TOOL_RETRYABLE = "TOOL_RETRYABLE"

    # Config / environment tier
    CONFIG_INVALID = "CONFIG_INVALID"
    CONFIG_MISSING_API_KEY = "CONFIG_MISSING_API_KEY"
    ENV_KEY_NOT_FOUND = "ENV_KEY_NOT_FOUND"

    # Agent / role tier
    AGENT_CONTEXT_NOT_SET = "AGENT_CONTEXT_NOT_SET"

    # Agent control-plane tier (metagpt.environment)
    AGENT_CONTROL = "AGENT_CONTROL"
    AGENT_LIMIT_REACHED = "AGENT_LIMIT_REACHED"
    AGENT_NOT_FOUND = "AGENT_NOT_FOUND"
    AGENT_PATH_EXISTS = "AGENT_PATH_EXISTS"
    AGENT_NOT_KNOWN = "AGENT_NOT_KNOWN"

    # Resource / budget tier
    RESOURCE_NO_MONEY = "RESOURCE_NO_MONEY"


class RecoveryAction(StrEnum):
    """Suggested recovery for an error — a *hint* for the retry/failover loop.

    ``MetaGPTError.recovery`` derives a sensible default from ``retryable`` (RETRY
    vs ABORT); concrete exceptions override ``default_recovery`` when a more
    specific action applies (e.g. context overflow → COMPRESS, auth/billing →
    ROTATE_CREDENTIAL).

    The first five actions are wired in ``RecoveryRunner``. The trailing
    "transform the outgoing request then retry" family (SHRINK_IMAGE /
    DOWNGRADE_TOOL_CONTENT / STRIP_REQUEST_STATE) is dispatched through the
    runner's injectable ``message_transformers`` registry, wired by default to
    ``router.llm.transformers.DEFAULT_MESSAGE_TRANSFORMERS`` (provider-specific
    payload repairs ported conceptually from hermes-agent). A transformer that
    can't repair the payload returns ``None``, degrading that action to a
    re-raise. The exception layer only emits these hints — it never imports the
    transformers.
    """

    ABORT = "abort"  # give up — surface to caller (default for non-retryable)
    RETRY = "retry"  # retry the same request, typically with backoff
    COMPRESS = "compress"  # request too large — compress context then retry
    ROTATE_CREDENTIAL = "rotate_credential"  # auth/billing — switch key/account
    FALLBACK = "fallback"  # switch to a different model / provider
    # ── Reserved: "repair the outgoing request, then retry the same provider" ──
    SHRINK_IMAGE = "shrink_image"  # an image part exceeds the provider's per-image limit
    DOWNGRADE_TOOL_CONTENT = "downgrade_tool_content"  # provider rejects list-type tool content
    STRIP_REQUEST_STATE = "strip_request_state"  # drop opaque request state the provider rejects
