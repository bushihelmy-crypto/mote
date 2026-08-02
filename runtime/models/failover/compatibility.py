"""Request-projection compatibility for immutable endpoint snapshots."""

import hashlib
import json

from mote.contracts.model.failover import EndpointDescriptor
from mote.contracts.model.operations import ModelOperation


def endpoint_projection_shape(
    endpoint: EndpointDescriptor,
    operation: ModelOperation,
) -> tuple[str, bool, bool, bool, bool, bool]:
    """Return fields that must remain invariant across one failover attempt set."""
    capabilities = endpoint.capabilities
    return (
        "native" if capabilities.supports_tools else "xml",
        capabilities.supports_native_schema,
        capabilities.supports_vision,
        capabilities.supports_pdf,
        capabilities.supports_native_tool_search,
        operation in capabilities.supported_operations,
    )


def endpoint_projection_fingerprint(
    endpoint: EndpointDescriptor,
    operation: ModelOperation,
) -> str:
    payload = json.dumps(
        endpoint_projection_shape(endpoint, operation),
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def endpoints_are_projection_compatible(
    endpoints: tuple[EndpointDescriptor, ...],
    operation: ModelOperation,
) -> bool:
    return len({endpoint_projection_shape(item, operation) for item in endpoints}) <= 1


__all__ = [
    "endpoint_projection_fingerprint",
    "endpoint_projection_shape",
    "endpoints_are_projection_compatible",
]
