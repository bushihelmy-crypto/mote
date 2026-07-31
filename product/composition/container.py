"""Per-Application ownership of Interface-independent Product catalogs."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

from mote.contracts.tool import CommandProtocol
from mote.product.agents.catalog import AgentCatalog
from mote.product.agents.discovery import builtin_agent_catalog
from mote.product.agents.factory import CodingAgentFactory
from mote.product.composition.lifecycle import lifecycle_resources
from mote.product.config.adapters.hooks import load_global_hooks
from mote.product.config.adapters.mcp import load_mcp_servers
from mote.product.config.diagnostics import is_secret_path
from mote.product.config.schema import MoteConfig
from mote.product.config.sources import discover_source_files
from mote.product.media_generation.catalog import builtin_media_provider_registry
from mote.product.media_generation.registry import MediaProviderRegistry
from mote.product.models.bootstrap import builtin_provider_registry
from mote.product.models.registry import LLMProviderRegistry
from mote.product.paths import RuntimePaths, default_runtime_paths, mote_layered_files
from mote.product.routing import builtin_routing_strategy_builders
from mote.product.routing.squilla.ml.runtime import RoutingModelRuntime
from mote.product.skills import ProductSkillServiceFactory
from mote.product.toolsets import builtin_tool_catalog, builtin_toolsets
from mote.product.toolsets.builtin.agent_tool import Agent
from mote.product.toolsets.builtin.generate_media.generate_media_tool import GenerateMedia
from mote.product.toolsets.builtin.web_search import WebSearch
from mote.product.web_search.registry import SearchBackendRegistry, builtin_search_backend_registry
from mote.runtime.tools.tool_registry import ToolCatalog


class _AgentComposition:
    """Own the recursive Product factory/catalog graph as one lazy value."""

    def __init__(
        self,
        config: MoteConfig,
        *,
        tools: ToolCatalog,
        routing_models: RoutingModelRuntime,
        cwd: Path | None,
        paths: RuntimePaths,
        agent_types: tuple[type, ...] | None = None,
    ) -> None:
        self._config = config
        self._tools = tools
        self._routing_models = routing_models
        self._cwd = cwd
        self._paths = paths
        self._agent_types = agent_types

    @cached_property
    def agents(self) -> AgentCatalog[str]:
        if self._agent_types is None:
            return builtin_agent_catalog(self.factory, self._cwd)
        return AgentCatalog.from_types(self._agent_types, self.factory)

    def _toolsets(self, protocol: str | CommandProtocol):
        agents = self.agents
        return builtin_toolsets(
            protocol,
            catalog=self._tools,
            capability_factories={
                "Agent": lambda: Agent(agents),
                "GenerateMedia": lambda: GenerateMedia(self._config.multimodal),
                "WebSearch": lambda: WebSearch(self._config.tools.web_search),
            },
            descriptions={"Agent": Agent.description_for(agents)},
        )

    @cached_property
    def factory(self) -> CodingAgentFactory:
        source_files = discover_source_files(
            user_config_root=self._paths.user_config_root,
            source_root=self._paths.user_config_root,
        )
        return CodingAgentFactory(
            toolsets_factory=self._toolsets,
            routing_strategy_builders_factory=lambda: builtin_routing_strategy_builders(self._routing_models),
            paths=self._paths,
            cwd=self._cwd,
            skill_service_factory=ProductSkillServiceFactory(self._paths.user_config_root),
            hook_config=load_global_hooks(
                mote_layered_files(
                    "hooks.json",
                    self._cwd,
                    user_config_root=self._paths.user_config_root,
                )
            ),
            mcp_servers=tuple(
                load_mcp_servers(
                    mote_layered_files(
                        "mcp.json",
                        self._cwd,
                        user_config_root=self._paths.user_config_root,
                    )
                )
            ),
            primary_config_path=source_files[-1].path if source_files else None,
            config_secret_predicate=is_secret_path,
            user_config_root=self._paths.user_config_root,
            session_workspace_root=self._paths.session_workspace_root,
            browser_profiles_root=self._paths.browser_profiles_root,
            sandbox_ca_root=self._paths.sandbox_ca_root,
            secrets_root=self._paths.secrets_root,
            oauth_root=self._paths.oauth_root,
        )


@dataclass(frozen=True, slots=True)
class ProductContainer:
    """Isolated Product factories and catalogs owned by one Application."""

    agent_factory: CodingAgentFactory
    providers: LLMProviderRegistry
    media_providers: MediaProviderRegistry
    search_backends: SearchBackendRegistry
    tools: ToolCatalog
    agents: AgentCatalog[str]
    routing_models: RoutingModelRuntime
    paths: RuntimePaths
    _config: MoteConfig = field(repr=False, compare=False)

    @classmethod
    def standard(
        cls,
        config: MoteConfig,
        *,
        cwd: Path | None = None,
        paths: RuntimePaths | None = None,
    ) -> "ProductContainer":
        paths = paths or default_runtime_paths()
        media_providers = builtin_media_provider_registry()
        search_backends = builtin_search_backend_registry()
        tools = builtin_tool_catalog()
        routing_models = RoutingModelRuntime()
        agent_composition = _AgentComposition(config, tools=tools, routing_models=routing_models, cwd=cwd, paths=paths)
        return cls(
            agent_factory=agent_composition.factory,
            providers=builtin_provider_registry(oauth_root=paths.oauth_root),
            media_providers=media_providers,
            search_backends=search_backends,
            tools=tools,
            agents=agent_composition.agents,
            routing_models=routing_models,
            paths=paths,
            _config=config,
        )

    def lifecycle_resources(self):
        return lifecycle_resources(self.routing_models)

    def with_plugins(
        self,
        *,
        tool_types: tuple[type, ...] = (),
        agent_types: tuple[type, ...] = (),
    ) -> "ProductContainer":
        tools = self.tools.with_types(*tool_types)
        composition = _AgentComposition(
            self._config,
            tools=tools,
            routing_models=self.routing_models,
            cwd=None,
            paths=self.paths,
            agent_types=(*self.agents.declared_types(), *agent_types),
        )
        return type(self)(
            agent_factory=composition.factory,
            providers=self.providers,
            media_providers=self.media_providers,
            search_backends=self.search_backends,
            tools=tools,
            agents=composition.agents,
            routing_models=self.routing_models,
            paths=self.paths,
            _config=self._config,
        )


__all__ = ["ProductContainer"]
