#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for ``mote.runtime.tools.base_tool.BaseTool``.

Covers binding/capability injection (the narrow allowlist), schema generation
(auto + custom), and the native-schema path.
"""
from __future__ import annotations

import pytest

from mote.contracts.config.tool import DEFAULT_MAX_RESULT_SIZE_CHARS
from mote.contracts.tool.effects import ToolEffect
from mote.runtime.tools.base_tool import BaseTool
from mote.runtime.tools.definitions import native_definition, xml_definition

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
    def test_xml_definition_renders_signature(self):
        definition = xml_definition(EchoTool)
        schema = definition.render(EchoTool())
        assert schema["name"] == "Echo"
        # description falls back to the call() docstring (not the class docstring).
        assert "Return" in schema["description"]
        # The call contract does not duplicate the top-level description or
        # expose Python's sync/async implementation detail.
        assert "description" not in schema["parameters"]
        assert "type" not in schema["parameters"]
        assert "text" in schema["parameters"]["parameters"]
        assert "text" in schema["parameters"]["signature"]

    def test_description_is_docstring_body_without_args(self):
        # Docstring-native: the description is the docstring BODY (summary line +
        # manual) with the ``Args:`` section dropped (params travel separately).
        class Described(BaseTool):
            name = "Described"

            async def call(self, x: str):  # pragma: no cover
                """One-line summary.

                A fuller operating manual paragraph.

                Args:
                    x: should not leak into the description.
                """
                return "ok"

        desc = xml_definition(Described).description
        assert desc.startswith("One-line summary.")
        assert "fuller operating manual" in desc
        assert "should not leak" not in desc  # Args: stripped from the wire prose

    def test_summary_is_docstring_first_line(self):
        # The one-line MENU entry is the first docstring line.
        class Described(BaseTool):
            name = "Described"

            async def call(self, x: str):  # pragma: no cover
                """One-line summary.

                Body paragraph.
                """
                return "ok"

        assert xml_definition(Described).summary == "One-line summary."

    def test_summary_tracks_custom_schema_description(self):
        # A dynamic-description tool's menu tracks its custom_schema description's
        # first line, not the raw docstring.
        class Dyn(BaseTool):
            name = "Dyn"

            @classmethod
            def model_description(cls):
                return "Live blurb.\nMore detail."

            async def call(self):  # pragma: no cover
                """Ignored docstring."""
                return "ok"

        assert xml_definition(Dyn).summary == "Live blurb."

    def test_search_text_appends_keywords(self):
        # search_text() = summary + recall keywords (the SEARCH corpus). The
        # keywords never touch summary()/get_schema() — they exist only here.
        class Kw(BaseTool):
            name = "Kw"
            keywords = ["synonym", "别名"]

            async def call(self):  # pragma: no cover
                """Do a thing."""
                return "ok"

        definition = xml_definition(Kw)
        assert definition.summary == "Do a thing."
        assert "synonym" not in definition.description
        assert definition.search_text == "Do a thing. synonym 别名"

    def test_search_text_no_keywords_equals_summary(self):
        # No keywords → search corpus is just the summary (no trailing space/join).
        class NoKw(BaseTool):
            name = "NoKw"

            async def call(self):  # pragma: no cover
                """Only a summary."""
                return "ok"

        definition = xml_definition(NoKw)
        assert definition.search_text == definition.summary == "Only a summary."

    def test_execution_capability_has_no_wire_schema_surface(self):
        tool = AddTool()
        assert not hasattr(tool, "tool_schema")
        assert not hasattr(tool, "native_schema")


class TestNativeSchema:
    def test_native_definition_shape(self):
        native = native_definition(AddTool).render(AddTool())
        assert native["name"] == "Add"
        assert "description" in native
        input_schema = native["input_schema"]
        assert input_schema["type"] == "object"
        assert set(input_schema["properties"]) == {"a", "b"}
        # `a` has no default -> required; `b` defaults to 0 -> optional.
        assert input_schema["required"] == ["a"]

    def test_native_and_xml_definitions_are_distinct_types(self):
        assert type(native_definition(AddTool)) is not type(xml_definition(AddTool))


class TestResolveEffect:
    def test_default_is_external(self):
        # A plain tool declares neither mutates_filesystem nor an explicit
        # effect -> conservative EXTERNAL (guarded, not silently replayed).
        assert EchoTool.resolve_effect() is ToolEffect.EXTERNAL

    def test_filesystem_mutation_derives_local(self):
        class FsTool(BaseTool):
            name = "Fs"
            mutates_filesystem = True

            async def call(self, **kwargs):
                return ""

        assert FsTool.resolve_effect() is ToolEffect.LOCAL

    def test_reconstructable_alone_does_not_imply_pure(self):
        # reconstructable is a compaction concern, NOT side-effect-free:
        # a reconstructable non-fs tool still derives EXTERNAL (cf. Bash).
        class ReconTool(BaseTool):
            name = "Recon"
            reconstructable = True

            async def call(self, **kwargs):
                return ""

        assert ReconTool.resolve_effect() is ToolEffect.EXTERNAL

    def test_explicit_effect_wins_over_derivation(self):
        class PureButFs(BaseTool):
            name = "PureButFs"
            mutates_filesystem = True
            effect = ToolEffect.PURE

        assert PureButFs.resolve_effect() is ToolEffect.PURE

    def test_read_tools_are_pure(self):
        from mote.product.toolsets.builtin.read import Read
        from mote.product.toolsets.builtin.search import Search

        assert Read.resolve_effect() is ToolEffect.PURE
        assert Search.resolve_effect() is ToolEffect.PURE

    def test_external_tools_derive_external(self):
        from mote.product.toolsets.builtin.bash import Bash

        assert Bash.resolve_effect() is ToolEffect.EXTERNAL


class TestMisc:
    def test_default_result_cap(self):
        assert EchoTool.max_result_size_chars == DEFAULT_MAX_RESULT_SIZE_CHARS

    def test_cleanup_session_is_noop(self):
        tool = EchoTool()
        # Default cleanup does nothing and must not raise.
        assert tool.cleanup_session("sess") is None
