"""Runtime activation of validated declarative model endpoint configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

from mote.contracts.config.model.failover import CredentialPoolConfig, ModelEndpointConfig
from mote.contracts.config.model.llm import LLMConfig


@dataclass(frozen=True)
class ResolvedEndpointConfig:
    llm_config: LLMConfig
    credential_slot_ids: tuple[str, ...]


def direct_credential_slot_ids(
    endpoint_id: str,
    config: LLMConfig,
) -> tuple[str, ...]:
    """Return opaque slots for one directly configured credential source."""

    if config.oauth is not None:
        return (f"{endpoint_id}:oauth-current", f"{endpoint_id}:oauth-refresh")
    count = len(config.api_key) if isinstance(config.api_key, list) else 1
    return tuple(f"{endpoint_id}:{index}" for index in range(count))


def resolve_endpoint_config(
    endpoint_id: str,
    endpoint: ModelEndpointConfig,
    credential_pools: Mapping[str, CredentialPoolConfig],
    *,
    environ: Mapping[str, str] | None = None,
) -> ResolvedEndpointConfig:
    """Resolve opaque env references only while activating the Runtime snapshot."""

    api_key = endpoint.api_key
    slot_ids: tuple[str, ...] = ()
    if endpoint.credential_pool is not None:
        pool = credential_pools[endpoint.credential_pool]
        source = os.environ if environ is None else environ
        keys: list[str] = []
        ids: list[str] = []
        for slot in pool.slots:
            env_name = slot.secret_ref.removeprefix("env://")
            value = source.get(env_name)
            if not value:
                raise ValueError(
                    f"credential slot {slot.id!r} for endpoint {endpoint_id!r} "
                    f"references unset environment variable {env_name!r}"
                )
            keys.append(value)
            ids.append(slot.id)
        api_key = keys[0] if len(keys) == 1 else keys
        slot_ids = tuple(ids)

    values = {name: getattr(endpoint, name) for name in LLMConfig.model_fields}
    values["api_key"] = api_key
    llm_config = LLMConfig(**values)
    if not slot_ids:
        slot_ids = direct_credential_slot_ids(endpoint_id, llm_config)
    return ResolvedEndpointConfig(
        llm_config=llm_config,
        credential_slot_ids=slot_ids,
    )


__all__ = [
    "ResolvedEndpointConfig",
    "direct_credential_slot_ids",
    "resolve_endpoint_config",
]
