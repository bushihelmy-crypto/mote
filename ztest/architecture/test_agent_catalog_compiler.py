from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CATALOG = ROOT / "product" / "agents" / "catalog.py"
DISCOVERY = ROOT / "product" / "agents" / "discovery.py"


def test_catalog_version_has_one_non_reflective_compiler_owner():
    source = CATALOG.read_text(encoding="utf-8")
    tree = ast.parse(source)

    assert "inspect" not in source
    hash_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "sha256"
    ]
    assert len(hash_calls) == 1
    assert "def compile_agent_catalog(" in source


def test_builtin_discovery_cannot_assemble_private_snapshots_or_versions():
    source = DISCOVERY.read_text(encoding="utf-8")
    assert "._definitions" not in source
    assert "version=" not in source
    assert "AgentCatalog(" not in source
    assert "AgentCatalog.from_types(" in source


def test_lookup_uses_compiled_namespace_owner_map():
    source = CATALOG.read_text(encoding="utf-8")
    assert "return self._namespace.get(name)" in source
    assert "for definition in self._definitions" not in source.split("def get(", 1)[1].split("def agent_type(", 1)[0]
