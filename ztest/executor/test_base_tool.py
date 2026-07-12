#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for ``mote.executor.base_tool.BaseTool``.

Covers binding/capability injection (the narrow allowlist), schema generation
(auto + custom), and the native-schema path.
"""
from __future__ import annotations

import pytest

from mote.common.schema import DEFAULT_MAX_RESULT_SIZE_CHARS
from mote.executor.base_tool import BaseTool

from .conftest import AddTool, CapTool, EchoTool, FakeRole


class TestBind:
    def test_bind_sets_session_id_and_returns_self(self):
        tool = EchoTool()
        assert tool.session_id == ""
        returned = tool.bind("sess-1")
        assert returned is tool
        assert tool.session_id == "sess-1"

    def test_bind_without_role_skips_capability_injection(self):
        # No role => `requires` are not resolved; binding still succeeds.
        tool = CapTool()
        tool.bind("sess")
        assert tool.session_id == "sess"

    def test_capability_injected_from_allowlist(self):
        role = FakeRole({"greet": lambda: "hello"})
        tool = CapTool()
        tool.bind("sess", role=role)
        assert tool.greet() == "hello"

    def test_requires_not_in_allowlist_raises(self):
        # Role publishes no `greet` capability -> bind must reject it.
        role = FakeRole({"other": lambda: None})
        tool = CapTool()
        with pytest.raises(AttributeError, match="not a capability published"):
            tool.bind("sess", role=role)

    def test_only_declared_capabilities_are_injected(self):
        # A capability the tool does not require is never set on it.
        role = FakeRole({"greet": lambda: "hi", "secret": lambda: "no"})
        tool = CapTool()
        tool.bind("sess", role=role)
        assert not hasattr(tool, "secret")


class TestSchema:
    def test_get_schema_auto_from_signature(self):
        schema = EchoTool.get_schema()
        assert schema["name"] == "Echo"
        # description falls back to the call() docstring (not the class docstring).
        assert "Return" in schema["description"]
        # parameters is the full docstring-derived sub-schema (type/signature/parameters).
        assert schema["parameters"]["type"] == "async_function"
        assert "text" in schema["parameters"]["parameters"]
        assert "text" in schema["parameters"]["signature"]

    def test_description_override_takes_precedence(self):
        class Described(BaseTool):
            name = "Described"
            description = "Custom description here."

            async def call(self):  # pragma: no cover
                """Docstring line that should be ignored."""
                return "ok"

        assert Described.get_schema()["description"] == "Custom description here."

    def test_custom_schema_short_circuits(self):
        custom = {"name": "X", "description": "d", "parameters": "p"}

        class CustomSchemaTool(BaseTool):
            name = "X"

            @classmethod
            def custom_schema(cls):
                return custom

            async def call(self):  # pragma: no cover
                return "ok"

        assert CustomSchemaTool.get_schema() == custom

    def test_tool_schema_delegates_to_get_schema(self):
        tool = AddTool()
        assert tool.tool_schema() == AddTool.get_schema()


class TestNativeSchema:
    def test_get_native_schema_shape(self):
        native = AddTool.get_native_schema()
        assert native["name"] == "Add"
        assert "description" in native
        input_schema = native["input_schema"]
        assert input_schema["type"] == "object"
        assert set(input_schema["properties"]) == {"a", "b"}
        # `a` has no default -> required; `b` defaults to 0 -> optional.
        assert input_schema["required"] == ["a"]

    def test_native_schema_instance_delegates_to_class(self):
        tool = AddTool()
        assert tool.native_schema() == AddTool.get_native_schema()


class TestMisc:
    def test_default_result_cap(self):
        assert EchoTool.max_result_size_chars == DEFAULT_MAX_RESULT_SIZE_CHARS

    def test_cleanup_session_is_noop(self):
        tool = EchoTool()
        # Default cleanup does nothing and must not raise.
        assert tool.cleanup_session("sess") is None
