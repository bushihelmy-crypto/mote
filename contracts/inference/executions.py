from typing import Any, Literal

from pydantic import Field

from mote.contracts.inference.base import FrozenContract
from mote.contracts.inference.deadline import CrossProcessDeadline
from mote.contracts.inference.identity import InferencePrincipal, TrustedSchedulingClass


class BoundExecutionRequest(FrozenContract):
    schema_version: Literal[1] = 1
    execution_id: str = Field(min_length=1)
    owner_journal_id: str = Field(min_length=1)
    generation_id: str = Field(min_length=1)
    generation_artifact_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    endpoint_binding_id: str = Field(min_length=1)
    credential_slot_id: str = Field(min_length=1)
    credential_version: str = Field(min_length=1)
    operation: str = Field(min_length=1)
    payload: dict[str, Any]
    deadline: CrossProcessDeadline
    principal: InferencePrincipal
    scheduling: TrustedSchedulingClass


class SessionApplicationMessage(FrozenContract):
    schema_version: Literal[1] = 1
    session_id: str = Field(min_length=1)
    sequence: int = Field(ge=1)
    message_type: str = Field(min_length=1)
    payload: dict[str, Any]


class TransferPartRequest(BoundExecutionRequest):
    transfer_id: str = Field(min_length=1)
    part_number: int = Field(ge=1)
    offset: int = Field(ge=0)
    length: int = Field(ge=1)
    content_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
