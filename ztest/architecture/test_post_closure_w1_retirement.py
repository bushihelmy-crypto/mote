"""Exact zero-residue gates for deterministic W1 surface retirement."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_ROOTS = ("contracts", "kernel", "runtime", "orchestration", "product")


def _production_sources() -> list[tuple[Path, str]]:
    return [
        (path, path.read_text(encoding="utf-8")) for root in PRODUCTION_ROOTS for path in (ROOT / root).rglob("*.py")
    ]


def test_provider_moderation_surface_and_universal_error_decorator_are_retired() -> None:
    assert not (ROOT / "product/models/providers/error_handling.py").exists()
    for path, source in _production_sources():
        tree = ast.parse(source, filename=str(path))
        assert not any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "moderation"
            for node in ast.walk(tree)
        ), path
        assert "provider_error_handler" not in source, path


def test_inference_admin_surface_has_no_production_entrypoint_or_consumer() -> None:
    assert not any((ROOT / "product/interfaces/inference_admin_api").rglob("*.py"))
    for path, source in _production_sources():
        assert "inference_admin_api" not in source, path


def test_legacy_model_client_port_and_llm_client_are_retired() -> None:
    assert not (ROOT / "contracts/ports/model/client.py").exists()
    for path, source in _production_sources():
        tree = ast.parse(source, filename=str(path))
        assert not any(isinstance(node, ast.ClassDef) and node.name == "LLMClient" for node in ast.walk(tree)), path
        assert "contracts.ports.model.client" not in source, path
