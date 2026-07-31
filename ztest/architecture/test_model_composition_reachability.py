from __future__ import annotations

from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2]


def _production_sources(*roots: str):
    for root in roots:
        yield from (PACKAGE_ROOT / root).rglob("*.py")


def test_runtime_has_no_product_model_syntax_or_second_generation_owner() -> None:
    forbidden = (
        "ModelsConfig",
        "AtomicModelRuntime",
        "build_model_runtime_snapshot",
        "models.default",
        "models.tasks",
        "builtin_model_gateway",
        "manage_runtime",
    )
    violations = []
    for path in _production_sources("runtime"):
        source = path.read_text(encoding="utf-8")
        for token in forbidden:
            if token in source:
                violations.append(f"{path.relative_to(PACKAGE_ROOT)}: {token}")
    assert violations == []


def test_production_roots_install_atomic_application_composition() -> None:
    roots = (PACKAGE_ROOT / "engine.py", PACKAGE_ROOT / "product/entrypoints/cli/bootstrap.py")
    for path in roots:
        source = path.read_text(encoding="utf-8")
        assert "install_initial_application_composition" in source
        assert "builtin_model_gateway" not in source


def test_product_model_runtime_only_accepts_compiled_bindings() -> None:
    assert not (PACKAGE_ROOT / "product/models/gateway.py").exists()
    assert not (PACKAGE_ROOT / "product/models/endpoint.py").exists()
    source = (PACKAGE_ROOT / "product/models/runtime_generation.py").read_text(encoding="utf-8")
    assert "CompiledModelGeneration" in source
    assert "ModelsConfig" not in source
    assert "ProductModelEndpointResolver" not in source
    assert "ProductModelEndpointAdapter" not in source
