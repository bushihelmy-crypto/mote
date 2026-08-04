"""Per-Application ownership of Interface-independent Product catalogs."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

from mote.contracts.agent import ApprovedDeclaration
from mote.contracts.ports.task.operations import BackgroundTaskServiceFactory
from mote.contracts.ports.tool.deferred import DeferredResultProjectorFactory
from mote.contracts.tool import CommandProtocol
from mote.product.agents.background_tasks import build_background_task_pool
from mote.product.agents.catalog import AgentCatalog, SpawnableTextAgentClass
from mote.product.agents.deferred_projection import build_deferred_result_projector
from mote.product.agents.discovery import builtin_agent_catalog
from mote.product.agents.factory import CodingAgentFactory
from mote.product.config.adapters.hooks import load_global_hooks
from mote.product.config.adapters.mcp import load_mcp_servers
from mote.product.config.diagnostics import is_secret_path
from mote.product.config.model_checkpoint import approved_model_checkpoint_policy
from mote.product.config.schema import Config
from mote.product.config.sources import discover_source_files
from mote.product.extensions.sources import ApprovedExtensionSnapshot, ExtensionKind, ExtensionSourcePolicy
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
        config: Config,
        *,
        tools: ToolCatalog,
        routing_models: RoutingModelRuntime,
        cwd: Path | None,
        paths: RuntimePaths,
        extension_sources: ExtensionSourcePolicy,
        background_task_pool_builder: BackgroundTaskServiceFactory,
        deferred_result_projector_factory: DeferredResultProjectorFactory,
        agent_types: tuple[SpawnableTextAgentClass, ...] = (),
    ) -> None:
        self._config = config
        self._tools = tools
        self._routing_models = routing_models
        self._cwd = cwd
        self._paths = paths
        self._extension_sources = extension_sources
        self._background_task_pool_builder = background_task_pool_builder
        self._deferred_result_projector_factory = deferred_result_projector_factory
        self._agent_types = agent_types

    @cached_property
    def agents(self) -> AgentCatalog[str]:
        catalog = builtin_agent_catalog(self.factory, self._cwd, source_policy=self._extension_sources)
        if not self._agent_types:
            return catalog
        return catalog.with_types(self._agent_types, self.factory)

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
        )
        hook_sources = self._extension_sources.admitted_files(
            ExtensionKind.HOOK,
            mote_layered_files("hooks.json", self._cwd, user_config_root=self._paths.user_config_root),
        )
        mcp_sources = self._extension_sources.admitted_files(
            ExtensionKind.MCP,
            mote_layered_files("mcp.json", self._cwd, user_config_root=self._paths.user_config_root),
        )
        hook_config = load_global_hooks(hook_sources)
        mcp_servers = tuple(load_mcp_servers(mcp_sources))
        return CodingAgentFactory(
            model_checkpoint_policy=approved_model_checkpoint_policy(),
            toolsets_factory=self._toolsets,
            background_task_pool_builder=self._background_task_pool_builder,
            deferred_result_projector_factory=self._deferred_result_projector_factory,
            routing_strategy_builders_factory=lambda: builtin_routing_strategy_builders(self._routing_models),
            paths=self._paths,
            cwd=self._cwd,
            skill_service_factory=ProductSkillServiceFactory(
                self._paths.user_config_root,
                source_policy=self._extension_sources,
            ),
            hooks=(
                ApprovedDeclaration(hook_config, tuple(source.approved_identity() for source in hook_sources))
                if hook_config is not None
                else None
            ),
            mcp=(
                ApprovedDeclaration(mcp_servers, tuple(source.approved_identity() for source in mcp_sources))
                if mcp_servers
                else None
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


@dataclass(frozen=True)
class ProductContainer:
    """Isolated, lazily materialized Product composition declaration.

    ``standard`` is a pure construction boundary.  Reading a catalog property
    materializes that built-in capability for an activating consumer; checkout
    extension discovery is therefore never a side effect of construction.
    """

    routing_models: RoutingModelRuntime
    paths: RuntimePaths
    _config: Config = field(repr=False, compare=False)
    _agent_composition: _AgentComposition = field(repr=False, compare=False)
    extension_sources: ExtensionSourcePolicy = field(repr=False, compare=False)

    @cached_property
    def agent_factory(self) -> CodingAgentFactory:
        return self._agent_composition.factory

    @cached_property
    def providers(self) -> LLMProviderRegistry:
        return builtin_provider_registry(oauth_root=self.paths.oauth_root)

    @cached_property
    def media_providers(self) -> MediaProviderRegistry:
        return builtin_media_provider_registry()

    @cached_property
    def search_backends(self) -> SearchBackendRegistry:
        return builtin_search_backend_registry()

    @cached_property
    def tools(self) -> ToolCatalog:
        return self._agent_composition._tools

    @cached_property
    def agents(self) -> AgentCatalog[str]:
        return self._agent_composition.agents

    @classmethod
    def standard(
        cls,
        config: Config,
        *,
        cwd: Path | None = None,
        paths: RuntimePaths | None = None,
        extension_approvals: ApprovedExtensionSnapshot = ApprovedExtensionSnapshot(),
        background_task_pool_builder: BackgroundTaskServiceFactory | None = None,
        deferred_result_projector_factory: DeferredResultProjectorFactory = build_deferred_result_projector,
    ) -> "ProductContainer":
        paths = paths or default_runtime_paths()
        tools = builtin_tool_catalog()
        routing_models = RoutingModelRuntime()
        extension_sources = ExtensionSourcePolicy(
            user_root=paths.user_config_root,
            builtin_roots=(paths.package_data_root,),
            snapshot=extension_approvals,
        )
        agent_composition = _AgentComposition(
            config,
            tools=tools,
            routing_models=routing_models,
            cwd=cwd,
            paths=paths,
            extension_sources=extension_sources,
            background_task_pool_builder=(
                background_task_pool_builder if background_task_pool_builder is not None else build_background_task_pool
            ),
            deferred_result_projector_factory=deferred_result_projector_factory,
        )
        return cls(
            routing_models=routing_models,
            paths=paths,
            _config=config,
            _agent_composition=agent_composition,
            extension_sources=extension_sources,
        )

    def with_plugins(
        self,
        *,
        tool_types: tuple[type, ...] = (),
        agent_types: tuple[SpawnableTextAgentClass, ...] = (),
    ) -> "ProductContainer":
        tools = self.tools.with_types(*tool_types)
        composition = _AgentComposition(
            self._config,
            tools=tools,
            routing_models=self.routing_models,
            cwd=self._agent_composition._cwd,
            paths=self.paths,
            extension_sources=self.extension_sources,
            background_task_pool_builder=self._agent_composition._background_task_pool_builder,
            deferred_result_projector_factory=self._agent_composition._deferred_result_projector_factory,
            agent_types=(*self._agent_composition._agent_types, *agent_types),
        )
        return type(self)(
            routing_models=self.routing_models,
            paths=self.paths,
            _config=self._config,
            _agent_composition=composition,
            extension_sources=self.extension_sources,
        )


__all__ = ["ProductContainer"]
