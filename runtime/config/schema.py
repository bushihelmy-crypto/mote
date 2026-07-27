#!/usr/bin/env python
# -*- coding: utf-8 -*-
from pydantic import Field, model_validator

from mote.contracts.config.base import ConfigModel as YamlModel
from mote.contracts.config.context import ContextConfig
from mote.contracts.config.mcp import MCPConfig
from mote.contracts.config.models import ModelsConfig
from mote.contracts.config.multimodal import MultimodalConfig
from mote.contracts.config.observability import ObservabilityConfig
from mote.contracts.config.resilience import ResilienceConfig
from mote.contracts.config.routing import RouterConfig
from mote.contracts.config.secrets import SecretsConfig
from mote.contracts.config.tools import ToolsConfig
from mote.contracts.config.ui import UIConfig
from mote.contracts.config.workspace import WorkspaceConfig


class Config(YamlModel):
    """Configurations for Mote.

    Grouped by concern: ``models`` (which LLMs run), ``tools`` (tool runtime
    knobs), ``context`` (context engineering), ``multimodal`` (media services),
    ``mcp`` (the MCP master switch — servers live in their own
    ``mcp_config.json``, never here), ``observability`` (Sentry/Langfuse), ``ui``
    (human display), ``secrets`` (redaction/vault) and ``workspace`` (disk-layer
    TTL cleanup).
    """

    # Which models run: the default LLM, per-task overrides, routing switch and
    # the api-key helper.
    models: ModelsConfig

    # Tool-facing runtime knobs (proxy, browser locale).
    tools: ToolsConfig = Field(default_factory=ToolsConfig)

    # Context engineering (compaction, code map, skills).
    context: ContextConfig = Field(default_factory=ContextConfig)

    # Multimodal services (image/audio/music/video generation).
    multimodal: MultimodalConfig = Field(default_factory=MultimodalConfig)

    # MCP subsystem master switch (servers live in ``mcp_config.json``).
    mcp: MCPConfig = Field(default_factory=MCPConfig)

    # Error tracking (Sentry) + LLM tracing (Langfuse).
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)

    # Human display layer (CLI) preferences — e.g. the display language. Purely
    # human-facing; model-facing text (prompts, tool output) stays English.
    ui: UIConfig = Field(default_factory=UIConfig)

    # Always-on secret disclosure boundary. Storage and cipher are configurable;
    # prompt/result protection itself is a core policy and cannot be disabled.
    secrets: SecretsConfig = Field(default_factory=SecretsConfig)

    # On-disk workspace settings — currently the periodic TTL cleanup sweep that
    # reclaims stale per-session artifacts (rollout / blobs / tool_results /
    # task_outputs). Grouped here so the workspace tree has one config home.
    workspace: WorkspaceConfig = Field(default_factory=WorkspaceConfig)

    # Circuit-breaker thresholds for per-resource health gating (LLM provider
    # failover today; MCP / egress tomorrow).
    resilience: ResilienceConfig = Field(default_factory=ResilienceConfig)

    # Intelligent-routing knobs — the routing *strategy* every per-role
    # LLMRouter is built with, plus the spawn-time seed-floor switch. A concern
    # of its own (orthogonal to *which* models exist under ``models``), so it
    # lives here at top level. Defaults reproduce today's behavior (rule-based,
    # no spawn routing), so this block is inert until explicitly enabled.
    router: RouterConfig = Field(default_factory=RouterConfig)

    @model_validator(mode="after")
    def _validate_semantic_routing_graph(self) -> "Config":
        configured_routes = set(self.router.routes)
        model_routes = set(self.models.routes.semantic)
        missing_gateway_routes = configured_routes - model_routes
        if missing_gateway_routes:
            raise ValueError(
                "router semantic routes have no models.routes.semantic binding: " f"{sorted(missing_gateway_routes)!r}"
            )
        unknown_metadata = model_routes - configured_routes
        if unknown_metadata:
            raise ValueError("models.routes.semantic routes have no router metadata: " f"{sorted(unknown_metadata)!r}")

        for kind, agent in (
            ("main_agent", self.router.main_agent),
            ("sub_agent", self.router.sub_agent),
        ):
            if agent.strategy is None:
                continue
            if not configured_routes:
                raise ValueError(f"router.{kind} enables routing with an empty semantic pool")
            pool = set(agent.candidates or tuple(configured_routes))
            if not pool:
                raise ValueError(f"router.{kind} enables routing with an empty semantic pool")
            unknown = pool - configured_routes
            if unknown:
                raise ValueError(f"router.{kind} references unknown semantic routes {sorted(unknown)!r}")
            if agent.default_route not in pool:
                raise ValueError(f"router.{kind}.default_route must belong to its candidate pool")
            disabled = {route_id for route_id in pool if not self.router.routes[route_id].enabled}
            if disabled:
                raise ValueError(f"router.{kind} references disabled semantic routes " f"{sorted(disabled)!r}")
            mapped = set(agent.class_routes.values())
            if agent.strategy == "squilla" and set(agent.class_routes) != {
                "R0",
                "R1",
                "R2",
                "R3",
            }:
                raise ValueError(f"router.{kind} squilla policy requires explicit R0-R3 class_routes")
            if not mapped.issubset(pool):
                raise ValueError(
                    f"router.{kind}.class_routes reference routes outside its pool: " f"{sorted(mapped - pool)!r}"
                )
            mismatched_classes = {
                route_class: route_id
                for route_class, route_id in agent.class_routes.items()
                if self.router.routes[route_id].quality_class != route_class
            }
            if mismatched_classes:
                raise ValueError(
                    f"router.{kind}.class_routes disagree with route quality_class: " f"{mismatched_classes!r}"
                )
        if self.router.spawn_routing and self.router.sub_agent.strategy != "squilla":
            raise ValueError("router.spawn_routing requires router.sub_agent.strategy='squilla'")
        return self
