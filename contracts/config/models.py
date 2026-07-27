#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Model configuration: the default LLM and task-routed LLMs.

Groups everything about *which model runs* under one roof:

- ``default``       — the main LLM every request uses unless routed elsewhere.
- ``tasks``         — task-name -> LLMConfig overrides (e.g. ``compression`` /
                      ``summary``); each inherits transport + credentials from
                      ``default`` unless the user set them explicitly.
- ``api_key_helper``— shell command that prints an API key on stdout, used to
                      fill ``default.api_key`` at load time when no static/env
                      key is present (trusted layers only; see CREDENTIAL_DENYLIST).
"""
from __future__ import annotations

from typing import Any, Dict

from pydantic import Field, model_validator

from mote.contracts.config.base import ConfigModel as YamlModel
from mote.contracts.config.llm import LLMConfig
from mote.contracts.config.model_failover import (
    CredentialPoolConfig,
    FailoverGroupConfig,
    ModelEndpointConfig,
    ModelRoutesConfig,
    RecoveryProfileConfig,
    default_recovery_profiles,
    ensure_default_recovery_profile,
)


def _default_tasks() -> Dict[str, LLMConfig]:
    """Built-in task-routed models. Keys are router task names."""
    return {
        "compression": LLMConfig(model="claude-sonnet-4-8"),
        "summary": LLMConfig(model="claude-sonnet-4-8"),
        "session_title": LLMConfig(model="claude-haiku-4-5-20251001"),
        "routing_judge": LLMConfig(model="claude-haiku-4-5-20251001"),
        # WebSearch's isolated secondary call (carries the provider server-side
        # web-search tool). A small/fast model suffices — it only relays the
        # query to the API's search backend and returns the structured hits. The
        # model MUST itself support server-side web search (be in
        # ``WEB_SEARCH_MODELS``); Haiku-4.5 qualifies and is the cheapest option.
        "web_search": LLMConfig(model="claude-haiku-4-5-20251001"),
        # WebBrowser's ``read_image`` isolated secondary call: feeds one on-page
        # image to a vision model and returns its textual reading (the browser
        # has no wire to hand an in-page ``<img>`` to the main model as media).
        # The routed model MUST support image input (be multimodal — see
        # ``supports_vision``); Haiku-4.5 is multimodal and the cheapest option.
        "image_description": LLMConfig(model="claude-haiku-4-5-20251001"),
    }


class ModelsConfig(YamlModel):
    """All LLM selection knobs (default model, task overrides)."""

    default: LLMConfig

    # Optional shell command that prints an API key on stdout. Used to fill
    # ``default.api_key`` at load time only when no static/env key is present
    # (precedence: env > static config > helper). Trusted layers only — the
    # untrusted workdir layer cannot inject it (see CREDENTIAL_DENYLIST).
    api_key_helper: str = ""

    # Forced reply language. When set, the system prompt's ``# Language`` section
    # tells the model to always respond in this language regardless of the user's
    # input language; empty means no override (mirror the user). Single source for
    # response language (replaces the old per-role ``RoleSchema.language``).
    response_language: str = "chinese"

    # Task-name -> isolated task model override. These never enter the semantic
    # routing pool. Transport + credentials inherit from ``default`` unless set.
    tasks: Dict[str, LLMConfig] = Field(default_factory=_default_tasks)

    # Explicit failover graph. Empty maps retain the legacy default/tasks-only
    # composition; when populated, Runtime composes exactly these endpoints and
    # groups without treating unrelated task cards as fallback candidates.
    credential_pools: Dict[str, CredentialPoolConfig] = Field(default_factory=dict)
    endpoints: Dict[str, ModelEndpointConfig] = Field(default_factory=dict)
    failover_groups: Dict[str, FailoverGroupConfig] = Field(default_factory=dict)
    routes: ModelRoutesConfig = Field(default_factory=ModelRoutesConfig)
    recovery_profiles: Dict[str, RecoveryProfileConfig] = Field(default_factory=default_recovery_profiles)

    @model_validator(mode="before")
    @classmethod
    def _add_default_recovery_profile(cls, values: Any) -> Any:
        return ensure_default_recovery_profile(values)

    @model_validator(mode="after")
    def _inherit_task_transport(self):
        """Let each task-routed llm inherit transport/credentials from ``default``.

        By default only the model differs on a task override; the endpoint, key
        and api type come from ``default`` unless the user set them explicitly.
        """
        base = LLMConfig()
        for task_llm in self.tasks.values():
            if task_llm.base_url == base.base_url:
                task_llm.base_url = self.default.base_url
            if task_llm.api_key == base.api_key:
                task_llm.api_key = self.default.api_key
            if task_llm.api_type == base.api_type:
                task_llm.api_type = self.default.api_type
        return self

    @model_validator(mode="after")
    def _validate_failover_graph(self) -> "ModelsConfig":
        overlapping_routes = set(self.routes.tasks) & set(self.routes.semantic)
        if overlapping_routes:
            raise ValueError("task and semantic model route ids must be disjoint: " f"{sorted(overlapping_routes)!r}")
        if "default" in self.routes.semantic:
            raise ValueError("the fixed default route cannot also belong to the semantic pool")
        for namespace, values in (
            ("credential pool", self.credential_pools),
            ("endpoint", self.endpoints),
            ("failover group", self.failover_groups),
            ("recovery profile", self.recovery_profiles),
        ):
            empty = [name for name in values if not name]
            if empty:
                raise ValueError(f"{namespace} id cannot be empty")

        for endpoint_id, endpoint in self.endpoints.items():
            pool = endpoint.credential_pool
            if pool is not None and pool not in self.credential_pools:
                raise ValueError(f"endpoint {endpoint_id!r} references unknown credential pool {pool!r}")
            if pool is not None and endpoint.oauth is not None:
                raise ValueError(f"endpoint {endpoint_id!r} cannot combine a static credential " "pool with OAuth")

        for group_id, group in self.failover_groups.items():
            unknown_endpoints = [endpoint for endpoint in group.endpoints if endpoint not in self.endpoints]
            if unknown_endpoints:
                raise ValueError(f"failover group {group_id!r} references unknown endpoints " f"{unknown_endpoints!r}")
            disabled_endpoints = [endpoint for endpoint in group.endpoints if not self.endpoints[endpoint].enabled]
            if disabled_endpoints:
                raise ValueError(
                    f"failover group {group_id!r} references disabled endpoints " f"{disabled_endpoints!r}"
                )
            if group.recovery_profile not in self.recovery_profiles:
                raise ValueError(
                    f"failover group {group_id!r} references unknown recovery " f"profile {group.recovery_profile!r}"
                )

        route_groups = []
        if self.routes.default is not None:
            route_groups.append(("default", self.routes.default))
        route_groups.extend((f"task {task!r}", group_id) for task, group_id in self.routes.tasks.items())
        route_groups.extend((f"semantic {route_id!r}", group_id) for route_id, group_id in self.routes.semantic.items())
        for route_name, group_id in route_groups:
            if group_id not in self.failover_groups:
                raise ValueError(f"model route {route_name} references unknown failover group " f"{group_id!r}")

        compression_group_id = self.routes.tasks.get("compression")
        if compression_group_id is not None:
            group = self.failover_groups[compression_group_id]
            profile = self.recovery_profiles[group.recovery_profile]
            if profile.max_request_transforms != 0:
                raise ValueError("compression route recovery profile must set " "max_request_transforms=0")
        return self
