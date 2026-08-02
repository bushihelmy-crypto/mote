"""R2.11 gates for the unique typed Product Agent-hosting seam."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PRODUCT = ROOT / "product"


def _production_modules() -> list[Path]:
    return sorted(PRODUCT.rglob("*.py"))


def test_product_has_one_agent_control_construction_owner() -> None:
    owners: list[str] = []
    for path in _production_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if any(
            isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "AgentControl"
            for node in ast.walk(tree)
        ):
            owners.append(path.relative_to(ROOT).as_posix())
    assert owners == ["product/session_hosting/composition.py"]


def test_product_never_assigns_agent_control_directly() -> None:
    violations: list[str] = []
    for path in _production_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            targets = (
                node.targets
                if isinstance(node, ast.Assign)
                else [node.target] if isinstance(node, ast.AnnAssign) else []
            )
            if any(isinstance(target, ast.Attribute) and target.attr == "agent_control" for target in targets):
                violations.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert violations == []


def test_hosting_core_does_not_erase_role_or_runtime_types() -> None:
    paths = (
        PRODUCT / "session_hosting" / "composition.py",
        PRODUCT / "session_hosting" / "registry.py",
        PRODUCT / "entrypoints" / "cli" / "backend.py",
    )
    for path in paths:
        source = path.read_text(encoding="utf-8")
        assert "from typing import Any" not in source
        assert "getattr(" not in source
    registry = (PRODUCT / "session_hosting" / "registry.py").read_text(encoding="utf-8")
    assert "class ResidentSession(Generic[OutputT])" in registry
    assert "runtime: AgentRuntime[OutputT]" in registry

    assert not (ROOT / "orchestration" / "agents" / "environment_facade.py").exists()
    assert "class HostedAgent(RunnableAgent[OutputT], Protocol[OutputT])" in registry
    assert "_sessions: dict[str, ResidentSession[OutputT]]" in registry
    assert "_sessions: Dict[str, Any]" not in registry


def test_cli_and_registry_delegate_to_canonical_composition() -> None:
    for relative in (
        "product/session_hosting/registry.py",
        "product/entrypoints/cli/backend.py",
    ):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "compose_resident_agent(" in source


def test_resident_agent_composition_installs_durable_turn_governance() -> None:
    composition = (ROOT / "product/session_hosting/composition.py").read_text(encoding="utf-8")
    control = (ROOT / "orchestration/agents/control.py").read_text(encoding="utf-8")
    scheduler = (ROOT / "orchestration/agents/execution/turn_scheduler.py").read_text(encoding="utf-8")
    assert "turn_queue_capacity=governance.turn_queue_capacity" in composition
    assert "root_turn_weights=tuple(sorted(governance.root_weights.items()))" in composition
    assert "DurableTurnQueueStore(" in control
    assert "DurableTurnScheduler(" in scheduler
    assert "_run_durable_claim" in scheduler
