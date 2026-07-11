#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for ``mote.executor.tool_registry.ToolRegistry``.

Uses the ``fresh_registry`` fixture (an isolated instance built via ``__new__``)
so the process-wide singleton is never mutated.
"""
from __future__ import annotations

import pytest

from mote.executor.base_tool import BaseTool


def _tool(name: str = "", aliases=None):
    """Build a throwaway BaseTool subclass (NOT auto-registered)."""

    class _T(BaseTool):
        async def call(self):  # pragma: no cover - never invoked
            return "ok"

    _T.name = name
    _T.aliases = aliases or []
    return _T


class TestRegister:
    def test_register_by_explicit_name(self, fresh_registry):
        cls = _tool("Alpha")
        fresh_registry.register(cls)
        assert fresh_registry.get("Alpha") is cls

    def test_register_defaults_name_to_classname(self, fresh_registry):
        class Beta(BaseTool):
            async def call(self):  # pragma: no cover
                return "ok"

        fresh_registry.register(Beta)
        # name was empty -> resolved to the class name, and set back on the class.
        assert Beta.name == "Beta"
        assert fresh_registry.get("Beta") is Beta

    def test_register_returns_the_class(self, fresh_registry):
        cls = _tool("Gamma")
        assert fresh_registry.register(cls) is cls

    def test_aliases_are_registered(self, fresh_registry):
        cls = _tool("Delta", aliases=["d", "delta.run"])
        fresh_registry.register(cls)
        assert fresh_registry.get("Delta") is cls
        assert fresh_registry.get("d") is cls
        assert fresh_registry.get("delta.run") is cls


class TestFrozenMethods:
    def test_overriding_bind_is_rejected(self, fresh_registry):
        class BadBind(BaseTool):
            name = "BadBind"

            def bind(self, session_id, role=None):  # noqa: D401 - illegal override
                return self

            async def call(self):  # pragma: no cover
                return "ok"

        with pytest.raises(TypeError, match="must not override 'bind'"):
            fresh_registry.register(BadBind)

    def test_overriding_session_id_is_rejected(self, fresh_registry):
        class BadSession(BaseTool):
            name = "BadSession"

            @property
            def session_id(self):  # illegal override
                return "x"

            async def call(self):  # pragma: no cover
                return "ok"

        with pytest.raises(TypeError, match="must not override 'session_id'"):
            fresh_registry.register(BadSession)


class TestConflicts:
    def test_duplicate_name_different_class_rejected(self, fresh_registry):
        fresh_registry.register(_tool("Dup"))
        with pytest.raises(ValueError, match="already registered"):
            fresh_registry.register(_tool("Dup"))

    def test_alias_collision_with_other_tool_rejected(self, fresh_registry):
        fresh_registry.register(_tool("First", aliases=["shared"]))
        with pytest.raises(ValueError, match="already registered"):
            fresh_registry.register(_tool("Second", aliases=["shared"]))

    def test_reregistering_same_class_is_idempotent(self, fresh_registry):
        cls = _tool("Same", aliases=["s"])
        fresh_registry.register(cls)
        # Re-running registration (as discover() re-import would) must not raise.
        fresh_registry.register(cls)
        assert fresh_registry.get("Same") is cls
        assert fresh_registry.get("s") is cls


class TestLookup:
    def test_get_unknown_returns_none(self, fresh_registry):
        assert fresh_registry.get("missing") is None

    def test_all_tools_deduplicates_aliases(self, fresh_registry):
        cls = _tool("Solo", aliases=["a", "b"])
        fresh_registry.register(cls)
        tools = fresh_registry.all_tools()
        # Registered under 3 keys, but all_tools() returns one entry keyed by primary name.
        assert tools == {"Solo": cls}

    def test_all_tools_returns_each_class_once(self, fresh_registry):
        c1 = _tool("One", aliases=["1"])
        c2 = _tool("Two")
        fresh_registry.register(c1)
        fresh_registry.register(c2)
        assert set(fresh_registry.all_tools().values()) == {c1, c2}

    def test_all_names_lists_primary_and_aliases(self, fresh_registry):
        cls = _tool("Main", aliases=["m1", "m2"])
        fresh_registry.register(cls)
        assert fresh_registry.all_names(cls) == ["Main", "m1", "m2"]

    def test_all_names_falls_back_to_classname(self, fresh_registry):
        class NoName(BaseTool):
            async def call(self):  # pragma: no cover
                return "ok"

        # Not registered (so name stays empty) -> all_names uses __name__.
        assert NoName.name == ""
        assert fresh_registry.all_names(NoName) == ["NoName"]
