"""Structural Runtime index built from canonical model topology."""

from __future__ import annotations

from dataclasses import dataclass

from mote.contracts.model.failover import AttemptBudget, EndpointCapabilities, EndpointDescriptor
from mote.contracts.model.topology import ModelTopology, RouteId
from mote.contracts.model.topology_codec import topology_revision


@dataclass(frozen=True)
class RuntimeFailoverGroup:
    group_id: str
    endpoint_ids: tuple[str, ...]
    policy_id: str
    budget: AttemptBudget


@dataclass(frozen=True)
class CanonicalModelRuntimeSnapshot:
    revision: str
    endpoints: tuple[EndpointDescriptor, ...]
    groups: tuple[RuntimeFailoverGroup, ...]
    route_groups: tuple[tuple[RouteId, str], ...]
    credential_slots: tuple[tuple[str, tuple[str, ...]], ...]

    def endpoint(self, endpoint_id: str) -> EndpointDescriptor | None:
        return next((item for item in self.endpoints if item.endpoint_id == endpoint_id), None)

    def group(self, group_id: str) -> RuntimeFailoverGroup | None:
        return next((item for item in self.groups if item.group_id == group_id), None)

    def group_for_route(self, route_id: RouteId) -> RuntimeFailoverGroup | None:
        group_id = next((group for route, group in self.route_groups if route == route_id), None)
        return self.group(group_id) if group_id is not None else None

    def slots_for_endpoint(self, endpoint_id: str) -> tuple[str, ...]:
        return next(
            (slots for current, slots in self.credential_slots if current == endpoint_id),
            (),
        )


def build_canonical_model_runtime_snapshot(
    topology: ModelTopology,
) -> CanonicalModelRuntimeSnapshot:
    revision = topology_revision(topology)
    profiles = {profile.profile_id: profile for profile in topology.recovery_profiles}
    endpoints = tuple(
        EndpointDescriptor(
            endpoint_id=endpoint.endpoint_id,
            transport=endpoint.transport,
            provider=endpoint.provider,
            model=endpoint.model,
            base_url_identity=endpoint.base_url,
            capabilities=EndpointCapabilities(
                **endpoint.capabilities.model_dump(exclude={"context_tokens"}),
                context_tokens=endpoint.capabilities.context_tokens,
            ),
            governance_domain=endpoint.governance_domain,
            region=endpoint.region,
            pricing_class=endpoint.pricing_class,
            credential_pool_id=f"slots:{endpoint.endpoint_id}",
            lifecycle_revision=revision,
            execution_policy=endpoint.execution_policy,
        )
        for endpoint in topology.endpoints
    )
    groups = tuple(
        RuntimeFailoverGroup(
            group_id=group.group_id,
            endpoint_ids=group.endpoint_ids,
            policy_id="canonical-v1",
            budget=AttemptBudget(
                max_wire_attempts=profiles[group.recovery_profile_id].max_wire_attempts,
                max_attempts_per_endpoint=profiles[group.recovery_profile_id].max_attempts_per_endpoint,
                max_endpoint_switches=profiles[group.recovery_profile_id].max_endpoint_switches,
                max_credential_rotations=profiles[group.recovery_profile_id].max_credential_rotations,
                max_request_transforms=profiles[group.recovery_profile_id].max_request_transforms,
                total_deadline_seconds=profiles[group.recovery_profile_id].total_deadline_ms / 1000,
                single_attempt_timeout_seconds=profiles[group.recovery_profile_id].single_attempt_timeout_ms / 1000,
                max_backoff_seconds=profiles[group.recovery_profile_id].max_backoff_ms / 1000,
            ),
        )
        for group in topology.failover_groups
    )
    return CanonicalModelRuntimeSnapshot(
        revision=revision,
        endpoints=endpoints,
        groups=groups,
        route_groups=tuple((binding.route_id, binding.group_id) for binding in topology.routes),
        credential_slots=tuple((endpoint.endpoint_id, endpoint.credential_slots) for endpoint in topology.endpoints),
    )


__all__ = [
    "CanonicalModelRuntimeSnapshot",
    "RuntimeFailoverGroup",
    "build_canonical_model_runtime_snapshot",
]
