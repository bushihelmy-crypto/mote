from __future__ import annotations

import ast
from pathlib import Path

import pytest

from mote.contracts.tools import ToolsetProtocolError
from mote.kernel.tools.toolset import NativeToolset, XmlToolset, validate_toolset_protocols

PACKAGE_ROOT = Path(__file__).resolve().parents[2]


def test_agent_composition_rejects_wrong_protocol_toolset() -> None:
    with pytest.raises(ToolsetProtocolError):
        validate_toolset_protocols("xml", (NativeToolset("native", ()),))
    with pytest.raises(ToolsetProtocolError):
        validate_toolset_protocols("native", (XmlToolset("xml", ()),))


def test_execution_capabilities_have_no_wire_schema_methods() -> None:
    forbidden = {"tool_schema", "native_schema", "get_schema", "get_native_schema"}
    violations: list[str] = []
    for path in (PACKAGE_ROOT / "runtime" / "tools").rglob("*.py"):
        if path.name == "definitions.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in forbidden:
                violations.append(f"{path.relative_to(PACKAGE_ROOT)}:{node.lineno}:{node.name}")
    assert violations == []


def test_ambiguous_function_and_presentation_types_are_deleted() -> None:
    assert not (PACKAGE_ROOT / "runtime" / "tools" / "tool_presentation.py").exists()
    source = (PACKAGE_ROOT / "runtime" / "tools" / "function_toolset.py").read_text(encoding="utf-8")
    assert "class FunctionToolset" not in source
