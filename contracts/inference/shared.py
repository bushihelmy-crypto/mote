from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from mote.contracts.inference.base import FrozenContract
from mote.contracts.inference.identity import InferencePrincipal


class CallerIncarnation(FrozenContract):
    pid: int = Field(ge=1)
    process_start_ticks: int = Field(ge=1)
    boot_id: str = Field(min_length=1)


class SharedHandshake(FrozenContract):
    schema_version: Literal[1] = 1
    protocol_versions: tuple[int, ...] = Field(min_length=1)
    application_id: str = Field(min_length=1)
    caller: CallerIncarnation
    socket_generation: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    subject_id: str = Field(min_length=1)
    policy_revision: str = Field(min_length=1)
    delegation_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    nonce: str = Field(min_length=16)
    issued_at: datetime
    expires_at: datetime
    key_id: str = Field(min_length=1)
    signature: str = Field(min_length=1)

    @model_validator(mode="after")
    def _valid_window(self) -> "SharedHandshake":
        if self.issued_at.utcoffset() is None or self.expires_at.utcoffset() is None:
            raise ValueError("handshake timestamps must be timezone-aware")
        if self.issued_at >= self.expires_at:
            raise ValueError("handshake validity window is empty")
        return self


class SharedSessionCredential(FrozenContract):
    schema_version: Literal[1] = 1
    session_id: str = Field(min_length=1)
    protocol_version: int = Field(ge=1)
    socket_generation: str = Field(min_length=1)
    application_id: str = Field(min_length=1, max_length=256)
    caller: CallerIncarnation
    principal: InferencePrincipal
    issued_at: datetime
    expires_at: datetime
    key_id: str = Field(min_length=1)
    permit_issuer_key_id: str = Field(min_length=1)
    permit_trust_revision: int = Field(ge=1)
    permit_private_key: str = Field(min_length=40, repr=False)
    signature: str = Field(min_length=1)


class ProtocolNegotiation(FrozenContract):
    schema_version: Literal[1] = 1
    supported_versions: tuple[int, ...] = Field(min_length=1)
    capabilities: tuple[str, ...] = ()


class ProtocolNegotiationResult(FrozenContract):
    schema_version: Literal[1] = 1
    protocol_version: int = Field(ge=1)
    capabilities: tuple[str, ...] = ()
    socket_generation: str = Field(min_length=1)
