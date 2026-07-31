from typing import Any, Literal

from pydantic import Field

from mote.contracts.inference.base import FrozenContract
from mote.contracts.inference.deadline import CrossProcessDeadline
from mote.contracts.inference.identity import InferencePrincipal, TrustedSchedulingClass
from mote.contracts.model.failover import EndpointDescriptor


class InferenceAttemptRequest(FrozenContract):
    schema_version: Literal[1] = 1
    model_call_id: str = Field(min_length=1)
    owner_journal_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    generation_id: str = Field(min_length=1)
    generation_artifact_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    endpoint: EndpointDescriptor
    credential_slot_id: str = Field(min_length=1)
    credential_version: str = Field(min_length=1)
    invocation: dict[str, Any]
    deadline: CrossProcessDeadline
    stream: bool
    artifact_reference: str | None = None
    principal: InferencePrincipal
    scheduling: TrustedSchedulingClass
