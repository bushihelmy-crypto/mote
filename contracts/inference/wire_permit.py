from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from mote.contracts.inference.base import FrozenContract


class ExecutionTaxonomy(StrEnum):
    UNARY_FINITE_ATTEMPT = "unary_finite_attempt"
    DURABLE_OPERATION = "durable_operation"
    LONG_LIVED_SESSION = "long_lived_session"
    ARTIFACT_TRANSFER = "artifact_transfer"


class WirePermit(FrozenContract):
    schema_version: Literal[1] = 1
    encoding_version: Literal["rfc8785-json-v1"] = "rfc8785-json-v1"
    signature_algorithm: Literal["Ed25519"] = "Ed25519"
    domain_separation: Literal["mote.gateway.wire-permit.v1"] = "mote.gateway.wire-permit.v1"
    attempt_id: str = Field(min_length=1)
    execution_taxonomy: ExecutionTaxonomy
    owner_journal_id: str = Field(min_length=1)
    wire_unit: str = Field(min_length=1)
    generation_id: str = Field(min_length=1)
    generation_artifact_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    ordinal: int = Field(ge=1)
    nonce: str = Field(min_length=16)
    issued_journal_revision: int = Field(ge=1)
    not_before: datetime
    expires_at: datetime
    issuer_key_id: str = Field(min_length=1)
    audience: str = Field(min_length=1)
    trust_revision: int = Field(ge=1)
    backup_epoch: int = Field(ge=0)
    admission_epoch: int = Field(ge=0)
    signature: str = Field(min_length=1)

    @model_validator(mode="after")
    def _validity_window_is_aware_and_positive(self) -> "WirePermit":
        if self.not_before.utcoffset() is None or self.expires_at.utcoffset() is None:
            raise ValueError("permit validity timestamps must be timezone-aware")
        if self.not_before >= self.expires_at:
            raise ValueError("permit expires_at must follow not_before")
        return self
