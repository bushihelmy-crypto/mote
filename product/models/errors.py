"""Product model-provider catalog failures."""

from typing import ClassVar

from mote.contracts.foundation.errors.base import NonRetryableError
from mote.contracts.foundation.errors.codes import ErrorCode


class ProviderNotFoundError(NonRetryableError):
    default_code: ClassVar[ErrorCode] = ErrorCode.ROUTER_PROVIDER_NOT_FOUND


__all__ = ["ProviderNotFoundError"]
