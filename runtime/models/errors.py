"""Runtime model-route lookup failures."""

from typing import ClassVar

from mote.contracts.foundation.errors.base import NonRetryableError
from mote.contracts.foundation.errors.codes import ErrorCode, RecoveryAction


class ModelNotFoundError(NonRetryableError):
    default_code: ClassVar[ErrorCode] = ErrorCode.ROUTER_MODEL_NOT_FOUND
    default_recovery: ClassVar[RecoveryAction] = RecoveryAction.FALLBACK


__all__ = ["ModelNotFoundError"]
