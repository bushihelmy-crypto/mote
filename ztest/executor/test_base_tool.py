#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for ``mote.executor.base_tool.BaseTool``.

Covers binding/capability injection (the narrow allowlist), schema generation
(auto + custom), and the native-schema path.
"""
from __future__ import annotations

import pytest

from mote.common.schema import DEFAULT_MAX_RESULT_SIZE_CHARS, ToolEffect
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

        desc = Described.get_schema()["description"]
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

        assert Described.summary() == "One-line summary."

    def test_summary_tracks_custom_schema_description(self):
        # A dynamic-description tool's menu tracks its custom_schema description's
        # first line, not the raw docstring.
        class Dyn(BaseTool):
            name = "Dyn"

            @classmethod
            def custom_schema(cls):
                return {"name": "Dyn", "description": "Live blurb.\nMore detail.", "parameters": {}}

            async def call(self):  # pragma: no cover
                """Ignored docstring."""
                return "ok"

        assert Dyn.summary() == "Live blurb."

    def test_search_text_appends_keywords(self):
        # search_text() = summary + recall keywords (the SEARCH corpus). The
        # keywords never touch summary()/get_schema() — they exist only here.
        class Kw(BaseTool):
            name = "Kw"
            keywords = ["synonym", "别名"]

            async def call(self):  # pragma: no cover
                """Do a thing."""
                return "ok"

        assert Kw.summary() == "Do a thing."  # menu/wire unaffected
        assert "synonym" not in Kw.get_schema()["description"]  # never on the wire
        assert Kw.search_text() == "Do a thing. synonym 别名"

    def test_search_text_no_keywords_equals_summary(self):
        # No keywords → search corpus is just the summary (no trailing space/join).
        class NoKw(BaseTool):
            name = "NoKw"

            async def call(self):  # pragma: no cover
                """Only a summary."""
                return "ok"

        assert NoKw.search_text() == NoKw.summary() == "Only a summary."

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
        from mote.executor.tools.glob import Glob
        from mote.executor.tools.grep import Grep
        from mote.executor.tools.read import Read

        assert Read.resolve_effect() is ToolEffect.PURE
        assert Grep.resolve_effect() is ToolEffect.PURE
        assert Glob.resolve_effect() is ToolEffect.PURE

    def test_external_tools_derive_external(self):
        from mote.executor.tools.bash import Bash

        assert Bash.resolve_effect() is ToolEffect.EXTERNAL


class TestMisc:
    def test_default_result_cap(self):
        assert EchoTool.max_result_size_chars == DEFAULT_MAX_RESULT_SIZE_CHARS

    def test_cleanup_session_is_noop(self):
        tool = EchoTool()
        # Default cleanup does nothing and must not raise.
        assert tool.cleanup_session("sess") is None
