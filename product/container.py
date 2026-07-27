"""Per-Application ownership of Product composition catalogs."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mote.product.agents.discovery import builtin_agent_catalog
from mote.product.agents.factory import CodingAgentFactory
from mote.product.cli.commands.core import CommandRegistry
from mote.product.cli.commands.registry import default_registry as default_command_registry
from mote.product.cli.consumers.core import ConsumerRegistry
from mote.product.cli.consumers.registry import default_registry as default_consumer_registry
from mote.product.integrations.bootstrap import builtin_provider_registry
from mote.product.routing import builtin_routing_strategy_builders
from mote.product.routing.squilla.ml.runtime import RoutingModelRuntime
from mote.product.toolsets import builtin_tool_catalog, builtin_toolsets
from mote.product.toolsets.builtin.agent_tool import Agent
from mote.product.toolsets.builtin.generate_media.bootstrap import builtin_media_provider_registry
from mote.product.toolsets.builtin.generate_media.generate_media_tool import GenerateMedia
from mote.product.toolsets.builtin.generate_media.registry import MediaProviderRegistry
from mote.product.toolsets.builtin.web_search import WebSearch
from mote.product.toolsets.builtin.web_search_registry import SearchBackendRegistry, builtin_search_backend_registry
from mote.runtime.lifecycle import LifecycleResource
from mote.runtime.models.clients.registry import LLMProviderRegistry
from mote.runtime.tools.agent_registry import AgentCatalog
from mote.runtime.tools.tool_registry import ToolCatalog


def _coding_agent_factory(
    config: Any,
    *,
    tools: ToolCatalog,
    agents: AgentCatalog,
    routing_models: RoutingModelRuntime,
) -> CodingAgentFactory:
    capability_factories = {
        "Agent": lambda: Agent(agents),
        "GenerateMedia": lambda: GenerateMedia(
            config.multimodal,
        ),
        "WebSearch": lambda: WebSearch(
            config.tools.web_search,
        ),
    }
    descriptions = {"Agent": Agent.description_for(agents)}
    return CodingAgentFactory(
        toolsets_factory=lambda protocol: builtin_toolsets(
            protocol,
            catalog=tools,
            capability_factories=capability_factories,
            descriptions=descriptions,
        ),
        routing_strategy_builders_factory=lambda: builtin_routing_strategy_builders(routing_models),
    )


@dataclass(frozen=True, slots=True)
class ProductContainer:
    """Isolated Product factories and catalogs owned by one Application.

    Runtime never imports this type. Product integrations are assembled once at
    the application boundary, then injected through Runtime's existing ports
    and immutable Agent wiring.
    """

    agent_factory: CodingAgentFactory
    providers: LLMProviderRegistry
    commands: CommandRegistry
    consumers: ConsumerRegistry
    media_providers: MediaProviderRegistry
    search_backends: SearchBackendRegistry
    tools: ToolCatalog
    agents: AgentCatalog
    routing_models: RoutingModelRuntime
    _config: Any = field(repr=False, compare=False)

    @classmethod
    def standard(cls, config: Any, *, cwd: Path | None = None) -> "ProductContainer":
        """Build a fresh container containing Mote's bundled integrations."""

        media_providers = builtin_media_provider_registry()
        search_backends = builtin_search_backend_registry()
        tools = builtin_tool_catalog()
        agents = builtin_agent_catalog(cwd)
        routing_models = RoutingModelRuntime()

        return cls(
            agent_factory=_coding_agent_factory(
                config,
                tools=tools,
                agents=agents,
                routing_models=routing_models,
            ),
            providers=builtin_provider_registry(),
            commands=default_command_registry(),
            consumers=default_consumer_registry(),
            media_providers=media_providers,
            search_backends=search_backends,
            tools=tools,
            agents=agents,
            routing_models=routing_models,
            _config=config,
        )

    def lifecycle_resources(self) -> tuple[LifecycleResource, ...]:
        """Return resources owned by the Engine serving this container."""

        return (self.routing_models.lifecycle_resource(),)

    def with_plugins(
        self,
        *,
        tool_types: tuple[type, ...] = (),
        agent_types: tuple[type, ...] = (),
    ) -> "ProductContainer":
        """Create a new catalog generation without changing running sessions."""

        tools = self.tools.with_types(*tool_types)
        agents = self.agents.with_types(*agent_types)
        return type(self)(
            agent_factory=_coding_agent_factory(
                self._config,
                tools=tools,
                agents=agents,
                routing_models=self.routing_models,
            ),
            providers=self.providers,
            commands=self.commands,
            consumers=self.consumers,
            media_providers=self.media_providers,
            search_backends=self.search_backends,
            tools=tools,
            agents=agents,
            routing_models=self.routing_models,
            _config=self._config,
        )


__all__ = ["ProductContainer"]
