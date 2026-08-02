"""Strict typed executable-generation artifact contract."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from mote.contracts.inference.base import FrozenContract


class DeploymentKind(StrEnum):
    EMBEDDED = "embedded"
    SHARED_PROCESS = "shared_process"


class RuntimeBindingKind(StrEnum):
    EMBEDDED = "embedded"
    SHARED_RPC = "shared_rpc"
    UNAVAILABLE = "unavailable"


class VersionBinding(FrozenContract):
    identity: str = Field(min_length=1)
    revision: str = Field(min_length=1)


class ModelGenerationBinding(FrozenContract):
    schema_version: Literal[1] = 1
    variant: Literal["model"] = "model"
    topology_revision: str = Field(min_length=1)


class ServiceGenerationBinding(FrozenContract):
    schema_version: Literal[1] = 1
    variant: Literal["service"] = "service"
    runtime: RuntimeBindingKind
    configured: bool


class SessionGenerationBinding(FrozenContract):
    schema_version: Literal[1] = 1
    variant: Literal["session"] = "session"
    runtime: RuntimeBindingKind
    configured: bool


class TransferGenerationBinding(FrozenContract):
    schema_version: Literal[1] = 1
    variant: Literal["transfer"] = "transfer"
    runtime: RuntimeBindingKind
    configured: bool


class CapabilityPricingSnapshot(FrozenContract):
    schema_version: Literal[1] = 1
    catalog_revision: str = Field(min_length=1)
    pricing_revision: str = Field(min_length=1)


class GenerationActivationPolicy(FrozenContract):
    schema_version: Literal[1] = 1
    deployment: DeploymentKind
    activate_immediately: bool


class GenerationArtifact(FrozenContract):
    schema_version: Literal[2] = 2
    generation_id: str = Field(min_length=1)
    parent_generation_id: str | None = None
    model_binding: ModelGenerationBinding
    service_binding: ServiceGenerationBinding
    session_binding: SessionGenerationBinding
    transfer_binding: TransferGenerationBinding
    credential_versions: tuple[VersionBinding, ...]
    transport_registry_revision: str = Field(min_length=1)
    client_profile_revision: str = Field(min_length=1)
    failure_policy_revision: str = Field(min_length=1)
    capability_pricing: CapabilityPricingSnapshot
    governance_plugins: tuple[VersionBinding, ...]
    required_wire_contract_range: tuple[int, int]
    activation_policy: GenerationActivationPolicy
    min_reader_version: int = Field(ge=1)
    min_writer_version: int = Field(ge=1)
    persistence_schemas: tuple[VersionBinding, ...]
    migration_set_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    artifact_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _unique_bindings(self) -> "GenerationArtifact":
        for label, values in (
            ("credential", self.credential_versions),
            ("governance plugin", self.governance_plugins),
            ("persistence schema", self.persistence_schemas),
        ):
            identities = tuple(item.identity for item in values)
            if len(identities) != len(set(identities)):
                raise ValueError(f"duplicate {label} identity")
        lower, upper = self.required_wire_contract_range
        if type(lower) is not int or type(upper) is not int or lower < 1 or upper < lower:
            raise ValueError("wire contract range is invalid")
        if self.min_reader_version > lower or self.min_writer_version > lower:
            raise ValueError("minimum reader/writer versions must support the full required wire range")
        return self


def compute_generation_artifact_digest(artifact: GenerationArtifact) -> str:
    payload = artifact.model_dump(mode="json", exclude={"artifact_digest"})
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def verify_generation_artifact_digest(artifact: GenerationArtifact) -> None:
    if artifact.artifact_digest != compute_generation_artifact_digest(artifact):
        raise ValueError("GenerationArtifact content digest mismatch")


__all__ = [
    "CapabilityPricingSnapshot",
    "DeploymentKind",
    "GenerationActivationPolicy",
    "GenerationArtifact",
    "ModelGenerationBinding",
    "RuntimeBindingKind",
    "ServiceGenerationBinding",
    "SessionGenerationBinding",
    "TransferGenerationBinding",
    "VersionBinding",
    "compute_generation_artifact_digest",
    "verify_generation_artifact_digest",
]
