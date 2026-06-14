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

from metagpt.executor.agent_registry import AgentRegistry
from metagpt.executor.mcp_adapter import MCPToolAdapter
from metagpt.executor.tool_convert import (
    convert_code_to_tool_schema,
    convert_code_to_tool_schema_ast,
    function_docstring_to_schema,
)

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


class TestConvertCode:
    def test_function_object(self):
        def fn(x: str) -> str:
            """Echo."""
            return x

        schema = convert_code_to_tool_schema(fn)
        assert schema["type"] == "function"
        assert schema["description"] == "Echo."

    def test_class_object_collects_public_methods(self):
        class Calc:
            """A calculator."""

            def add(self, a: int, b: int) -> int:
                """Add.

                Args:
                    a: x.
                    b: y.
                """
                return a + b

            def _private(self):  # excluded
                ...

        schema = convert_code_to_tool_schema(Calc)
        assert schema["type"] == "class"
        assert "add" in schema["methods"]
        assert "_private" not in schema["methods"]

    def test_ast_parsing(self):
        code = (
            "def mul(a: int, b: int) -> int:\n"
            '    """Multiply.\n\n'
            "    Args:\n"
            "        a: first.\n"
            "        b: second.\n"
            '    """\n'
            "    return a * b\n"
        )
        schemas = convert_code_to_tool_schema_ast(code)
        assert "mul" in schemas
        assert schemas["mul"]["type"] == "function"
        assert schemas["mul"]["signature"] == "(a: int, b: int) -> int"
        # AST path additionally captures the source.
        assert "code" in schemas["mul"]

    def test_ast_async_function(self):
        code = "async def go(x: str) -> str:\n    'Doc.'\n    return x\n"
        schemas = convert_code_to_tool_schema_ast(code)
        assert schemas["go"]["type"] == "async_function"


# ---------------------------------------------------------------------------
# agent_registry
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_agent_registry() -> AgentRegistry:
    """An isolated AgentRegistry bypassing the Singleton cache."""
    reg = AgentRegistry.__new__(AgentRegistry)
    reg._registry = {}
    return reg


class TestAgentRegistry:
    def test_register_non_role_rejected(self, fresh_agent_registry):
        class NotARole:
            agent_name = "Nope"

        with pytest.raises(TypeError, match="must subclass Role"):
            fresh_agent_registry.register(NotARole)

    def test_register_role_subclass(self, fresh_agent_registry):
        from metagpt.roles.role import Role

        class MyAgent(Role):
            agent_name = "MyAgent"
            aliases = ["ma"]

        fresh_agent_registry.register(MyAgent)
        assert fresh_agent_registry.get("MyAgent") is MyAgent
        assert fresh_agent_registry.get("ma") is MyAgent

    def test_default_agent_name_from_classname(self, fresh_agent_registry):
        from metagpt.roles.role import Role

        class Defaulted(Role):
            pass

        fresh_agent_registry.register(Defaulted)
        assert Defaulted.agent_name == "Defaulted"
        assert fresh_agent_registry.get("Defaulted") is Defaulted

    def test_conflict_rejected(self, fresh_agent_registry):
        from metagpt.roles.role import Role

        class AgentA(Role):
            agent_name = "Shared"

        class AgentB(Role):
            agent_name = "Shared"

        fresh_agent_registry.register(AgentA)
        with pytest.raises(ValueError, match="already registered"):
            fresh_agent_registry.register(AgentB)

    def test_idempotent_reregister(self, fresh_agent_registry):
        from metagpt.roles.role import Role

        class AgentC(Role):
            agent_name = "C"
            aliases = ["c"]

        fresh_agent_registry.register(AgentC)
        fresh_agent_registry.register(AgentC)  # no raise
        assert fresh_agent_registry.get("c") is AgentC

    def test_all_agents_deduplicates(self, fresh_agent_registry):
        from metagpt.roles.role import Role

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
        adapter = MCPToolAdapter(mcp=None, tool_name="server:search", schema=mcp_schema)
        assert adapter.name == "server:search"
        assert adapter.tool_schema() == mcp_schema

    @pytest.mark.asyncio
    async def test_call_delegates_to_mcp(self, mcp_schema):
        mcp = _FakeMCP()
        adapter = MCPToolAdapter(mcp=mcp, tool_name="server:search", schema=mcp_schema)
        result = await adapter.call(q="metagpt")
        assert mcp.calls == [("server:search", {"q": "metagpt"})]
        assert "server:search" in result

    def test_native_schema_passes_through_parameters(self, mcp_schema):
        adapter = MCPToolAdapter(mcp=None, tool_name="server:search", schema=mcp_schema)
        native = adapter.native_schema()
        assert native["name"] == "server:search"
        assert native["description"] == "search the web"
        # MCP already publishes JSON Schema -> used directly as input_schema.
        assert native["input_schema"] == mcp_schema["parameters"]

    def test_native_schema_defaults_when_no_parameters(self):
        adapter = MCPToolAdapter(mcp=None, tool_name="bare", schema={"name": "bare"})
        native = adapter.native_schema()
        assert native["input_schema"] == {"type": "object", "properties": {}}
        assert native["description"] == ""
