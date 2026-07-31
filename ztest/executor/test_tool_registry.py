"""Unit tests for immutable Application-owned tool catalogs."""

from __future__ import annotations

import pytest

from mote.runtime.tools.base_tool import BaseTool
from mote.runtime.tools.tool_registry import ToolCatalog


def _tool(name: str = "", aliases=()):
    class Tool(BaseTool):
        async def call(self, *, action: str = ""):
            return "ok"

    Tool.name = name
    Tool.aliases = aliases
    return Tool


def test_catalog_is_immutable_and_aliases_resolve() -> None:
    alpha = _tool("Alpha", ("a",))
    catalog = ToolCatalog.from_types((alpha,))
    assert catalog.get("Alpha") is alpha
    assert catalog.get("a") is alpha
    assert catalog.get("missing") is None
    assert catalog.all_tools() == {"Alpha": alpha}


def test_with_types_returns_a_new_content_addressed_snapshot() -> None:
    alpha = _tool("Alpha")
    beta = _tool("Beta")
    original = ToolCatalog.from_types((alpha,))
    extended = original.with_types(beta)
    assert original.get("Beta") is None
    assert extended.all_tools() == {"Alpha": alpha, "Beta": beta}
    assert original.version != extended.version


def test_duplicate_primary_or_dispatch_name_is_rejected() -> None:
    with pytest.raises(ValueError, match="declared more than once"):
        ToolCatalog.from_types((_tool("Dup"), _tool("Dup")))
    with pytest.raises(ValueError, match="dispatch name"):
        ToolCatalog.from_types((_tool("First", ("shared",)), _tool("Second", ("shared",))))


def test_stateful_tool_contract_is_validated_at_snapshot_creation() -> None:
    invalid = _tool("Stateful")
    invalid.stateful = True
    with pytest.raises(TypeError, match="get_runtime_host"):
        ToolCatalog.from_types((invalid,))


def test_stateful_tool_requires_handoff_and_action() -> None:
    missing_handoff = _tool("MissingHandoff")
    missing_handoff.stateful = True
    missing_handoff.requires = ("get_runtime_host",)
    with pytest.raises(TypeError, match="handoff_runtime"):
        ToolCatalog.from_types((missing_handoff,))

    class MissingAction(BaseTool):
        name = "MissingAction"
        stateful = True
        requires = ("get_runtime_host", "handoff_runtime")

        async def call(self):
            return "ok"

    with pytest.raises(TypeError, match="action parameter"):
        ToolCatalog.from_types((MissingAction,))
