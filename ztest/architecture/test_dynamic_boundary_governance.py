"""Hard structural gates for governed dynamic and event boundaries."""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
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


def _production_paths():
    for root in PRODUCTION_ROOTS:
        yield from (PACKAGE_ROOT / root).rglob("*.py")


def _qualified_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _qualified_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def test_uncommitted_facts_are_created_only_by_domain_codecs() -> None:
    actual: set[str] = set()
    for path in _production_paths():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if any(
            isinstance(node, ast.Call) and _qualified_name(node.func).endswith("UncommittedFact")
            for node in ast.walk(tree)
        ):
            actual.add(path.relative_to(PACKAGE_ROOT).as_posix())
    assert actual == APPROVED_FACT_ENCODERS


def test_product_has_no_dynamic_import_or_pep562_export() -> None:
    violations: list[str] = []
    for path in (PACKAGE_ROOT / "product").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        relative = path.relative_to(PACKAGE_ROOT).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "__getattr__":
                violations.append(f"{relative}:{node.lineno}: module/proxy __getattr__")
            if not isinstance(node, ast.Call):
                continue
            name = _qualified_name(node.func)
            if name in {"importlib.import_module", "__import__"}:
                violations.append(f"{relative}:{node.lineno}: {name}")
    assert not violations, "Dynamic Product boundary violations:\n" + "\n".join(sorted(violations))


def test_all_production_code_has_no_dynamic_import_or_pep562_export() -> None:
    violations: list[str] = []
    for path in _production_paths():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        relative = path.relative_to(PACKAGE_ROOT).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "__getattr__":
                violations.append(f"{relative}:{node.lineno}: __getattr__")
            if isinstance(node, ast.Call) and _qualified_name(node.func) in {
                "importlib.import_module",
                "pkgutil.walk_packages",
            }:
                violations.append(f"{relative}:{node.lineno}: {_qualified_name(node.func)}")
    assert not violations, "Dynamic production boundary violations:\n" + "\n".join(sorted(violations))


def test_telemetry_erasure_is_private_and_runtime_owned() -> None:
    telemetry = (PACKAGE_ROOT / "runtime/events/telemetry.py").read_text(encoding="utf-8")
    assert "class _TypedTelemetryBinding" in telemetry
    tree = ast.parse(telemetry, filename="runtime/events/telemetry.py")
    runtime = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "TelemetryRuntime")
    assert all(
        not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name != "subscribe_raw"
        for node in runtime.body
    )

    violations: list[str] = []
    for path in _production_paths():
        if path == PACKAGE_ROOT / "runtime/events/telemetry.py":
            continue
        source = path.read_text(encoding="utf-8")
        if "_TypedTelemetryBinding" in source or "TypedTelemetryBinding" in source:
            violations.append(path.relative_to(PACKAGE_ROOT).as_posix())
    assert not violations, f"Telemetry erasure leaked outside its owner: {violations}"


def test_governed_boundary_does_not_suppress_types_with_cast_any() -> None:
    violations: list[str] = []
    for path in _production_paths():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        relative = path.relative_to(PACKAGE_ROOT).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or _qualified_name(node.func) not in {
                "cast",
                "typing.cast",
            }:
                continue
            if node.args and isinstance(node.args[0], ast.Name) and node.args[0].id == "Any":
                violations.append(f"{relative}:{node.lineno}")
    assert not violations, "cast(Any, ...) is forbidden in production:\n" + "\n".join(sorted(violations))


def test_governed_public_boundaries_do_not_expose_any() -> None:
    violations: list[str] = []
    for relative in GOVERNED_TYPED_PATHS:
        path = PACKAGE_ROOT / relative
        candidates = path.rglob("*.py") if path.is_dir() else (path,)
        for candidate in candidates:
            tree = ast.parse(candidate.read_text(encoding="utf-8"), filename=str(candidate))
            for node in ast.walk(tree):
                if isinstance(node, ast.Name) and node.id == "Any":
                    violations.append(f"{candidate.relative_to(PACKAGE_ROOT)}:{node.lineno}")
    assert not violations, "Any leaked through a governed public boundary:\n" + "\n".join(violations)
