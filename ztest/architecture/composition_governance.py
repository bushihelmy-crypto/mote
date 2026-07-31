"""Isolated Product composition closure without importing package facades."""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_ROOTS = ("contracts", "kernel", "runtime", "orchestration", "product")
GOVERNED_EXPORT_PACKAGES = (
    "runtime/__init__.py",
    "runtime/agent/__init__.py",
    "runtime/artifacts/__init__.py",
    "runtime/durable/__init__.py",
    "runtime/events/__init__.py",
    "runtime/inference/__init__.py",
    "runtime/models/routing/__init__.py",
    "runtime/prompt/__init__.py",
    "orchestration/agents/__init__.py",
    "orchestration/automation/cron/__init__.py",
    "orchestration/background_tasks/__init__.py",
    "orchestration/workflows/__init__.py",
)


def _governance():
    helper_spec = importlib.util.spec_from_file_location(
        "mote_governance_artifact", ROOT / "ztest/architecture/governance_artifact.py"
    )
    if helper_spec is None or helper_spec.loader is None:
        raise RuntimeError("governance artifact loader is unavailable")
    helper = importlib.util.module_from_spec(helper_spec)
    helper_spec.loader.exec_module(helper)
    helper._modules()
    helper._load("mote.contracts.events.governance", "contracts/events/governance.py")
    return helper._load("mote.product.composition.governance", "product/composition/governance.py")


def _literal_all(tree: ast.Module) -> set[str]:
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets)
            and isinstance(node.value, (ast.List, ast.Tuple))
        ):
            return {
                item.value for item in node.value.elts if isinstance(item, ast.Constant) and isinstance(item.value, str)
            }
    return set()


def _factory_sources(governance) -> set[str]:
    path = ROOT / "product/composition/governance.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    assignment = next(
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "CAPABILITY_DECLARATIONS" for target in node.targets)
    )
    sources: set[str] = set()
    for call in ast.walk(assignment.value):
        if not isinstance(call, ast.Call):
            continue
        for keyword in call.keywords:
            if (
                keyword.arg == "canonical_factory"
                and isinstance(keyword.value, ast.Constant)
                and isinstance(keyword.value.value, str)
            ):
                sources.add(keyword.value.value)
        if (
            isinstance(call.func, ast.Name)
            and call.func.id == "_session_capability"
            and len(call.args) >= 3
            and isinstance(call.args[2], ast.Constant)
            and isinstance(call.args[2].value, str)
        ):
            sources.add(call.args[2].value)
    for item in governance.CAPABILITY_DECLARATIONS:
        if item.canonical_factory == item.implementation:
            sources.add(item.canonical_factory)
    return sources


def main() -> int:
    governance = _governance()
    violations: list[str] = []
    capabilities = tuple(governance.CAPABILITY_DECLARATIONS)
    candidates = tuple(governance.CANDIDATE_CLASSIFICATIONS)
    declared = {item.capability_id: item.implementation for item in capabilities}
    classified = {item.candidate_id: item.implementation for item in candidates}
    if declared != classified or len(classified) != len(candidates):
        violations.append("candidate/declaration bidirectional difference is non-empty")
    sources = _factory_sources(governance)
    classified_sources = {item.source_symbol for item in candidates}
    if sources != classified_sources:
        violations.append(
            f"factory source classifier difference discovered={sorted(sources - classified_sources)} "
            f"stale={sorted(classified_sources - sources)}"
        )
    keys = [item.recipe_key for item in capabilities]
    if len(keys) != len(set(keys)):
        violations.append("duplicate canonical composition recipe")
    if any(
        item.lifecycle_owner != item.start_owner or item.lifecycle_owner != item.stop_owner for item in capabilities
    ):
        violations.append("capability lifecycle start/stop ownership is not closed")

    classified_public = {item.symbol for item in governance.PUBLIC_SYMBOL_CLASSIFICATIONS}
    discovered_public: set[str] = set()
    for relative in GOVERNED_EXPORT_PACKAGES:
        path = ROOT / relative
        module = "mote." + relative.removesuffix("/__init__.py").replace("/", ".")
        exported = _literal_all(ast.parse(path.read_text(encoding="utf-8"), filename=relative))
        governed_names = {
            item.symbol.rsplit(".", 1)[-1]
            for item in governance.PUBLIC_SYMBOL_CLASSIFICATIONS
            if item.symbol.rsplit(".", 1)[0] == module
        }
        for name in exported & governed_names:
            discovered_public.add(f"{module}.{name}")
    if discovered_public != classified_public:
        violations.append(
            f"public symbol classifier difference discovered={sorted(discovered_public - classified_public)} "
            f"stale={sorted(classified_public - discovered_public)}"
        )

    runtime_product_imports: list[str] = []
    for path in (ROOT / "runtime").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("mote.product"):
                runtime_product_imports.append(f"{path.relative_to(ROOT)}:{node.lineno}")
            if isinstance(node, ast.Import) and any(alias.name.startswith("mote.product") for alias in node.names):
                runtime_product_imports.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    if runtime_product_imports:
        violations.append("Runtime imports Product: " + ", ".join(runtime_product_imports))

    if violations:
        print("\n".join(violations), file=sys.stderr)
        return 1
    print("composition recipes, source candidates, exports and lifecycle are closed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
