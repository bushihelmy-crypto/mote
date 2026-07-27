#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for the smaller executor helpers:

- ``tool_convert`` — function/AST -> tool schema.
- ``agent_registry`` — spawnable-agent registry (must subclass Role).
- ``mcp_adapter`` — wrap a discovered MCP tool as a BaseTool.

NOTE: deliberately NO ``from __future__ import annotations`` here — these tests
assert the rendered ``inspect.signature`` of locally-defined functions, which
would be stringized (``'int'`` instead of ``int``) under PEP 563.
"""
import pytest

from mote.runtime.tools.agent_registry import AgentRegistry
from mote.runtime.tools.mcp.adapter import MCPToolAdapter, McpXmlSchemaError
from mote.runtime.tools.mcp.toolsets import NativeMcpToolset, XmlMcpToolset
from mote.runtime.tools.mcp.types import DiscoveredMcpTool
from mote.runtime.tools.tool_convert import function_docstring_to_schema
from mote.runtime.tools.tool_executor import ToolExecutor

# ---------------------------------------------------------------------------
# tool_convert
# ---------------------------------------------------------------------------


class TestFunctionDocstringToSchema:
    def test_sync_function_type(self):
        def fn(x: str) -> str:
            """One liner."""
            return x

        schema = function_docstring_to_schema(fn, fn.__doc__)
        assert schema["type"] == "function"
        assert schema["description"] == "One liner."
        assert schema["signature"] == "(x: str) -> str"

    def test_async_function_type(self):
        async def fn(a: int, b: str = "z") -> str:
            """Does a thing.

            Args:
                a: the a.
                b: the b.
            """
            return ""

        schema = function_docstring_to_schema(fn, fn.__doc__)
        assert schema["type"] == "async_function"
        assert schema["signature"] == "(a: int, b: str = 'z') -> str"
        assert "the a." in schema["parameters"]

    def test_empty_docstring_yields_empty_params(self):
        def fn(x):
            ...

        schema = function_docstring_to_schema(fn, "")
        assert schema["parameters"] == ""


# ---------------------------------------------------------------------------
# agent_registry
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_agent_registry() -> AgentRegistry:
    """Return an isolated Agent registry."""
    return AgentRegistry()


class TestAgentRegistry:
    def test_register_non_role_rejected(self, fresh_agent_registry):
        class NotARole:
            agent_name = "Nope"

        with pytest.raises(TypeError, match="must subclass Role"):
            fresh_agent_registry.register(NotARole)

    def test_register_role_subclass(self, fresh_agent_registry):
        from mote.runtime.agent.role import Role

        class MyAgent(Role):
            agent_name = "MyAgent"
            aliases = ["ma"]

        fresh_agent_registry.register(MyAgent)
        assert fresh_agent_registry.get("MyAgent") is MyAgent
        assert fresh_agent_registry.get("ma") is MyAgent

    def test_default_agent_name_from_classname(self, fresh_agent_registry):
        from mote.runtime.agent.role import Role

        class Defaulted(Role):
            pass

        fresh_agent_registry.register(Defaulted)
        assert Defaulted.agent_name == "Defaulted"
        assert fresh_agent_registry.get("Defaulted") is Defaulted

    def test_conflict_rejected(self, fresh_agent_registry):
        from mote.runtime.agent.role import Role

        class AgentA(Role):
            agent_name = "Shared"

        class AgentB(Role):
            agent_name = "Shared"

        fresh_agent_registry.register(AgentA)
        with pytest.raises(ValueError, match="already registered"):
            fresh_agent_registry.register(AgentB)

    def test_idempotent_reregister(self, fresh_agent_registry):
        from mote.runtime.agent.role import Role

        class AgentC(Role):
            agent_name = "C"
            aliases = ["c"]

        fresh_agent_registry.register(AgentC)
        fresh_agent_registry.register(AgentC)  # no raise
        assert fresh_agent_registry.get("c") is AgentC

    def test_all_agents_deduplicates(self, fresh_agent_registry):
        from mote.runtime.agent.role import Role

        class AgentD(Role):
            agent_name = "D"
            aliases = ["d1", "d2"]

        fresh_agent_registry.register(AgentD)
        assert fresh_agent_registry.all_agents() == {"D": AgentD}

    def test_get_unknown_returns_none(self, fresh_agent_registry):
        assert fresh_agent_registry.get("ghost") is None


# ---------------------------------------------------------------------------
# mcp_adapter
# ---------------------------------------------------------------------------


class _FakeMCP:
    def __init__(self):
        self.calls = []

    async def call_tool(self, name, kwargs):
        self.calls.append((name, kwargs))
        return f"{name}:{kwargs}"


@pytest.fixture
def mcp_schema():
    return {
        "name": "server:search",
        "description": "search the web",
        "parameters": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
    }


class TestMCPToolAdapter:
    def test_name_and_tool_schema(self, mcp_schema):
        adapter = MCPToolAdapter.from_discovery(_FakeMCP(), _discovered(mcp_schema))
        assert adapter.name == "server:search"
        definition = adapter.native_definition()
        assert definition.name == "server:search"
        assert definition.category == "mcp"

    @pytest.mark.asyncio
    async def test_call_delegates_to_mcp(self, mcp_schema):
        mcp = _FakeMCP()
        adapter = MCPToolAdapter.from_discovery(mcp, _discovered(mcp_schema))
        result = await adapter.call(q="mote")
        assert mcp.calls == [("server:search", {"q": "mote"})]
        assert "server:search" in result

    def test_native_schema_passes_through_parameters(self, mcp_schema):
        adapter = MCPToolAdapter.from_discovery(_FakeMCP(), _discovered(mcp_schema))
        native = adapter.native_definition().render(adapter)
        assert native["name"] == "server:search"
        assert native["description"] == "search the web"
        # MCP already publishes JSON Schema -> used directly as input_schema.
        assert native["input_schema"] == mcp_schema["parameters"]

    def test_native_schema_defaults_when_no_parameters(self):
        adapter = MCPToolAdapter.from_discovery(_FakeMCP(), DiscoveredMcpTool("bare", "", {}))
        native = adapter.native_definition().render(adapter)
        assert native["input_schema"] == {"type": "object", "properties": {}}
        assert native["description"] == ""

    def test_xml_definition_is_a_separate_explicit_adapter(self, mcp_schema):
        adapter = MCPToolAdapter.from_discovery(_FakeMCP(), _discovered(mcp_schema))
        xml = adapter.xml_definition()
        native = adapter.native_definition()
        assert type(xml) is not type(native)
        assert xml.render(adapter)["parameters"] == mcp_schema["parameters"]

    @pytest.mark.asyncio
    async def test_xml_mcp_decodes_scalar_strings_before_call(self):
        schema = {
            "name": "server:scale",
            "description": "scale a value",
            "parameters": {
                "type": "object",
                "properties": {
                    "count": {"type": "integer"},
                    "enabled": {"type": "boolean"},
                },
            },
        }
        mcp = _FakeMCP()
        adapter = MCPToolAdapter.from_discovery(mcp, _discovered(schema))
        executor = ToolExecutor("xml", command_protocol="xml")
        executor.register_xml_tool(adapter.xml_definition(), adapter)
        result = await executor.run_command("server:scale", {"count": "3", "enabled": "true"})
        assert result.success is True
        assert mcp.calls == [("server:scale", {"count": 3, "enabled": True})]

    def test_xml_mcp_rejects_nested_arguments_during_definition(self):
        adapter = MCPToolAdapter.from_discovery(
            _FakeMCP(),
            DiscoveredMcpTool(
                name="server:nested",
                description="",
                input_schema={
                    "type": "object",
                    "properties": {"items": {"type": "array", "items": {"type": "string"}}},
                },
            ),
        )
        with pytest.raises(McpXmlSchemaError, match="items.*array"):
            adapter.xml_definition()


def _discovered(schema):
    return DiscoveredMcpTool(
        name=schema["name"],
        description=schema.get("description", ""),
        input_schema=schema.get("parameters") or {},
    )


class _DiscoveryMCP(_FakeMCP):
    def __init__(self, tools):
        super().__init__()
        self._tools = tuple(tools)

    def discovered_tools(self):
        return self._tools


class TestMcpToolsets:
    def test_same_discovery_requires_explicit_protocol_toolset(self, mcp_schema):
        source = _DiscoveryMCP((_discovered(mcp_schema),))
        xml = XmlMcpToolset(source)
        native = NativeMcpToolset(source)

        xml_definition = xml.definitions()[0]
        native_definition = native.definitions()[0]
        assert xml_definition.protocol.value == "xml"
        assert native_definition.protocol.value == "native"
        assert type(xml_definition) is not type(native_definition)

    def test_xml_projection_rejects_nested_schema_without_blocking_native(self):
        source = _DiscoveryMCP(
            (
                DiscoveredMcpTool(
                    name="server:nested",
                    description="nested input",
                    input_schema={
                        "type": "object",
                        "properties": {"payload": {"type": "object", "properties": {}}},
                    },
                ),
            )
        )
        assert NativeMcpToolset(source).tool_names() == ("server:nested",)
        with pytest.raises(McpXmlSchemaError, match="payload.*object"):
            XmlMcpToolset(source)
