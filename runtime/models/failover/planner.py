"""Pure immutable planning for one provider-neutral model invocation."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import datetime, timezone

from mote.contracts.model.errors import (
    ModelCapabilityUnsatisfiedError,
    ModelGovernanceViolationError,
    ModelRouteUnavailableError,
)
from mote.contracts.model.failover import EndpointDescriptor, FailoverPlan
from mote.contracts.model.invocation import ModelInvocation, ModelOperation, RequestRequirements, ResponseMode
from mote.contracts.model.topology import RouteId
from mote.contracts.model.topology_codec import encode_route_id
from mote.runtime.models.failover.snapshot import CanonicalModelRuntimeSnapshot, RuntimeFailoverGroup


class FailoverPlanner:
    """Compile an invocation and one snapshot into a closed immutable plan."""

    def __init__(
        self,
        snapshot: CanonicalModelRuntimeSnapshot,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._snapshot = snapshot
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    @property
    def snapshot(self) -> CanonicalModelRuntimeSnapshot:
        return self._snapshot

    def plan(self, invocation: ModelInvocation) -> FailoverPlan:
        group = self._resolve_group(invocation.route_id)
        candidates = tuple(
            endpoint
            for endpoint_id in group.endpoint_ids
            if (endpoint := self._snapshot.endpoint(endpoint_id)) is not None
        )
        if len(candidates) != len(group.endpoint_ids):
            missing = [
                endpoint_id for endpoint_id in group.endpoint_ids if self._snapshot.endpoint(endpoint_id) is None
            ]
            raise ModelRouteUnavailableError(
                f"route {invocation.route_id!r} contains unavailable endpoints",
                route_id=encode_route_id(invocation.route_id),
                group_id=group.group_id,
                missing_endpoints=missing,
                config_revision=self._snapshot.revision,
            )

        governed = tuple(
            endpoint for endpoint in candidates if self._governance_allows(endpoint, invocation.requirements)
        )
        if not governed:
            raise ModelGovernanceViolationError(
                f"no endpoint on route {invocation.route_id!r} satisfies governance",
                route_id=encode_route_id(invocation.route_id),
                group_id=group.group_id,
                required_domain=invocation.requirements.governance_domain,
                allowed_regions=sorted(invocation.requirements.allowed_regions),
                candidates=[endpoint.endpoint_id for endpoint in candidates],
                config_revision=self._snapshot.revision,
            )

        missing_by_endpoint = {
            endpoint.endpoint_id: self._missing_capabilities(
                endpoint,
                invocation,
            )
            for endpoint in governed
        }
        eligible = tuple(endpoint for endpoint in governed if not missing_by_endpoint[endpoint.endpoint_id])
        if not eligible:
            raise ModelCapabilityUnsatisfiedError(
                f"no endpoint on route {invocation.route_id!r} satisfies requirements",
                route_id=encode_route_id(invocation.route_id),
                group_id=group.group_id,
                missing_by_endpoint=missing_by_endpoint,
                config_revision=self._snapshot.revision,
            )

        plan_id = hashlib.sha256(
            (
                f"{invocation.model_call_id}\0{self._snapshot.revision}\0"
                f"{group.group_id}\0{','.join(e.endpoint_id for e in eligible)}"
            ).encode("utf-8")
        ).hexdigest()[:32]
        return FailoverPlan(
            plan_id=plan_id,
            model_call_id=invocation.model_call_id,
            config_revision=self._snapshot.revision,
            policy_id=group.policy_id,
            endpoints=eligible,
            requirements=invocation.requirements,
            budget=group.budget,
            created_at=self._clock(),
        )

    def _resolve_group(self, route_id: RouteId) -> RuntimeFailoverGroup:
        group = self._snapshot.group_for_route(route_id)
        if group is None:
            raise ModelRouteUnavailableError(
                f"unknown model route {route_id!r}",
                route_id=encode_route_id(route_id),
                available_routes=sorted(
                    route if isinstance(route, str) else encode_route_id(route)
                    for route, _group in self._snapshot.route_groups
                ),
                available_groups=sorted(group.group_id for group in self._snapshot.groups),
                config_revision=self._snapshot.revision,
            )
        return group

    @staticmethod
    def _governance_allows(
        endpoint: EndpointDescriptor,
        requirements: RequestRequirements,
    ) -> bool:
        if endpoint.governance_domain != requirements.governance_domain:
            return False
        return not requirements.allowed_regions or endpoint.region in requirements.allowed_regions

    @staticmethod
    def _missing_capabilities(
        endpoint: EndpointDescriptor,
        invocation: ModelInvocation,
    ) -> list[str]:
        requirements = invocation.requirements
        capabilities = endpoint.capabilities
        missing: list[str] = []
        if (
            requirements.needs_tools or requirements.response_mode is ResponseMode.NATIVE_TOOLS
        ) and not capabilities.supports_tools:
            missing.append("tools")
        if (
            requirements.needs_native_schema or requirements.response_mode is ResponseMode.NATIVE_SCHEMA
        ) and not capabilities.supports_native_schema:
            missing.append("native_schema")
        if (
            requirements.needs_server_web_search or invocation.operation is ModelOperation.WEB_SEARCH
        ) and not capabilities.supports_server_web_search:
            missing.append("server_web_search")
        if (
            requirements.needs_vision or invocation.operation is ModelOperation.IMAGE_DESCRIPTION
        ) and not capabilities.supports_vision:
            missing.append("vision")
        if requirements.needs_pdf and not capabilities.supports_pdf:
            missing.append("pdf")
        if requirements.needs_native_tool_search and not capabilities.supports_native_tool_search:
            missing.append("native_tool_search")
        if capabilities.context_tokens < requirements.min_context_tokens:
            missing.append("context_tokens")
        return missing


__all__ = ["FailoverPlanner"]
