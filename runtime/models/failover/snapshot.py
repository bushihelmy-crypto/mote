"""Immutable, secret-opaque Runtime model configuration snapshots."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlsplit, urlunsplit

from mote.contracts.config.llm import LLMConfig
from mote.contracts.config.model_failover import ModelEndpointConfig
from mote.contracts.config.models import ModelsConfig
from mote.contracts.models.failover import AttemptBudget, EndpointCapabilities, EndpointDescriptor
from mote.contracts.models.profile import profile_for
from mote.contracts.models.tokenization import TOKEN_MAX
from mote.contracts.models.transport import resolve_api_type
from mote.runtime.models.failover.configuration import direct_credential_slot_ids


@dataclass(frozen=True)
class RuntimeFailoverGroup:
    group_id: str
    endpoint_ids: tuple[str, ...]
    policy_id: str
    budget: AttemptBudget


@dataclass(frozen=True)
class ModelRuntimeSnapshot:
    revision: str
    endpoints: tuple[EndpointDescriptor, ...]
    groups: tuple[RuntimeFailoverGroup, ...]
    route_groups: tuple[tuple[str, str], ...]
    credential_slots: tuple[tuple[str, tuple[str, ...]], ...]

    def endpoint(self, endpoint_id: str) -> EndpointDescriptor | None:
        return next(
            (endpoint for endpoint in self.endpoints if endpoint.endpoint_id == endpoint_id),
            None,
        )

    def group(self, group_id: str) -> RuntimeFailoverGroup | None:
        return next(
            (group for group in self.groups if group.group_id == group_id),
            None,
        )

    def group_for_route(self, route_id: str) -> RuntimeFailoverGroup | None:
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


def build_model_runtime_snapshot(models: ModelsConfig) -> ModelRuntimeSnapshot:
    """Compile validated config into an immutable, secret-free planner view."""

    public_shape = _public_config_shape(models)
    revision = hashlib.sha256(
        json.dumps(public_shape, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:24]

    if models.endpoints:
        endpoints = tuple(
            _declarative_descriptor(endpoint_id, endpoint, revision)
            for endpoint_id, endpoint in models.endpoints.items()
            if endpoint.enabled
        )
        groups = tuple(
            RuntimeFailoverGroup(
                group_id=group_id,
                endpoint_ids=tuple(group.endpoints),
                policy_id="default-v1",
                budget=models.recovery_profiles[group.recovery_profile].to_attempt_budget(),
            )
            for group_id, group in models.failover_groups.items()
        )
        declarative_routes: list[tuple[str, str]] = []
        if models.routes.default is not None:
            declarative_routes.append(("default", models.routes.default))
        declarative_routes.extend(models.routes.tasks.items())
        declarative_routes.extend(models.routes.semantic.items())
        slots = tuple(
            (endpoint_id, _credential_slot_ids(endpoint_id, endpoint, models))
            for endpoint_id, endpoint in models.endpoints.items()
            if endpoint.enabled
        )
        return ModelRuntimeSnapshot(
            revision=revision,
            endpoints=endpoints,
            groups=groups,
            route_groups=tuple(declarative_routes),
            credential_slots=slots,
        )

    legacy_configs = {"default": models.default, **models.tasks}
    endpoints = tuple(
        _legacy_descriptor(endpoint_id, llm_config, revision) for endpoint_id, llm_config in legacy_configs.items()
    )
    budget = models.recovery_profiles["default"].to_attempt_budget()
    groups = tuple(
        RuntimeFailoverGroup(
            group_id=f"route:{endpoint_id}",
            endpoint_ids=(endpoint_id,),
            policy_id="default-v1",
            budget=budget,
        )
        for endpoint_id in legacy_configs
    )
    legacy_routes = tuple((endpoint_id, f"route:{endpoint_id}") for endpoint_id in legacy_configs)
    slots = tuple(
        (endpoint_id, _direct_slot_ids(endpoint_id, config)) for endpoint_id, config in legacy_configs.items()
    )
    return ModelRuntimeSnapshot(
        revision=revision,
        endpoints=endpoints,
        groups=groups,
        route_groups=legacy_routes,
        credential_slots=slots,
    )


def _declarative_descriptor(
    endpoint_id: str,
    endpoint: ModelEndpointConfig,
    revision: str,
) -> EndpointDescriptor:
    pool_id = endpoint.credential_pool or f"direct:{endpoint_id}"
    return EndpointDescriptor(
        endpoint_id=endpoint_id,
        transport=resolve_api_type(endpoint).value,
        provider=endpoint.provider or resolve_api_type(endpoint).value,
        model=endpoint.model or "unknown",
        base_url_identity=_base_url_identity(endpoint.base_url),
        capabilities=_capabilities(endpoint),
        governance_domain=endpoint.governance_domain,
        region=endpoint.region,
        pricing_class=endpoint.pricing_class,
        credential_pool_id=pool_id,
        lifecycle_revision=revision,
    )


def _legacy_descriptor(
    endpoint_id: str,
    llm_config: LLMConfig,
    revision: str,
) -> EndpointDescriptor:
    return EndpointDescriptor(
        endpoint_id=endpoint_id,
        transport=resolve_api_type(llm_config).value,
        provider=llm_config.provider or resolve_api_type(llm_config).value,
        model=llm_config.model or "unknown",
        base_url_identity=_base_url_identity(llm_config.base_url),
        capabilities=_capabilities(llm_config),
        credential_pool_id=f"direct:{endpoint_id}",
        lifecycle_revision=revision,
    )


def _capabilities(config: LLMConfig) -> EndpointCapabilities:
    profile = profile_for(config.model)
    explicit = config.capabilities if isinstance(config, ModelEndpointConfig) else None

    def choose(name: str, inferred: bool) -> bool:
        value = getattr(explicit, name, None) if explicit is not None else None
        return inferred if value is None else value

    context_tokens = (
        explicit.context_tokens
        if explicit is not None and explicit.context_tokens is not None
        else _context_window(config.model)
    )
    return EndpointCapabilities(
        supports_tools=choose("supports_tools", explicit is None),
        supports_native_schema=choose(
            "supports_native_schema",
            profile.supports_native_structured_output,
        ),
        supports_server_web_search=choose(
            "supports_server_web_search",
            profile.supports_web_search,
        ),
        supports_vision=choose("supports_vision", profile.supports_vision),
        supports_pdf=choose("supports_pdf", profile.supports_pdf_input),
        supports_native_tool_search=choose(
            "supports_native_tool_search",
            profile.supports_native_tool_search,
        ),
        context_tokens=context_tokens,
    )


def _context_window(model: str | None) -> int:
    name = model or ""
    if name in TOKEN_MAX:
        return TOKEN_MAX[name]
    markers = [marker for marker in TOKEN_MAX if marker in name]
    return TOKEN_MAX[max(markers, key=len)] if markers else 0


def _base_url_identity(base_url: str) -> str:
    parsed = urlsplit(base_url)
    host = parsed.hostname or "unknown"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = f":{parsed.port}" if parsed.port is not None else ""
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme.lower(), f"{host.lower()}{port}", path, "", ""))


def _credential_slot_ids(
    endpoint_id: str,
    endpoint: ModelEndpointConfig,
    models: ModelsConfig,
) -> tuple[str, ...]:
    if endpoint.credential_pool is not None:
        return tuple(slot.id for slot in models.credential_pools[endpoint.credential_pool].slots)
    return _direct_slot_ids(endpoint_id, endpoint)


def _direct_slot_ids(endpoint_id: str, config: LLMConfig) -> tuple[str, ...]:
    return direct_credential_slot_ids(endpoint_id, config)


def _public_config_shape(models: ModelsConfig) -> Mapping[str, object]:
    if models.endpoints:
        endpoints = {
            endpoint_id: {
                "provider": endpoint.provider,
                "transport": resolve_api_type(endpoint).value,
                "model": endpoint.model,
                "base_url": _base_url_identity(endpoint.base_url),
                "credential_pool": endpoint.credential_pool,
                "credential_slots": list(_credential_slot_ids(endpoint_id, endpoint, models)),
                "region": endpoint.region,
                "governance_domain": endpoint.governance_domain,
                "pricing_class": endpoint.pricing_class,
                "capabilities": endpoint.capabilities.model_dump(exclude={"extra_fields"}),
                "enabled": endpoint.enabled,
            }
            for endpoint_id, endpoint in models.endpoints.items()
        }
        groups = {
            group_id: group.model_dump(exclude={"extra_fields"}) for group_id, group in models.failover_groups.items()
        }
        routes = models.routes.model_dump(exclude={"extra_fields"})
    else:
        endpoints = {
            endpoint_id: {
                "provider": config.provider,
                "transport": resolve_api_type(config).value,
                "model": config.model,
                "base_url": _base_url_identity(config.base_url),
                "credential_slots": len(config.api_key) if isinstance(config.api_key, list) else 1,
            }
            for endpoint_id, config in {
                "default": models.default,
                **models.tasks,
            }.items()
        }
        groups = {}
        routes = {}
    profiles = {
        profile_id: profile.model_dump(exclude={"extra_fields"})
        for profile_id, profile in models.recovery_profiles.items()
    }
    return {
        "endpoints": endpoints,
        "groups": groups,
        "routes": routes,
        "profiles": profiles,
    }


__all__ = [
    "ModelRuntimeSnapshot",
    "RuntimeFailoverGroup",
    "build_model_runtime_snapshot",
]
