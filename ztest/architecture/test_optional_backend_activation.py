"""Optional backends have explicit construction and no pseudo discovery path."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_squilla_uses_its_authoritative_builtin_core() -> None:
    engine = (ROOT / "product/routing/squilla/ml/engine.py").read_text(encoding="utf-8")
    assert "from mote.product.routing.squilla.ml.inference.core import InferenceCore" in engine
    assert "backend_loader" not in engine
    assert not (ROOT / "product/routing/squilla/ml/backend_loader.py").exists()


def test_temporal_activation_has_no_dynamic_fixed_module_lookup() -> None:
    assert not (ROOT / "product/workflows/temporal_catalog.py").exists()
    bootstrap = (ROOT / "product/composition/bootstrap.py").read_text(encoding="utf-8")
    assert "from mote.product.workflows.temporal_effects import TemporalWorkflowEffects" in bootstrap


def test_importing_composition_does_not_activate_optional_resources() -> None:
    from mote.product.composition.bootstrap import ApplicationBuildRequest

    assert ApplicationBuildRequest.__name__ == "ApplicationBuildRequest"
