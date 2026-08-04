"""Hard closure checks for Product-owned production composition."""

from __future__ import annotations

import ast
from pathlib import Path

from mote.contracts.composition import CandidateRole, CapabilityStatus, OwnerStatus, PublicSymbolRole
from mote.product.composition.governance import (
    ACP_ROOT,
    AGUI_ROOT,
    CANDIDATE_CLASSIFICATIONS,
    CAPABILITY_DECLARATIONS,
    CLASSIFIER_VERSION,
    OWNER_DECLARATIONS,
    PUBLIC_SYMBOL_CLASSIFICATIONS,
    TEXTUAL_ROOT,
)

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_ROOTS = ("contracts", "kernel", "runtime", "orchestration", "product")


def _symbol_exists(symbol: str) -> bool:
    parts = symbol.removeprefix("mote.").split(".")
    for boundary in range(len(parts), 0, -1):
        module_path = PACKAGE_ROOT.joinpath(*parts[:boundary]).with_suffix(".py")
        package_path = PACKAGE_ROOT.joinpath(*parts[:boundary], "__init__.py")
        path = module_path if module_path.is_file() else package_path
        if not path.is_file():
            continue
        node = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        remaining = parts[boundary:]
        if len(remaining) == 1:
            exported = _literal_all(node)
            imported = {
                alias.asname or alias.name
                for item in node.body
                if isinstance(item, ast.ImportFrom)
                for alias in item.names
            }
            if remaining[0] in exported & imported:
                return True
        current = node.body
        for name in remaining:
            definition = next(
                (
                    item
                    for item in current
                    if isinstance(item, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == name
                ),
                None,
            )
            if definition is None:
                return False
            current = definition.body
        return True
    return False


def test_capability_recipes_and_candidates_are_an_exact_bijection() -> None:
    assert CLASSIFIER_VERSION == "product-composition-v1"
    active = tuple(item for item in CAPABILITY_DECLARATIONS if item.status is CapabilityStatus.ACTIVE)
    recipe_keys = [item.recipe_key for item in active]
    assert len(recipe_keys) == len(set(recipe_keys))
    declared = {item.capability_id: item.implementation for item in active}
    discovered = {item.candidate_id: item.implementation for item in CANDIDATE_CLASSIFICATIONS}
    assert declared == discovered
    assert len(discovered) == len(CANDIDATE_CLASSIFICATIONS)
    assert all(item.role is not CandidateRole.EXPLICIT_CAPABILITY for item in CANDIDATE_CLASSIFICATIONS)
    declaration_symbols = {item.canonical_factory for item in CAPABILITY_DECLARATIONS}
    assert all(item.source_symbol in declaration_symbols for item in CANDIDATE_CLASSIFICATIONS)
    discovered_sources = _discover_declared_factory_references()
    classified_sources = {item.source_symbol for item in CANDIDATE_CLASSIFICATIONS}
    assert discovered_sources == classified_sources


def test_composition_symbols_exist_in_source() -> None:
    missing: list[str] = []
    for item in CAPABILITY_DECLARATIONS:
        for symbol in (
            item.implementation,
            item.applicable_root,
            item.canonical_factory,
        ):
            if not _symbol_exists(symbol):
                missing.append(symbol)
    assert not missing, f"Composition declarations reference missing symbols: {sorted(set(missing))}"


def test_dangerous_public_runtime_orchestration_symbols_are_closed() -> None:
    symbols = [item.symbol for item in PUBLIC_SYMBOL_CLASSIFICATIONS]
    assert len(symbols) == len(set(symbols))
    assert all(_symbol_exists(item.symbol) for item in PUBLIC_SYMBOL_CLASSIFICATIONS)
    capability_implementations = {item.implementation for item in CAPABILITY_DECLARATIONS}
    for item in PUBLIC_SYMBOL_CLASSIFICATIONS:
        if item.role is PublicSymbolRole.INTERNAL_FACTORY:
            assert item.symbol not in capability_implementations


def test_public_symbol_classifier_covers_declared_package_entrypoints() -> None:
    classified = {item.symbol for item in PUBLIC_SYMBOL_CLASSIFICATIONS}
    governed = {
        "runtime/__init__.py": _literal_all(
            ast.parse((PACKAGE_ROOT / "runtime/__init__.py").read_text(encoding="utf-8"))
        ),
        "runtime/agent/__init__.py": {"Role"},
        "runtime/artifacts/__init__.py": {"DurableArtifactStore"},
        "runtime/durable/__init__.py": set(),
        "runtime/events/__init__.py": {"EventFabric"},
        "runtime/inference/__init__.py": {"GatewayGenerationOwner"},
        "runtime/models/routing/__init__.py": {"build_route_catalog"},
        "runtime/prompt/__init__.py": {"build_prompt_policy"},
        "orchestration/agents/__init__.py": {"AgentControl"},
        "orchestration/automation/cron/__init__.py": {"CronService"},
        "orchestration/background_tasks/__init__.py": {"BackgroundTaskPool"},
        "orchestration/workflows/__init__.py": {"WorkflowBuilder"},
    }
    discovered: set[str] = set()
    for relative, expected in governed.items():
        module = "mote." + relative.removesuffix("/__init__.py").replace("/", ".")
        source = (PACKAGE_ROOT / relative).read_text(encoding="utf-8")
        tree = ast.parse(source, filename=relative)
        exported = _literal_all(tree)
        assert expected <= exported
        discovered.update(f"{module}.{name}" for name in expected)
    assert discovered == classified


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


def _discover_declared_factory_references() -> set[str]:
    path = PACKAGE_ROOT / "product/composition/governance.py"
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
    for item in CAPABILITY_DECLARATIONS:
        if item.canonical_factory in {item.implementation, TEXTUAL_ROOT, AGUI_ROOT, ACP_ROOT}:
            sources.add(item.canonical_factory)
    return sources


def test_owner_scopes_resolve_to_one_most_specific_active_owner() -> None:
    active = tuple(item for item in OWNER_DECLARATIONS if item.status is OwnerStatus.ACTIVE)
    assert len({item.owner_id for item in active}) == len(active)
    assert len({item.path_prefix for item in active}) == len(active)
    for item in CAPABILITY_DECLARATIONS:
        module_path = item.implementation.removeprefix("mote.").replace(".", "/")
        matches = [owner for owner in active if module_path.startswith(owner.path_prefix)]
        assert matches, f"No owner scope covers {item.implementation}"
        most_specific = max(len(owner.path_prefix) for owner in matches)
        assert sum(len(owner.path_prefix) == most_specific for owner in matches) == 1


def test_no_process_global_tool_discovery_path_remains() -> None:
    violations: list[str] = []
    for root in PRODUCTION_ROOTS:
        for path in (PACKAGE_ROOT / root).rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Name) and node.id in {
                    "register_agent",
                    "register_tool",
                    "declared_agent_catalog",
                    "declared_tool_catalog",
                    "ToolRegistry",
                }:
                    violations.append(f"{path.relative_to(PACKAGE_ROOT)}:{node.lineno}:{node.id}")
    assert not violations, "Legacy global tool discovery remains:\n" + "\n".join(violations)


def test_runtime_event_facade_does_not_reexport_contracts() -> None:
    path = PACKAGE_ROOT / "runtime/events/__init__.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    violations = [
        node.module
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("mote.contracts.events")
    ]
    assert not violations

    consumers = []
    for root in PRODUCTION_ROOTS:
        for candidate in (PACKAGE_ROOT / root).rglob("*.py"):
            candidate_tree = ast.parse(candidate.read_text(encoding="utf-8"), filename=str(candidate))
            if any(
                isinstance(node, ast.ImportFrom) and node.module == "mote.runtime.events"
                for node in ast.walk(candidate_tree)
            ):
                consumers.append(candidate.relative_to(PACKAGE_ROOT).as_posix())
    assert not consumers, f"Runtime event compatibility consumers remain: {consumers}"
