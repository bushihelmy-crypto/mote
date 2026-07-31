from __future__ import annotations

from types import SimpleNamespace

import pytest

from mote.contracts.agent import BaseAgent
from mote.contracts.tool import CommandProtocol
from mote.kernel.output import text_output_contract
from mote.product.composition.container import ProductContainer
from mote.product.media_generation.registry import MediaProvider
from mote.product.web_search.registry import SearchBackend
from mote.runtime.agent import Role
from mote.runtime.control.lifecycle import LifecycleState
from mote.runtime.errors import ToolNotConfiguredError
from mote.runtime.services import EngineServices
from mote.runtime.tools.base_tool import BaseTool
from mote.runtime.tools.provider_definitions import NativeToolDefinition, XmlToolDefinition


def _config():
    def generation():
        return SimpleNamespace(
            provider="tenant",
            base_url="https://example.com",
            api_key="key",
            model="model",
            text_to_video_model="model",
            reference_guided_video_model="model",
        )

    return SimpleNamespace(
        multimodal=SimpleNamespace(
            image_generation=generation(),
            audio_generation=generation(),
            music_generation=generation(),
            video_generation=generation(),
        ),
        tools=SimpleNamespace(web_search=SimpleNamespace(backend="tenant")),
    )


def test_standard_product_containers_are_fully_isolated() -> None:
    first = ProductContainer.standard(_config())
    second = ProductContainer.standard(_config())

    assert first is not second
    assert first.agent_factory is not second.agent_factory
    assert first.providers is not second.providers
    assert first.media_providers is not second.media_providers
    assert first.search_backends is not second.search_backends
    assert first.tools is not second.tools
    assert first.agents is not second.agents
    assert first.routing_models is not second.routing_models


def test_builtin_catalogs_are_copied_into_each_container() -> None:
    container = ProductContainer.standard(_config())

    assert container.search_backends.get_backend("provider").name == "provider"
    assert sorted(container.media_providers.providers) == [
        ("audio", "openai"),
        ("image", "openai"),
        ("music", "openai"),
        ("video", "openai"),
    ]


def test_all_container_agents_share_one_routing_model_runtime() -> None:
    container = ProductContainer.standard(_config())
    first = container.agent_factory.dependencies(
        deps=None,
        output_contract=text_output_contract(),
    ).routing_strategy_builders["squilla"]()
    second = container.agent_factory.dependencies(
        deps=None,
        output_contract=text_output_contract(),
    ).routing_strategy_builders["squilla"]()

    assert first is not second
    assert first.runtime is container.routing_models
    assert second.runtime is container.routing_models


@pytest.mark.asyncio
async def test_engine_services_close_product_routing_runtime() -> None:
    class _Context:
        async def aclose(self) -> None:
            return None

    container = ProductContainer.standard(_config())
    services = EngineServices(
        context=_Context(),
        resources=container.lifecycle_resources(),
    )

    await services.aclose()

    assert container.routing_models.state is LifecycleState.CLOSED


def test_tool_factories_resolve_only_the_owning_container() -> None:
    class TenantMedia(MediaProvider):
        async def start_once(self, item, *, idempotency_key, timeout_seconds):
            return "tenant-id"

        async def poll_once(self, operation_id, state, *, timeout_seconds):
            return {"item": state}

    class TenantSearch(SearchBackend):
        name = "tenant"

        async def search(self, query, *, allowed_domains=None, blocked_domains=None):
            return [query]

    first = ProductContainer.standard(_config())
    second = ProductContainer.standard(_config())
    first.media_providers.register("image", "tenant", TenantMedia)
    first.search_backends.register("tenant", TenantSearch)

    def definitions(container):
        dependencies = container.agent_factory.dependencies(
            deps=None,
            output_contract=text_output_contract(),
            command_protocol=CommandProtocol.NATIVE,
        )
        return {
            definition.name: definition for toolset in dependencies.toolsets for definition in toolset.definitions()
        }

    first_definitions = definitions(first)
    second_definitions = definitions(second)
    first_search = first_definitions["WebSearch"].capability_factory()
    second_search = second_definitions["WebSearch"].capability_factory()

    assert isinstance(
        first.media_providers.create("image", first._config.multimodal.image_generation),
        TenantMedia,
    )
    assert isinstance(first.search_backends.create(first_search._config), TenantSearch)
    with pytest.raises(ToolNotConfiguredError):
        second.media_providers.create("image", second._config.multimodal.image_generation)
    with pytest.raises(ToolNotConfiguredError):
        second.search_backends.create(second_search._config)


def test_plugin_catalog_generation_does_not_mutate_existing_sessions() -> None:
    class TenantTool(BaseTool):
        name = "TenantTool"

        async def call(self) -> str:
            """Return the tenant marker."""

            return "tenant"

    class TenantAgent(BaseAgent, Role):
        agent_name = "TenantAgent"
        description = "Tenant-specific worker."

    original = ProductContainer.standard(_config())
    extended = original.with_plugins(
        tool_types=(TenantTool,),
        agent_types=(TenantAgent,),
    )

    assert extended.routing_models is original.routing_models

    assert original.tools.get("TenantTool") is None
    assert original.agents.get("TenantAgent") is None
    assert extended.tools.get("TenantTool") is TenantTool
    tenant_definition = extended.agents.get("TenantAgent")
    assert tenant_definition is not None
    assert tenant_definition.name == "TenantAgent"
    assert original.tools.version != extended.tools.version
    assert original.agents.version != extended.agents.version

    original_names = {
        definition.name
        for toolset in original.agent_factory.dependencies(
            deps=None,
            output_contract=text_output_contract(),
        ).toolsets
        for definition in toolset.definitions()
    }
    extended_names = {
        definition.name
        for toolset in extended.agent_factory.dependencies(
            deps=None,
            output_contract=text_output_contract(),
        ).toolsets
        for definition in toolset.definitions()
    }
    assert "TenantTool" not in original_names
    assert "TenantTool" in extended_names


def test_application_snapshot_projects_separate_xml_and_native_definitions() -> None:
    container = ProductContainer.standard(_config())

    def definitions(protocol):
        return [
            definition
            for toolset in container.agent_factory.dependencies(
                deps=None,
                output_contract=text_output_contract(),
                command_protocol=protocol,
            ).toolsets
            for definition in toolset.definitions()
        ]

    xml = definitions(CommandProtocol.XML)
    native = definitions(CommandProtocol.NATIVE)
    assert xml and native
    assert all(isinstance(definition, XmlToolDefinition) for definition in xml)
    assert all(isinstance(definition, NativeToolDefinition) for definition in native)
    assert {definition.name for definition in xml} == {definition.name for definition in native}

    xml_agent = next(definition for definition in xml if definition.name == "Agent")
    native_agent = next(definition for definition in native if definition.name == "Agent")
    assert xml_agent is not native_agent
    assert xml_agent.capability_factory()._agent_catalog is container.agents
    assert native_agent.capability_factory()._agent_catalog is container.agents
