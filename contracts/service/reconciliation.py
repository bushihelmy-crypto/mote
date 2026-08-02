"""Immutable projection used by the hosted-service reconciliation owner."""

from pydantic import BaseModel, ConfigDict, Field

from mote.contracts.service.models import ServiceInvocation


class PendingServiceCall(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    invocation: ServiceInvocation
    stream_revision: int = Field(ge=1)
    cursor: str = Field(min_length=1)


__all__ = ["PendingServiceCall"]
