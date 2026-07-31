"""Authenticated provider webhook evidence contract."""

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from mote.contracts.inference.base import FrozenContract


class ProviderEvidenceConflictError(RuntimeError):
    """Authenticated evidence conflicts with durable execution identity."""


class ProviderEvidence(FrozenContract):
    schema_version: Literal[1] = 1
    provider: str = Field(min_length=1, max_length=128)
    event_id: str = Field(min_length=1, max_length=256)
    execution_id: str = Field(min_length=1, max_length=256)
    event_type: str = Field(min_length=1, max_length=256)
    occurred_at: datetime
    provider_resource_id: str | None = Field(default=None, max_length=512)

    @model_validator(mode="after")
    def _timestamp_is_aware(self) -> "ProviderEvidence":
        if self.occurred_at.utcoffset() is None:
            raise ValueError("provider evidence timestamp must be timezone-aware")
        return self


class ProviderEvidenceQuery(FrozenContract):
    schema_version: Literal[1] = 1
    provider: str = Field(min_length=1, max_length=128)
    execution_id: str = Field(min_length=1, max_length=256)
    generation_id: str = Field(min_length=1, max_length=256)
    provider_resource_id: str | None = Field(default=None, max_length=512)
    attempt: int = Field(ge=1, le=100)
    deadline: datetime

    @model_validator(mode="after")
    def _deadline_is_aware(self) -> "ProviderEvidenceQuery":
        if self.deadline.utcoffset() is None:
            raise ValueError("provider evidence query deadline must be timezone-aware")
        return self


__all__ = [
    "ProviderEvidence",
    "ProviderEvidenceConflictError",
    "ProviderEvidenceQuery",
]
