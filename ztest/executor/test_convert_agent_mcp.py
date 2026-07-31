#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for the smaller executor helpers:

- ``tool_convert`` — function/AST -> tool schema.
- immutable spawnable-agent catalog.
- ``mcp_adapter`` — wrap a discovered MCP tool as a BaseTool.

NOTE: deliberately NO ``from __future__ import annotations`` here — these tests
assert the rendered ``inspect.signature`` of locally-defined functions, which
would be stringized (``'int'`` instead of ``int``) under PEP 563.
"""

import pytest

from mote.product.agents.catalog import AgentCatalog
from mote.runtime.tools.mcp.adapter import MCPToolAdapter, McpXmlSchemaError
from mote.runtime.tools.mcp.toolsets import NativeMcpToolset, XmlMcpToolset
from mote.runtime.tools.mcp.types import DiscoveredMcpTool
from mote.runtime.tools.tool_convert import function_docstring_to_schema
from mote.runtime.tools.tool_executor import ToolExecutor


class _FakeAgentBuilder:
    def build(self, request):
        raise AssertionError("catalog lookup must not construct an agent")


class _FakeAgentFactory:
    def child_builder(self, agent_cls):
        return _FakeAgentBuilder()


# ---------------------------------------------------------------------------
# tool_convert
# ---------------------------------------------------------------------------


class TestFunctionDocstringToSchema:
    def test_schema_contains_only_call_contract(self):
        def fn(x: str) -> str:
            """One liner."""
            return x

        schema = function_docstring_to_schema(fn, fn.__doc__)
        assert set(schema) == {"signature", "parameters"}
        assert schema["signature"] == "(x: str) -> str"

    def test_async_function_signature_and_parameters(self):
        async def fn(a: int, b: str = "z") -> str:
            """Does a thing.

            Args:
                a: the a.
                b: the b.
            """
            return ""

        schema = function_docstring_to_schema(fn, fn.__doc__)
        assert schema["signature"] == "(a: int, b: str = 'z') -> str"
        assert "the a." in schema["parameters"]

    def test_empty_docstring_yields_empty_params(self):
        def fn(x): ...

        schema = function_docstring_to_schema(fn, "")
        assert schema["parameters"] == ""


# ---------------------------------------------------------------------------
# agent catalog
# ---------------------------------------------------------------------------


class TestAgentCatalog:
    def test_non_role_rejected(self):
        class NotARole:
            agent_name = "Nope"

        with pytest.raises(TypeError, match="must subclass BaseRole"):
            AgentCatalog.from_types((NotARole,), _FakeAgentFactory())

    def test_role_subclass_and_alias_resolve(self):
        from mote.runtime.agent.role import Role

        class MyAgent(Role):
            agent_name = "MyAgent"
            aliases = ["ma"]

        catalog = AgentCatalog.from_types((MyAgent,), _FakeAgentFactory())
        assert catalog.get("MyAgent").name == "MyAgent"
        assert catalog.get("ma").name == "MyAgent"
        assert catalog.agent_type("ma") is MyAgent
        assert catalog.get("ghost") is None


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
        "parameters": {
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "required": ["q"],
        },
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
