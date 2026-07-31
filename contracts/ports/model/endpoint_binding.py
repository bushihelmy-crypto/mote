from typing import Protocol

from mote.contracts.model.endpoint_binding import ResolvedEndpointBinding
from mote.contracts.model.failover import EndpointDescriptor


class ModelEndpointBindingResolver(Protocol):
    def resolve(
        self,
        endpoint: EndpointDescriptor,
        credential_slot_id: str,
    ) -> ResolvedEndpointBinding | None:
        ...
