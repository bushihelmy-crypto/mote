"""Closed governance vocabulary for Product-owned construction."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class OwnerStatus(StrEnum):
    ACTIVE = "active"
    RETIRED = "retired"


class FacadeStatus(StrEnum):
    CANONICAL = "canonical"
    COMPATIBILITY = "compatibility"
    INTERNAL_AGGREGATION = "internal_aggregation"


class CapabilityStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class Enablement(StrEnum):
    REQUIRED = "required"
    CONFIGURED = "configured"
    OPTIONAL_DEPENDENCY = "optional_dependency"


class DeploymentMode(StrEnum):
    EMBEDDED = "embedded"
    SHARED_DAEMON = "shared_daemon"
    OPTIONAL_HOST = "optional_host"


class InstanceScope(StrEnum):
    APPLICATION = "application"
    SESSION = "session"
    PROCESS = "process"
    CONNECTION = "connection"


class CandidateRole(StrEnum):
    GOVERNED_PORT_IMPLEMENTATION = "governed_port_implementation"
    INFRASTRUCTURE_FACTORY = "infrastructure_factory"
    LIFECYCLE_FACTORY = "lifecycle_factory"
    PRODUCTION_ROOT_REFERENCE = "production_root_reference"
    PLUGIN_EXTENSION_DECLARATION = "plugin_extension_declaration"
    EXPLICIT_CAPABILITY = "explicit_capability"


class PublicSymbolRole(StrEnum):
    PRODUCTION_CAPABILITY = "production_capability"
    INTERNAL_FACTORY = "internal_factory"
    EXTERNAL_ADAPTER = "external_adapter"
    TEST_ONLY = "test_only"


@dataclass(frozen=True, slots=True)
class OwnerDeclaration:
    owner_id: str
    path_prefix: str
    status: OwnerStatus = OwnerStatus.ACTIVE
    replacement_owner_id: str = ""

    def __post_init__(self) -> None:
        if not self.owner_id or not self.path_prefix:
            raise ValueError("owner identity and path prefix are required")
        if self.status is OwnerStatus.ACTIVE and self.replacement_owner_id:
            raise ValueError("an active owner cannot have a replacement")
        if self.status is OwnerStatus.RETIRED and not self.replacement_owner_id:
            raise ValueError("a retired owner requires a replacement")


@dataclass(frozen=True, slots=True)
class FacadeDeclaration:
    facade_id: str
    symbol: str
    defining_symbol: str
    owner_id: str
    status: FacadeStatus

    def __post_init__(self) -> None:
        if any(
            not value
            for value in (
                self.facade_id,
                self.symbol,
                self.defining_symbol,
                self.owner_id,
            )
        ):
            raise ValueError("facade declarations must be complete")


@dataclass(frozen=True, slots=True)
class CapabilityDeclaration:
    capability_id: str
    implementation: str
    implementation_owner: str
    applicable_root: str
    enablement: Enablement
    canonical_factory: str
    required_ports: tuple[str, ...]
    deployment_mode: DeploymentMode
    instance_scope: InstanceScope
    lifecycle_owner: str
    start_owner: str
    stop_owner: str
    status: CapabilityStatus = CapabilityStatus.ACTIVE
    replacement_capability_id: str = ""

    def __post_init__(self) -> None:
        identity = (
            self.capability_id,
            self.implementation,
            self.implementation_owner,
            self.applicable_root,
            self.canonical_factory,
            self.lifecycle_owner,
            self.start_owner,
            self.stop_owner,
        )
        if any(not value for value in identity):
            raise ValueError("capability declarations must be complete")
        if len(set(self.required_ports)) != len(self.required_ports):
            raise ValueError("required ports must be unique")
        if self.status is CapabilityStatus.ACTIVE and self.replacement_capability_id:
            raise ValueError("an active capability cannot have a replacement")
        if self.status is CapabilityStatus.DISABLED and self.replacement_capability_id:
            raise ValueError("disabled capabilities are not migration aliases")

    @property
    def recipe_key(self) -> tuple[str, str, DeploymentMode, InstanceScope]:
        return (
            self.capability_id,
            self.applicable_root,
            self.deployment_mode,
            self.instance_scope,
        )


@dataclass(frozen=True, slots=True)
class CandidateClassification:
    candidate_id: str
    implementation: str
    role: CandidateRole
    source_symbol: str

    def __post_init__(self) -> None:
        if any(not value for value in (self.candidate_id, self.implementation, self.source_symbol)):
            raise ValueError("candidate classifications must be complete")


@dataclass(frozen=True, slots=True)
class PublicSymbolClassification:
    symbol: str
    role: PublicSymbolRole
    owner_id: str
    evidence: str

    def __post_init__(self) -> None:
        if any(not value for value in (self.symbol, self.owner_id, self.evidence)):
            raise ValueError("public symbol classifications must be complete")


__all__ = [
    "CandidateClassification",
    "CandidateRole",
    "CapabilityDeclaration",
    "CapabilityStatus",
    "DeploymentMode",
    "Enablement",
    "FacadeDeclaration",
    "FacadeStatus",
    "InstanceScope",
    "OwnerDeclaration",
    "OwnerStatus",
    "PublicSymbolClassification",
    "PublicSymbolRole",
]
