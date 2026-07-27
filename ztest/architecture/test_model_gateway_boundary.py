"""Architecture locks for the canonical model control plane."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parents[2]


def _runtime_imports(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imports.append((node.lineno, node.module))
        elif isinstance(node, ast.Import):
            imports.extend((node.lineno, alias.name) for alias in node.names)
    return imports


def test_kernel_never_imports_runtime_or_product() -> None:
    violations = []
    for path in (ROOT / "kernel").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module.startswith(("mote.runtime", "mote.product")):
                    violations.append(f"{path.relative_to(ROOT)}:{node.lineno} {module}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith(("mote.runtime", "mote.product")):
                        violations.append(f"{path.relative_to(ROOT)}:{node.lineno} {alias.name}")
    assert violations == []


def test_legacy_llm_control_paths_do_not_exist() -> None:
    assert not (ROOT / "runtime/models/gateway_client.py").exists()
    assert not (ROOT / "runtime/models/clients/health.py").exists()
    assert not (ROOT / "runtime/models/clients/validators.py").exists()
    sources = {
        path.relative_to(ROOT): path.read_text(encoding="utf-8")
        for root in (
            ROOT / "contracts/events",
            ROOT / "kernel",
            ROOT / "runtime/models",
            ROOT / "product/integrations/models",
        )
        for path in root.rglob("*.py")
    }
    forbidden = (
        "GatewayLLMClient",
        "_RoutedLLMClient",
        "_run_with_recovery",
        "_fallback_supplier",
        "infer_native_tool_provider",
        "LLMRequestEvent",
        "LLMResponseEvent",
        "LLMErrorEvent",
        "LLMRetryEvent",
        "def rotate_credential",
    )
    violations = [f"{path}: {token}" for path, source in sources.items() for token in forbidden if token in source]
    assert violations == []


def test_semantic_router_core_is_provider_neutral() -> None:
    core = (
        ROOT / "contracts/models/routing.py",
        ROOT / "contracts/ports/routing.py",
        ROOT / "runtime/models/gateway.py",
        ROOT / "runtime/models/routing/catalog.py",
        ROOT / "runtime/models/routing/policy.py",
        ROOT / "runtime/models/routing/service.py",
        ROOT / "runtime/models/routing/state.py",
    )
    forbidden_prefixes = (
        "mote.contracts.config.llm",
        "mote.product",
        "mote.runtime.models.clients",
        "anthropic",
        "openai",
    )
    violations = [
        f"{path.relative_to(ROOT)}:{lineno} {module}"
        for path in core
        for lineno, module in _runtime_imports(path)
        if module.startswith(forbidden_prefixes)
    ]
    forbidden_names = {"BaseLLM", "LLMConfig", "AsyncOpenAI", "AsyncAnthropic"}
    for path in core:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        violations.extend(
            f"{path.relative_to(ROOT)}:{node.lineno} {node.id}"
            for node in ast.walk(tree)
            if isinstance(node, ast.Name) and node.id in forbidden_names
        )
    assert violations == []


def test_runtime_composition_injects_product_policy_only_through_wiring() -> None:
    path = ROOT / "runtime/agent/runtime_modules/cognition.py"
    imports = [module for _lineno, module in _runtime_imports(path)]
    assert not any(module.startswith("mote.product") for module in imports)
    assert "routing_strategy_builders" in path.read_text(encoding="utf-8")


def test_legacy_semantic_router_state_and_strategy_surfaces_are_deleted() -> None:
    for relative in (
        "runtime/models/routing/control.py",
        "runtime/models/routing/legacy_adapter.py",
        "runtime/models/routing/strategy.py",
    ):
        assert not (ROOT / relative).exists()

    kernel_source = (ROOT / "kernel/models/routing.py").read_text(encoding="utf-8")
    product_source = (ROOT / "product/routing/squilla/strategy.py").read_text(encoding="utf-8")
    for legacy_name in ("ModelCard", "RoutingRequest", "RoutingDecision"):
        assert f"class {legacy_name}" not in kernel_source
    for legacy_surface in (
        "def select(",
        "RoutingHistoryStore",
        "SeedFloorStore",
        "control_holds",
        "seed_floors",
    ):
        assert legacy_surface not in product_source


def test_squilla_model_lifetime_has_no_process_global_owner() -> None:
    engine_source = (ROOT / "product/routing/squilla/ml/engine.py").read_text(encoding="utf-8")
    product_source = (ROOT / "product/routing/__init__.py").read_text(encoding="utf-8")
    backend_source = (ROOT / "product/cli/backend.py").read_text(encoding="utf-8")

    for legacy_owner in ("_SHARED", "shared_engine", "def prewarm("):
        assert legacy_owner not in engine_source
    assert "RoutingModelRuntime" in product_source
    assert "ProductContainer.standard" not in backend_source
