"""Side-effect-free resolution of compiled model endpoint bindings."""

from __future__ import annotations

import hashlib
import json

from mote.contracts.model.endpoint_binding import ResolvedEndpointBinding
from mote.contracts.model.failover import EndpointDescriptor
from mote.product.models.compiler import CredentialBindingSpec


class ProductModelBindingResolver:
    def __init__(self, bindings: CredentialBindingSpec) -> None:
        self._handles = dict(bindings.handles)

    def resolve(
        self,
        endpoint: EndpointDescriptor,
        credential_slot_id: str,
    ) -> ResolvedEndpointBinding | None:
        handle = self._handles.get(credential_slot_id)
        if handle is None:
            return None
        public = {
            "endpoint_id": endpoint.endpoint_id,
            "provider": endpoint.provider,
            "transport": endpoint.transport,
            "model": endpoint.model,
            "lifecycle_revision": endpoint.lifecycle_revision,
            "credential_slot_id": credential_slot_id,
            "credential_identity": handle.identity.value,
            "credential_epoch": handle.epoch.value,
        }
        identity = hashlib.sha256(json.dumps(public, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        capability = hashlib.sha256(
            json.dumps(
                endpoint.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        tenant = hashlib.sha256(
            f"mote-model-tenant-v2\0{endpoint.endpoint_id}\0{credential_slot_id}".encode()
        ).hexdigest()[:24]
        return ResolvedEndpointBinding(
            endpoint=endpoint,
            credential_slot_id=credential_slot_id,
            credential_version=handle.epoch.value,
            tenant_fingerprint=tenant,
            classification_policy_id=f"failure-v2:{endpoint.transport}",
            transport_identity=f"sha256:{identity}",
            capability_identity=f"sha256:{capability}",
        )
