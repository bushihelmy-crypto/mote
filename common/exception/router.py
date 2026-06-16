"""Router tier exceptions (``metagpt/router``).

Routing is a distinct concern from the LLM call itself: it selects *which* model
to use and resolves the concrete provider class. These are configuration-shaped,
non-retryable failures — retrying the same routing request with the same
registry would fail identically.
"""

from __future__ import annotations

from typing import ClassVar

from metagpt.common.exception.base import MetaGPTError, NonRetryableError
from metagpt.common.exception.codes import ErrorCode, RecoveryAction


class RouterError(MetaGPTError):
    """Base for model-routing failures."""


class ModelNotFoundError(RouterError, NonRetryableError):
    """No registered model card matches the requested name.

    Raised by ``LLMRouter._build`` when an explicit/task-mapped name has no
    corresponding :class:`~metagpt.router.schema.ModelCard`. The suggested
    recovery is to fall back to another model rather than abort outright.
    """

    default_code: ClassVar[ErrorCode] = ErrorCode.ROUTER_MODEL_NOT_FOUND
    default_recovery: ClassVar[RecoveryAction] = RecoveryAction.FALLBACK


class ProviderNotFoundError(RouterError, NonRetryableError):
    """No LLM provider class is registered for the requested ``LLMType``.

    Raised by the provider registry when ``config.api_type`` has no
    ``@register_provider`` entry (typically a misconfigured ``api_type``).
    """

    default_code: ClassVar[ErrorCode] = ErrorCode.ROUTER_PROVIDER_NOT_FOUND


class RouterControlValidationError(RouterError, NonRetryableError, ValueError):
    """A router-control hold target id is not among the registered models.

    Inherits ``ValueError`` to preserve backward compatibility with existing
    ``except ValueError`` handlers around control-target resolution.
    """

    default_code: ClassVar[ErrorCode] = ErrorCode.ROUTER_CONTROL_INVALID
