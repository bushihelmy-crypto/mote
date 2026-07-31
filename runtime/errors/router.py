"""Stable lookup failures at the model route and provider composition seams."""

from __future__ import annotations

from typing import ClassVar

from mote.contracts.foundation.errors.base import MoteError, NonRetryableError
from mote.contracts.foundation.errors.codes import ErrorCode, RecoveryAction


class RouterError(MoteError):
    """Base for model-routing failures."""


class ModelNotFoundError(RouterError, NonRetryableError):
    """The canonical ModelGateway does not expose the requested logical route."""

    default_code: ClassVar[ErrorCode] = ErrorCode.ROUTER_MODEL_NOT_FOUND
    default_recovery: ClassVar[RecoveryAction] = RecoveryAction.FALLBACK


class ProviderNotFoundError(RouterError, NonRetryableError):
    """No LLM provider class is registered for the requested ``LLMType``.

    Raised by the provider registry when ``config.api_type`` has no
    provider catalog entry (typically a misconfigured ``api_type``).
    """

    default_code: ClassVar[ErrorCode] = ErrorCode.ROUTER_PROVIDER_NOT_FOUND
