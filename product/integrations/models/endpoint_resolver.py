"""Product composition of endpoint and opaque credential-slot bindings."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Mapping

from mote.contracts.config.llm import LLMConfig
from mote.contracts.config.models import ModelsConfig
from mote.contracts.models.failover import EndpointDescriptor
from mote.contracts.ports.model_endpoint import ModelEndpointAdapter
from mote.product.integrations.models.endpoint_adapter import ProductModelEndpointAdapter
from mote.runtime.models.clients.registry import LLMProviderRegistry
from mote.runtime.models.failover.configuration import direct_credential_slot_ids, resolve_endpoint_config


@dataclass(frozen=True)
class _Binding:
    config: LLMConfig
    tenant_fingerprint: str
    force_oauth_refresh: bool = False


class ProductModelEndpointResolver:
    """Resolve a fresh single-credential adapter for every logical model call."""

    def __init__(
        self,
        models: ModelsConfig,
        providers: LLMProviderRegistry,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._providers = providers
        self._bindings = _bindings(models, environ=environ)

    def resolve(
        self,
        endpoint: EndpointDescriptor,
        credential_slot_id: str,
    ) -> ModelEndpointAdapter | None:
        binding = self._bindings.get((endpoint.endpoint_id, credential_slot_id))
        if binding is None:
            return None
        llm = self._providers.create(binding.config)
        prepare_credential = None
        if binding.force_oauth_refresh:
            prepare_credential = getattr(llm, "refresh_oauth_credential", None)
        return ProductModelEndpointAdapter(
            endpoint_id=endpoint.endpoint_id,
            credential_slot_id=credential_slot_id,
            tenant_fingerprint=binding.tenant_fingerprint,
            transport=endpoint.transport,
            llm=llm,
            prepare_credential=prepare_credential,
        )


def _bindings(
    models: ModelsConfig,
    *,
    environ: Mapping[str, str] | None,
) -> dict[tuple[str, str], _Binding]:
    bindings: dict[tuple[str, str], _Binding] = {}
    if models.endpoints:
        for endpoint_id, endpoint in models.endpoints.items():
            if not endpoint.enabled:
                continue
            resolved = resolve_endpoint_config(
                endpoint_id,
                endpoint,
                models.credential_pools,
                environ=environ,
            )
            _add_config_bindings(
                bindings,
                endpoint_id,
                resolved.llm_config,
                resolved.credential_slot_ids,
            )
        return bindings

    for endpoint_id, config in {"default": models.default, **models.tasks}.items():
        slot_ids = direct_credential_slot_ids(endpoint_id, config)
        _add_config_bindings(bindings, endpoint_id, config, slot_ids)
    return bindings


def _add_config_bindings(
    bindings: dict[tuple[str, str], _Binding],
    endpoint_id: str,
    config: LLMConfig,
    slot_ids: tuple[str, ...],
) -> None:
    if config.oauth is not None:
        for index, slot_id in enumerate(slot_ids):
            bindings[(endpoint_id, slot_id)] = _Binding(
                config=config.model_copy(deep=True),
                tenant_fingerprint=_tenant_fingerprint(endpoint_id, slot_id),
                force_oauth_refresh=index == 1,
            )
        return
    keys = config.api_key if isinstance(config.api_key, list) else [config.api_key]
    if len(keys) != len(slot_ids):
        raise ValueError(f"endpoint {endpoint_id!r} credential values do not match slot ids")
    for slot_id, api_key in zip(slot_ids, keys, strict=True):
        bound = config.model_copy(update={"api_key": api_key}, deep=True)
        bindings[(endpoint_id, slot_id)] = _Binding(
            config=bound,
            tenant_fingerprint=_tenant_fingerprint(endpoint_id, slot_id),
        )


def _tenant_fingerprint(endpoint_id: str, slot_id: str) -> str:
    public_binding = f"mote-model-tenant-v1\0{endpoint_id}\0{slot_id}"
    return hashlib.sha256(public_binding.encode("utf-8")).hexdigest()[:24]


__all__ = ["ProductModelEndpointResolver"]
