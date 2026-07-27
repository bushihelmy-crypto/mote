from __future__ import annotations

import pytest

from mote.contracts.services import ServiceEndpointDescriptor
from mote.runtime.service_gateway import ServiceFailoverGroup, ServiceRuntimeSnapshot, merge_service_runtime_snapshots


def _snapshot(name: str) -> ServiceRuntimeSnapshot:
    endpoint_id = f"endpoint-{name}"
    group_id = f"group-{name}"
    return ServiceRuntimeSnapshot(
        revision=f"revision-{name}",
        endpoints=(
            ServiceEndpointDescriptor(
                endpoint_id=endpoint_id,
                capability=f"capability-{name}",
                transport="https",
                provider=name,
                base_url_identity=f"base-{name}",
                credential_pool_id=f"pool-{name}",
                lifecycle_revision=f"lifecycle-{name}",
            ),
        ),
        groups=(ServiceFailoverGroup(group_id=group_id, endpoint_ids=(endpoint_id,)),),
        route_groups=((f"route-{name}", group_id),),
        credential_slots=((endpoint_id, (f"slot-{name}",)),),
    )


def test_merge_preserves_independent_service_families() -> None:
    merged = merge_service_runtime_snapshots(_snapshot("media"), _snapshot("search"))

    assert [endpoint.endpoint_id for endpoint in merged.endpoints] == [
        "endpoint-media",
        "endpoint-search",
    ]
    assert merged.group_for_route("route-media") is not None
    assert merged.group_for_route("route-search") is not None


def test_merge_rejects_ambiguous_route_ownership() -> None:
    left = _snapshot("same")
    right = ServiceRuntimeSnapshot(
        revision="other",
        endpoints=(),
        groups=(),
        route_groups=left.route_groups,
        credential_slots=(),
    )

    with pytest.raises(ValueError, match="duplicate service route"):
        merge_service_runtime_snapshots(left, right)
