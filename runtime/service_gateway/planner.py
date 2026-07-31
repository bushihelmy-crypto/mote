"""Pure planning for externally hosted Tool service invocations."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import datetime, timezone

from mote.contracts.service import ServiceInvocation, ServicePlan
from mote.contracts.service.errors import ServiceRouteUnavailableError
from mote.runtime.service_gateway.snapshot import ServiceFailoverGroup, ServiceRuntimeSnapshot


class ServiceFailoverPlanner:
    def __init__(
        self,
        snapshot: ServiceRuntimeSnapshot,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._snapshot = snapshot
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    @property
    def snapshot(self) -> ServiceRuntimeSnapshot:
        return self._snapshot

    def plan(self, invocation: ServiceInvocation) -> ServicePlan:
        group = self._resolve_group(invocation.route_id)
        candidates = tuple(
            endpoint
            for endpoint_id in group.endpoint_ids
            if (endpoint := self._snapshot.endpoint(endpoint_id)) is not None
        )
        if len(candidates) != len(group.endpoint_ids):
            missing = tuple(
                endpoint_id for endpoint_id in group.endpoint_ids if self._snapshot.endpoint(endpoint_id) is None
            )
            raise ServiceRouteUnavailableError(
                f"service route {invocation.route_id!r} has unavailable endpoints",
                route_id=invocation.route_id,
                missing_endpoints=missing,
                config_revision=self._snapshot.revision,
            )
        eligible = tuple(
            endpoint
            for endpoint in candidates
            if endpoint.capability == invocation.capability
            and endpoint.governance_domain == invocation.governance_domain
            and (not invocation.allowed_regions or endpoint.region in invocation.allowed_regions)
        )
        if not eligible:
            raise ServiceRouteUnavailableError(
                f"service route {invocation.route_id!r} cannot satisfy capability",
                route_id=invocation.route_id,
                capability=invocation.capability,
                governance_domain=invocation.governance_domain,
                allowed_regions=sorted(invocation.allowed_regions),
                config_revision=self._snapshot.revision,
            )
        plan_id = hashlib.sha256(
            (
                f"{invocation.service_call_id}\0{self._snapshot.revision}\0"
                f"{group.group_id}\0{','.join(item.endpoint_id for item in eligible)}"
            ).encode("utf-8")
        ).hexdigest()[:32]
        return ServicePlan(
            plan_id=plan_id,
            service_call_id=invocation.service_call_id,
            config_revision=self._snapshot.revision,
            policy_id=group.policy_id,
            endpoints=eligible,
            budget=group.budget,
            created_at=self._clock(),
        )

    def _resolve_group(self, route_id: str) -> ServiceFailoverGroup:
        group = self._snapshot.group(route_id)
        if group is None:
            group = self._snapshot.group_for_route(route_id)
        if group is None:
            raise ServiceRouteUnavailableError(
                f"unknown service route {route_id!r}",
                route_id=route_id,
                available_routes=sorted(route for route, _group in self._snapshot.route_groups),
                available_groups=sorted(group.group_id for group in self._snapshot.groups),
                config_revision=self._snapshot.revision,
            )
        return group


__all__ = ["ServiceFailoverPlanner"]
