"""Canonical, secret-free model topology contracts."""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mote.contracts.model.execution_policy import EndpointExecutionPolicy
from mote.contracts.model.operations import ModelOperation


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)


class DefaultRoute(FrozenModel):
    kind: Literal["default"] = "default"


class TaskRoute(FrozenModel):
    kind: Literal["task"] = "task"
    name: str = Field(min_length=1, pattern=r"^[^:\s]+$")


class SemanticRoute(FrozenModel):
    kind: Literal["semantic"] = "semantic"
    name: str = Field(min_length=1, pattern=r"^[^:\s]+$")


RouteId = Annotated[
    Union[DefaultRoute, TaskRoute, SemanticRoute],
    Field(discriminator="kind"),
]


class EndpointCapabilityDeclaration(FrozenModel):
    supported_operations: frozenset[ModelOperation] = Field(
        default_factory=lambda: frozenset({ModelOperation.GENERATE}),
        min_length=1,
    )
    supports_tools: bool
    supports_native_schema: bool
    supports_server_web_search: bool
    supports_vision: bool
    supports_pdf: bool
    supports_native_tool_search: bool
    context_tokens: int = Field(ge=0)


class ModelEndpointTopology(FrozenModel):
    endpoint_id: str = Field(min_length=1)
    transport: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    capabilities: EndpointCapabilityDeclaration
    governance_domain: str = Field(min_length=1)
    region: str = Field(min_length=1)
    pricing_class: str = Field(min_length=1)
    credential_slots: tuple[str, ...] = Field(min_length=1)
    execution_policy: EndpointExecutionPolicy = Field(default_factory=EndpointExecutionPolicy)

    @field_validator("credential_slots")
    @classmethod
    def _unique_slots(cls, slots: tuple[str, ...]) -> tuple[str, ...]:
        if any(not slot for slot in slots) or len(slots) != len(set(slots)):
            raise ValueError("credential slot ids must be non-empty and unique")
        return slots


class RecoveryProfileTopology(FrozenModel):
    profile_id: str = Field(min_length=1)
    max_wire_attempts: int = Field(ge=1)
    max_attempts_per_endpoint: int = Field(ge=1)
    max_endpoint_switches: int = Field(ge=0)
    max_credential_rotations: int = Field(ge=0)
    max_request_transforms: int = Field(ge=0)
    total_deadline_ms: int = Field(gt=0)
    single_attempt_timeout_ms: int = Field(gt=0)
    max_backoff_ms: int = Field(ge=0)


class FailoverGroupTopology(FrozenModel):
    group_id: str = Field(min_length=1)
    endpoint_ids: tuple[str, ...] = Field(min_length=1)
    recovery_profile_id: str = Field(min_length=1)

    @field_validator("endpoint_ids")
    @classmethod
    def _unique_endpoints(cls, endpoint_ids: tuple[str, ...]) -> tuple[str, ...]:
        if len(endpoint_ids) != len(set(endpoint_ids)):
            raise ValueError("failover endpoint ids must be unique")
        return endpoint_ids


class RouteBinding(FrozenModel):
    route_id: RouteId
    group_id: str = Field(min_length=1)


class ModelTopology(FrozenModel):
    schema_version: Literal["mote.model-topology/v2"] = Field(
        default="mote.model-topology/v2",
        alias="schema",
    )
    endpoints: tuple[ModelEndpointTopology, ...] = Field(min_length=1)
    recovery_profiles: tuple[RecoveryProfileTopology, ...] = Field(min_length=1)
    failover_groups: tuple[FailoverGroupTopology, ...] = Field(min_length=1)
    routes: tuple[RouteBinding, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _references_are_closed(self) -> "ModelTopology":
        endpoints = {value.endpoint_id for value in self.endpoints}
        profiles = {value.profile_id for value in self.recovery_profiles}
        groups = {value.group_id for value in self.failover_groups}
        if len(endpoints) != len(self.endpoints):
            raise ValueError("endpoint ids must be unique")
        if len(profiles) != len(self.recovery_profiles):
            raise ValueError("recovery profile ids must be unique")
        if len(groups) != len(self.failover_groups):
            raise ValueError("failover group ids must be unique")
        if len({value.route_id for value in self.routes}) != len(self.routes):
            raise ValueError("route ids must be unique")
        for group in self.failover_groups:
            if not set(group.endpoint_ids) <= endpoints:
                raise ValueError(f"group {group.group_id!r} references unknown endpoint")
            if group.recovery_profile_id not in profiles:
                raise ValueError(f"group {group.group_id!r} references unknown profile")
        if any(route.group_id not in groups for route in self.routes):
            raise ValueError("route references unknown failover group")
        if not any(isinstance(route.route_id, DefaultRoute) for route in self.routes):
            raise ValueError("topology requires a default route")
        return self


__all__ = [
    "DefaultRoute",
    "EndpointCapabilityDeclaration",
    "FailoverGroupTopology",
    "ModelEndpointTopology",
    "ModelTopology",
    "RecoveryProfileTopology",
    "RouteBinding",
    "RouteId",
    "SemanticRoute",
    "TaskRoute",
]
