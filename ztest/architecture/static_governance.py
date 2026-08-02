"""Low-resource static architecture gates with no package import side effects."""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_ROOTS = ("contracts", "kernel", "runtime", "orchestration", "product")
APPROVED_FACT_ENCODERS = {
    "product/inference/daemon/operations_audit_codec.py",
    "runtime/session/codec.py",
}
GOVERNED_TYPED_PATHS = (
    "contracts/composition",
    "contracts/ports/events/telemetry.py",
    "contracts/ports/code_intelligence/lsp.py",
    "contracts/ports/task/operations.py",
    "product/composition",
    "product/lsp/factory.py",
    "runtime/events/telemetry.py",
)


def production_paths():
    for root in PRODUCTION_ROOTS:
        yield from (ROOT / root).rglob("*.py")


def qualified_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = qualified_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def check_local_imports() -> list[str]:
    violations: list[str] = []
    for path in production_paths():
        relative = path.relative_to(ROOT).as_posix()
        parents: dict[ast.AST, ast.AST] = {}
        tree = _tree(path)
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parents[child] = parent
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            parent = parents.get(node)
            while parent is not None and not isinstance(parent, ast.Module):
                if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    violations.append(f"{relative}:{node.lineno}: nested import")
                    break
                parent = parents.get(parent)
    return violations


def _module_name(path: Path) -> str:
    parts = list(path.relative_to(ROOT).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(("mote", *parts))


def _resolve_from(source: str, node: ast.ImportFrom) -> str:
    if node.level == 0:
        return node.module or ""
    package = source.split(".")[:-1]
    prefix = package[: len(package) - node.level + 1]
    if node.module:
        prefix.extend(node.module.split("."))
    return ".".join(prefix)


def _product_import_graph() -> dict[str, set[str]]:
    graph: dict[str, set[str]] = defaultdict(set)
    for path in (ROOT / "product").rglob("*.py"):
        source = _module_name(path)
        graph[source]
        for node in ast.walk(_tree(path)):
            targets: list[str] = []
            if isinstance(node, ast.Import):
                targets.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                target = _resolve_from(source, node)
                if target:
                    targets.append(target)
            graph[source].update(target for target in targets if target.startswith("mote.product"))
    return graph


def _unit(module: str, depth: int) -> str | None:
    parts = module.split(".")
    if parts[:2] != ["mote", "product"] or len(parts) < 3:
        return None
    product_parts = parts[2:]
    if depth == 1:
        return product_parts[0]
    if len(product_parts) >= 2 and (ROOT / "product" / product_parts[0] / product_parts[1]).is_dir():
        return ".".join(product_parts[:2])
    return product_parts[0]


def _components(graph: dict[str, set[str]]) -> set[tuple[str, ...]]:
    indexes: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    stack: list[str] = []
    stacked: set[str] = set()
    result: set[tuple[str, ...]] = set()

    def visit(node: str) -> None:
        indexes[node] = lowlinks[node] = len(indexes)
        stack.append(node)
        stacked.add(node)
        for target in graph[node]:
            if target not in indexes:
                visit(target)
                lowlinks[node] = min(lowlinks[node], lowlinks[target])
            elif target in stacked:
                lowlinks[node] = min(lowlinks[node], indexes[target])
        if lowlinks[node] != indexes[node]:
            return
        component: list[str] = []
        while True:
            target = stack.pop()
            stacked.remove(target)
            component.append(target)
            if target == node:
                break
        if len(component) > 1:
            result.add(tuple(sorted(component)))

    for node in tuple(graph):
        if node not in indexes:
            visit(node)
    return result


def check_product_scc() -> list[str]:
    imports = _product_import_graph()
    violations: list[str] = []
    for depth in (1, 2):
        graph: dict[str, set[str]] = defaultdict(set)
        for source, targets in imports.items():
            source_unit = _unit(source, depth)
            if source_unit is None or (depth == 2 and "." not in source_unit):
                continue
            graph[source_unit]
            for target in targets:
                target_unit = _unit(target, depth)
                if target_unit is None or target_unit == source_unit:
                    continue
                if depth == 1 or _unit(source, 1) == _unit(target, 1):
                    graph[source_unit].add(target_unit)
        violations.extend(f"depth {depth} SCC: {', '.join(component)}" for component in sorted(_components(graph)))
    return violations


def check_telemetry_erasure() -> list[str]:
    owner = ROOT / "runtime/events/telemetry.py"
    tree = _tree(owner)
    classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}
    violations: list[str] = []
    if "_TypedTelemetryBinding" not in classes:
        violations.append("runtime/events/telemetry.py: private erased binding missing")
    runtime = classes.get("TelemetryRuntime")
    if runtime and any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "subscribe_raw"
        for node in runtime.body
    ):
        violations.append("runtime/events/telemetry.py: subscribe_raw remains public")
    for path in production_paths():
        if path == owner:
            continue
        source = path.read_text(encoding="utf-8")
        if "_TypedTelemetryBinding" in source or "TypedTelemetryBinding" in source:
            violations.append(f"{path.relative_to(ROOT)}: telemetry erasure leaked")
    return violations


def check_fact_admission() -> list[str]:
    actual = {
        path.relative_to(ROOT).as_posix()
        for path in production_paths()
        if any(
            isinstance(node, ast.Call) and qualified_name(node.func).endswith("UncommittedFact")
            for node in ast.walk(_tree(path))
        )
    }
    return [
        *(f"unapproved constructor: {path}" for path in sorted(actual - APPROVED_FACT_ENCODERS)),
        *(f"declared encoder missing constructor: {path}" for path in sorted(APPROVED_FACT_ENCODERS - actual)),
    ]


def check_dynamic_discovery() -> list[str]:
    violations: list[str] = []
    forbidden_calls = {"importlib.import_module", "pkgutil.walk_packages", "__import__"}
    approved_importers = {"product/routing/squilla/ml/backend_loader.py"}
    for path in production_paths():
        relative = path.relative_to(ROOT).as_posix()
        if relative in approved_importers:
            continue
        for node in ast.walk(_tree(path)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "__getattr__":
                violations.append(f"{relative}:{node.lineno}: dynamic __getattr__")
            if isinstance(node, ast.Call) and qualified_name(node.func) in forbidden_calls:
                violations.append(f"{relative}:{node.lineno}: {qualified_name(node.func)}")
    return violations


def check_governed_boundary() -> list[str]:
    violations: list[str] = []
    for relative in GOVERNED_TYPED_PATHS:
        path = ROOT / relative
        paths = path.rglob("*.py") if path.is_dir() else (path,)
        for candidate in paths:
            for node in ast.walk(_tree(candidate)):
                if isinstance(node, ast.Name) and node.id == "Any":
                    violations.append(f"{candidate.relative_to(ROOT)}:{node.lineno}: Any")
    for path in production_paths():
        for node in ast.walk(_tree(path)):
            if (
                isinstance(node, ast.Call)
                and qualified_name(node.func) in {"cast", "typing.cast"}
                and node.args
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id == "Any"
            ):
                violations.append(f"{path.relative_to(ROOT)}:{node.lineno}: cast(Any)")
    return violations


def check_derived_artifact() -> list[str]:
    artifacts = (
        ("ztest/architecture/governance_artifact.py", "zdocs/architecture/dynamic-boundary-governance-v1.json"),
        (
            "ztest/architecture/requirement_evidence.py",
            "zdocs/architecture/dynamic-boundary-requirement-evidence-v1.json",
        ),
    )
    violations: list[str] = []
    for generator, target_name in artifacts:
        completed = subprocess.run(
            [sys.executable, "-B", generator],
            cwd=ROOT,
            capture_output=True,
            check=False,
        )
        if completed.returncode:
            violations.append(f"{generator}: generator exited with status {completed.returncode}")
            continue
        target = ROOT / target_name
        try:
            generated = json.loads(completed.stdout)
            committed = json.loads(target.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as error:
            violations.append(f"{target_name}: artifact could not be compared: {error}")
            continue
        if generated != committed:
            violations.append(f"stale artifact: {target_name}")
    return violations


CHECKS = {
    "local-imports": check_local_imports,
    "product-scc": check_product_scc,
    "telemetry-erasure": check_telemetry_erasure,
    "fact-admission": check_fact_admission,
    "dynamic-discovery": check_dynamic_discovery,
    "derived-artifact": check_derived_artifact,
    "governed-boundary": check_governed_boundary,
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("check", choices=tuple(CHECKS))
    arguments = parser.parse_args()
    violations = CHECKS[arguments.check]()
    if violations:
        print("\n".join(sorted(violations)))
        return 1
    print(f"{arguments.check} architecture invariant is closed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
