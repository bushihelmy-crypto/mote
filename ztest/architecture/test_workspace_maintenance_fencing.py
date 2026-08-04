from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_generic_workspace_cleanup_and_runtime_maintenance_are_retired() -> None:
    retired = (
        "runtime/agent/runtime_maintenance.py",
        "runtime/session/workspace/cleanup.py",
        "runtime/session/workspace/cleanup_gate.py",
        "product/config/workspace.py",
    )
    assert all(not (ROOT / relative).exists() for relative in retired)


def test_runtime_services_do_not_expose_destructive_cleanup_coordination() -> None:
    services = (ROOT / "runtime/services.py").read_text(encoding="utf-8")
    assert "WorkspaceCleanupGate" not in services
    assert "workspace_cleanup_gate" not in services
