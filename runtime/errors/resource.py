"""Resource / budget tier exceptions.

``NoMoneyException`` keeps its name, its ``amount``/``message`` fields, and its
``__str__`` so existing raise/catch sites are unaffected. ``BudgetExceededError``
is provided as a clearer alias for new code.
"""

from __future__ import annotations

from typing import ClassVar

from mote.contracts.foundation.errors.base import MoteError, NonRetryableError
from mote.contracts.foundation.errors.codes import ErrorCode


class ResourceError(MoteError):
    """Base for resource / quota / budget failures."""


class NoMoneyException(ResourceError, NonRetryableError):
    """Raised when the operation cannot be completed due to insufficient funds."""

    default_code: ClassVar[ErrorCode] = ErrorCode.RESOURCE_NO_MONEY

    def __init__(self, amount, message: str = "Insufficient funds") -> None:
        self.amount = amount
        self.message = message
        super().__init__(message)

    def __str__(self) -> str:
        return f"{self.message} -> Amount required: {self.amount}"


# Clearer alias for new code.
BudgetExceededError = NoMoneyException
