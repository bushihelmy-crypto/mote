"""Immutable, secret-free routing snapshot for hosted Tool services."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass, field

from mote.contracts.model.failover import AttemptBudget
from mote.contracts.service import ServiceEndpointDescriptor


@dataclass(frozen=True)
class ServiceFailoverGroup:
    group_id: str
    endpoint_ids: tuple[str, ...]
    policy_id: str = "default-v1"
    budget: AttemptBudget = field(default_factory=AttemptBudget)


@dataclass(frozen=True)
class ServiceRuntimeSnapshot:
    revision: str
    endpoints: tuple[ServiceEndpointDescriptor, ...]
    groups: tuple[ServiceFailoverGroup, ...]
    route_groups: tuple[tuple[str, str], ...]
    credential_slots: tuple[tuple[str, tuple[str, ...]], ...]

    def endpoint(self, endpoint_id: str) -> ServiceEndpointDescriptor | None:
        return next(
            (endpoint for endpoint in self.endpoints if endpoint.endpoint_id == endpoint_id),
            None,
        )

    def group(self, group_id: str) -> ServiceFailoverGroup | None:
        return next(
            (group for group in self.groups if group.group_id == group_id),
            None,
        )

    def group_for_route(self, route_id: str) -> ServiceFailoverGroup | None:
        group_id = next(
            (group for route, group in self.route_groups if route == route_id),
            None,
        )
        return self.group(group_id) if group_id is not None else None

    def slots_for_endpoint(self, endpoint_id: str) -> tuple[str, ...]:
        return next(
            (slots for configured_endpoint, slots in self.credential_slots if configured_endpoint == endpoint_id),
            (),
        )


def merge_service_runtime_snapshots(
    *snapshots: ServiceRuntimeSnapshot,
) -> ServiceRuntimeSnapshot:
    """Merge independently compiled service families into one planner view."""

    endpoints = tuple(endpoint for snapshot in snapshots for endpoint in snapshot.endpoints)
    groups = tuple(group for snapshot in snapshots for group in snapshot.groups)
    route_groups = tuple(route_group for snapshot in snapshots for route_group in snapshot.route_groups)
    credential_slots = tuple(slots for snapshot in snapshots for slots in snapshot.credential_slots)
    _require_unique("service endpoint", (item.endpoint_id for item in endpoints))
    _require_unique("service failover group", (item.group_id for item in groups))
    _require_unique("service route", (route for route, _group in route_groups))
    _require_unique(
        "service credential binding",
        (endpoint_id for endpoint_id, _slots in credential_slots),
    )
    revision_source = "\0".join(snapshot.revision for snapshot in snapshots)
    return ServiceRuntimeSnapshot(
        revision=hashlib.sha256((revision_source or "service-empty").encode("utf-8")).hexdigest()[:24],
        endpoints=endpoints,
        groups=groups,
        route_groups=route_groups,
        credential_slots=credential_slots,
    )


def _require_unique(label: str, values: Iterable[str]) -> None:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    if duplicates:
        raise ValueError(f"duplicate {label} ids: {sorted(duplicates)!r}")


__all__ = [
    "ServiceFailoverGroup",
    "ServiceRuntimeSnapshot",
    "merge_service_runtime_snapshots",
]
