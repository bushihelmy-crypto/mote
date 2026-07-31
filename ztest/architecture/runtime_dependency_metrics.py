"""Print structured runtime package-boundary metrics for refactor comparisons."""
from __future__ import annotations

import ast
import json
from collections import defaultdict
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_ROOT = PACKAGE_ROOT / "runtime"


def _module_name(path: Path) -> str:
    relative = path.relative_to(PACKAGE_ROOT).with_suffix("")
    parts = relative.parts[:-1] if relative.name == "__init__" else relative.parts
    return "mote." + ".".join(parts)


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def _runtime_target(module: str, known: set[str]) -> str | None:
    if not module.startswith("mote.runtime"):
        return None
    candidate = module
    while candidate not in known and candidate.startswith("mote.runtime."):
        candidate = candidate.rsplit(".", 1)[0]
    return candidate if candidate in known else None


def _strongly_connected(graph: dict[str, set[str]]) -> list[list[str]]:
    index = 0
    stack: list[str] = []
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    on_stack: set[str] = set()
    components: list[list[str]] = []

    def visit(node: str) -> None:
        nonlocal index
        indices[node] = lowlinks[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        for target in graph[node]:
            if target not in indices:
                visit(target)
                lowlinks[node] = min(lowlinks[node], lowlinks[target])
            elif target in on_stack:
                lowlinks[node] = min(lowlinks[node], indices[target])
        if lowlinks[node] != indices[node]:
            return
        component: list[str] = []
        while True:
            member = stack.pop()
            on_stack.remove(member)
            component.append(member)
            if member == node:
                break
        components.append(component)

    for node in sorted(graph):
        if node not in indices:
            visit(node)
    return components


def _is_illegal(source: str, target: str) -> bool:
    if source.startswith("mote.runtime.tools"):
        return target.startswith(("mote.runtime.agent", "mote.runtime.context"))
    if source.startswith("mote.runtime.context"):
        return target.startswith(("mote.runtime.agent", "mote.runtime.tools"))
    if source.startswith("mote.runtime.artifacts"):
        return target.startswith("mote.runtime.fileops.artifact_")
    return False


def collect_metrics() -> dict[str, object]:
    paths = sorted(RUNTIME_ROOT.rglob("*.py"))
    path_by_module = {_module_name(path): path for path in paths}
    known = set(path_by_module)
    graph = {module: set() for module in known}
    raw_imports: dict[str, set[str]] = {}
    illegal_edges: set[tuple[str, str]] = set()
    reexports: dict[str, int] = {}

    for module, path in path_by_module.items():
        imported = _imports(path)
        raw_imports[module] = imported
        for imported_module in imported:
            target = _runtime_target(imported_module, known)
            if target and target != module:
                graph[module].add(target)
            if _is_illegal(module, imported_module):
                illegal_edges.add((module, imported_module))
        if path.name == "__init__.py":
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            reexports[module] = sum(
                len(node.names) for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))
            )

    nontrivial = [component for component in _strongly_connected(graph) if len(component) > 1]
    package_fanout: dict[str, set[str]] = defaultdict(set)
    for source, targets in graph.items():
        source_package = ".".join(source.split(".")[:3])
        for target in targets:
            target_package = ".".join(target.split(".")[:3])
            if target_package != source_package:
                package_fanout[source_package].add(target_package)

    top_files = sorted(((len(targets), source) for source, targets in graph.items()), reverse=True)[:10]
    top_packages = sorted(
        ((len(targets), source) for source, targets in package_fanout.items()),
        reverse=True,
    )[:10]
    return {
        "nontrivial_scc_count": len(nontrivial),
        "nontrivial_scc_nodes": sum(map(len, nontrivial)),
        "max_nontrivial_scc_size": max(map(len, nontrivial), default=0),
        "illegal_edge_count": len(illegal_edges),
        "illegal_edges": sorted([source, target] for source, target in illegal_edges),
        "init_reexport_counts": dict(sorted(reexports.items())),
        "top_file_fanout": [{"module": source, "count": count} for count, source in top_files],
        "top_package_fanout": [{"package": source, "count": count} for count, source in top_packages],
    }


if __name__ == "__main__":
    print(json.dumps(collect_metrics(), indent=2, sort_keys=True))
