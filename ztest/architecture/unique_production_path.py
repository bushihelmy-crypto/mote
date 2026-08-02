"""Exact source gate for migration debt and alternate production paths."""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_ROOTS = ("contracts", "kernel", "runtime", "orchestration", "product")
FORBIDDEN_NAMES = {
    "register_agent",
    "register_tool",
    "declared_agent_catalog",
    "declared_tool_catalog",
    "ToolRegistry",
}
APPROVED_DYNAMIC_PLUGIN_BOUNDARIES: set[str] = set()


def _migration_debt():
    helper_spec = importlib.util.spec_from_file_location(
        "mote_governance_artifact", ROOT / "ztest/architecture/governance_artifact.py"
    )
    if helper_spec is None or helper_spec.loader is None:
        raise RuntimeError("governance artifact loader is unavailable")
    helper = importlib.util.module_from_spec(helper_spec)
    helper_spec.loader.exec_module(helper)
    helper._modules()
    helper._load("mote.contracts.events.governance", "contracts/events/governance.py")
    helper._load("mote.contracts.ports", "contracts/ports/__init__.py")
    helper._load("mote.contracts.ports.events", "contracts/ports/events/__init__.py")
    helper._load("mote.contracts.ports.events.journal", "contracts/ports/events/journal.py")
    helper._load("mote.contracts.events.file", "contracts/events/file/__init__.py")
    helper._load("mote.contracts.events.file.facts", "contracts/events/file/facts.py")
    helper._load("mote.runtime.session.events", "runtime/session/events.py")
    helper._load("mote.runtime.session.codec", "runtime/session/codec.py")
    helper._load(
        "mote.product.inference.daemon.operations_audit_codec",
        "product/inference/daemon/operations_audit_codec.py",
    )
    helper._load("mote.product.inference.backends.sqlite", "product/inference/backends/sqlite.py")
    stores = helper._load("mote.product.composition.store_governance", "product/composition/store_governance.py")
    return stores.MIGRATION_DEBT_DECLARATIONS


def main() -> int:
    violations: list[str] = []
    if _migration_debt():
        violations.append("typed production migration debt is not empty")
    for root in PRODUCTION_ROOTS:
        for path in (ROOT / root).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            relative = path.relative_to(ROOT).as_posix()
            for node in ast.walk(tree):
                if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
                    violations.append(f"{relative}:{node.lineno}: legacy global path {node.id}")
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "__getattr__":
                    violations.append(f"{relative}:{node.lineno}: dynamic export path")
                if isinstance(node, ast.Call):
                    name = (
                        node.func.id
                        if isinstance(node.func, ast.Name)
                        else (node.func.attr if isinstance(node.func, ast.Attribute) else "")
                    )
                    if (
                        name in {"walk_packages", "import_module"}
                        and relative not in APPROVED_DYNAMIC_PLUGIN_BOUNDARIES
                    ):
                        violations.append(f"{relative}:{node.lineno}: dynamic discovery {name}")
    if (ROOT / "product/agents/registry.py").exists():
        violations.append("retired Agent registry source still exists")
    if violations:
        print("\n".join(sorted(violations)), file=sys.stderr)
        return 1
    print("typed migration debt and alternate production paths are zero")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
