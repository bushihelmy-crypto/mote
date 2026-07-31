"""Immutable Product-to-Runtime endpoint binding without executable behavior."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from mote.contracts.model.failover import EndpointDescriptor


class ResolvedEndpointBinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    endpoint: EndpointDescriptor
    credential_slot_id: str = Field(min_length=1)
    credential_version: str = Field(min_length=1)
    tenant_fingerprint: str = Field(min_length=1)
    classification_policy_id: str = Field(min_length=1)
    transport_identity: str = Field(min_length=1)
    capability_identity: str = Field(min_length=1)
