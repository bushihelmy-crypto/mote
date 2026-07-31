"""Low-resource closure checks for event transformations and subscriptions."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_ROOTS = ("contracts", "kernel", "runtime", "orchestration", "product")


def _transformations() -> tuple[object, ...]:
    import importlib.util

    helper_spec = importlib.util.spec_from_file_location(
        "mote_governance_artifact",
        ROOT / "ztest/architecture/governance_artifact.py",
    )
    if helper_spec is None or helper_spec.loader is None:
        raise RuntimeError("governance artifact loader is unavailable")
    helper = importlib.util.module_from_spec(helper_spec)
    helper_spec.loader.exec_module(helper)
    helper._modules()
    helper._load("mote.contracts.events.governance", "contracts/events/governance.py")
    module = helper._load(
        "mote.product.composition.event_governance",
        "product/composition/event_governance.py",
    )
    return module.TRANSFORMATION_DECLARATIONS


def _qualified(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _qualified(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def main() -> int:
    declarations = _transformations()
    violations: list[str] = []
    ids = [item.transformation_id for item in declarations]
    if len(ids) != len(set(ids)):
        violations.append("duplicate transformation identity")
    keys = [(item.bounded_domain, item.source_stage, item.target_stage) for item in declarations]
    if len(keys) != len(set(keys)):
        violations.append("multiple canonical owners for one stage crossing")
    converters = {item.converter.rsplit(".", 1)[-1] for item in declarations}
    required_converters: set[str] = set()
    subscription_calls: list[tuple[str, int]] = []
    for root in PRODUCTION_ROOTS:
        for path in (ROOT / root).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            relative = path.relative_to(ROOT).as_posix()
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                    node.name.startswith("encode_") or node.name.startswith("decode_")
                ):
                    if relative in {
                        "runtime/session/codec.py",
                        "product/inference/daemon/operations_audit_codec.py",
                    }:
                        required_converters.add(node.name)
                if isinstance(node, ast.Call) and _qualified(node.func) == "SubscriptionSpec":
                    subscription_calls.append((relative, node.lineno))
    missing = sorted(required_converters - converters)
    if missing:
        violations.append("undeclared codec transformations: " + ", ".join(missing))
    if [path for path, _line in subscription_calls] != [
        "runtime/agent/components/session.py",
        "runtime/agent/components/session.py",
    ]:
        violations.append("subscription source classifier drift: " + repr(subscription_calls))
    required_subscription_converters = {"mote.runtime.lsp.service.LspService.handle"}
    declared_converters = {item.converter for item in declarations}
    if not required_subscription_converters <= declared_converters:
        violations.append("recoverable subscription transformation is undeclared")
    if violations:
        print("\n".join(violations), file=sys.stderr)
        return 1
    print("event transformations and subscription sources are closed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
