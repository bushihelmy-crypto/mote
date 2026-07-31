from typing import Literal

from pydantic import Field

from mote.contracts.inference.base import FrozenContract


class InferencePrincipal(FrozenContract):
    schema_version: Literal[1] = 1
    tenant_id: str = Field(min_length=1, max_length=256)
    project_id: str = Field(min_length=1, max_length=256)
    subject_id: str = Field(min_length=1, max_length=256)
    policy_revision: str = Field(min_length=1, max_length=256)
    delegation_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class TrustedSchedulingClass(FrozenContract):
    schema_version: Literal[1] = 1
    tenant_weight: int = Field(default=1, ge=1, le=1000)
    project_weight: int = Field(default=1, ge=1, le=1000)
    priority: int = Field(default=0, ge=-100, le=100)
    cost_units: int = Field(default=1, ge=1)
