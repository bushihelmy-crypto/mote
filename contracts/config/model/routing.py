#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Validated configuration for provider-neutral semantic model routing."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import Field, field_validator, model_validator

from mote.contracts.config.base import ConfigModel as YamlModel

_ROUTE_CLASSES = frozenset({"R0", "R1", "R2", "R3"})


class SemanticRouteConfig(YamlModel):
    """Policy-facing metadata for one secret-free logical model route."""

    quality_class: str = "R1"
    quality_rank: int = Field(default=1, ge=0)
    cost_class: str = "standard"
    latency_class: str = "standard"
    tags: frozenset[str] = frozenset()
    data_classifications: frozenset[str] = frozenset({"default"})
    enabled: bool = True


class AgentRouterConfig(YamlModel):
    """Per-agent-kind routing strategy selection."""

    strategy: Optional[Literal["rule", "squilla"]] = None
    default_route: str = "default"
    candidates: tuple[str, ...] = ()
    class_routes: dict[str, str] = Field(default_factory=dict)
    deadline_ms: float = Field(default=50.0, gt=0.0, le=5_000.0)

    @field_validator("candidates")
    @classmethod
    def _candidate_ids_are_unique(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value for value in values):
            raise ValueError("routing candidate id cannot be empty")
        if len(values) != len(set(values)):
            raise ValueError("routing candidates contain duplicate route ids")
        return values

    @model_validator(mode="after")
    def _validate_class_routes(self) -> "AgentRouterConfig":
        if self.class_routes and set(self.class_routes) != _ROUTE_CLASSES:
            raise ValueError("class_routes must define exactly R0, R1, R2 and R3")
        return self


class RouterConfig(YamlModel):
    """Intelligent-routing knobs. Defaults reproduce today's behavior (inert)."""

    main_agent: AgentRouterConfig = Field(default_factory=AgentRouterConfig)
    sub_agent: AgentRouterConfig = Field(default_factory=AgentRouterConfig)
    spawn_routing: bool = False

    # The semantic pool only. Task routes remain under models.routes.tasks and
    # endpoint failover remains under models.failover_groups.
    routes: dict[str, SemanticRouteConfig] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_route_ids(self) -> "RouterConfig":
        if any(not route_id for route_id in self.routes):
            raise ValueError("semantic route id cannot be empty")
        return self


__all__ = ["AgentRouterConfig", "RouterConfig", "SemanticRouteConfig"]
