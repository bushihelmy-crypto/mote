from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_tool_identity_has_one_non_reflective_hash_owner():
    compiler = (ROOT / "runtime/tools/definition_compiler.py").read_text(encoding="utf-8")
    registry = (ROOT / "runtime/tools/tool_registry.py").read_text(encoding="utf-8")
    snapshots = (ROOT / "runtime/tools/snapshots.py").read_text(encoding="utf-8")

    assert "hashlib.sha256" in compiler
    assert "hashlib" not in registry
    assert "hashlib" not in snapshots
    assert "inspect.getsource" not in registry
    assert "definition_version" not in snapshots


def test_materialization_consumes_bound_semantic_identity_without_fixed_versions():
    snapshots = (ROOT / "runtime/tools/snapshots.py").read_text(encoding="utf-8")
    binding = (ROOT / "runtime/tools/tool_binding.py").read_text(encoding="utf-8")
    mcp = (ROOT / "runtime/tools/mcp/toolsets.py").read_text(encoding="utf-8")

    assert "compiled = tool.compiled_definition" in snapshots
    assert 'ToolCatalogIdentity("runtime-tools", fingerprint)' in snapshots
    assert "runtime-tools@1" not in snapshots
    assert "runtime-tool-provider@1" not in snapshots
    assert "def semantic_identity" in binding
    assert 'version: str = "1"' not in mcp


def test_every_definition_creator_declares_source_identity():
    for relative in (
        "runtime/tools/definitions.py",
        "runtime/tools/function_toolset.py",
        "runtime/tools/mcp/adapter.py",
    ):
        source = (ROOT / relative).read_text(encoding="utf-8")
        constructors = source.count("XmlToolDefinition(") + source.count("NativeToolDefinition(")
        assert source.count("source_identity=") == constructors
