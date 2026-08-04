"""Role publishes commands and immutable queries, never its component graph."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ROLE = ROOT / "runtime/agent/role.py"


def _public_role_members() -> set[str]:
    tree = ast.parse(ROLE.read_text(encoding="utf-8"))
    role = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Role")
    return {
        node.name
        for node in role.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_")
    }


def test_role_does_not_publish_component_or_backend_objects() -> None:
    forbidden = {
        "components",
        "wiring",
        "context",
        "executor",
        "context_manager",
        "session_log",
        "file_operations",
        "artifact_store",
        "artifact_resolver",
        "artifact_publisher",
        "runtime_host",
        "browser_profile_store",
        "resource_registry",
        "hook_manager",
        "lsp_service",
        "sandbox_runtime",
        "diagnostics_buffer",
        "file_watch_service",
        "turn_context_bus",
        "command_channel",
        "context_provider",
    }
    assert _public_role_members().isdisjoint(forbidden)


def test_production_consumers_do_not_reach_through_role_components() -> None:
    violations: list[str] = []
    for package in ("product", "orchestration", "kernel"):
        for path in (ROOT / package).rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
            if any(
                isinstance(node, ast.Attribute)
                and node.attr in {"components", "_components", "wiring", "context"}
                and isinstance(node.value, ast.Name)
                and node.value.id == "role"
                for node in ast.walk(tree)
            ):
                violations.append(str(path.relative_to(ROOT)))
    assert violations == []


def test_product_consumers_use_typed_role_commands_for_history_and_routing() -> None:
    backend = (ROOT / "product/entrypoints/cli/backend.py").read_text(encoding="utf-8")
    agent_tool = (ROOT / "product/toolsets/builtin/agent_tool.py").read_text(encoding="utf-8")
    assert "role.context_manager" not in backend
    assert "role.context" not in backend
    assert "role.wiring" not in backend
    assert "role.file_operations" not in backend
    assert "role.router" not in agent_tool
    assert "role.command_channel" not in agent_tool
    assert "role.clear_history()" in backend
    assert "role.delete_history_units(anchor_ids)" in backend
