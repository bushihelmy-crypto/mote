"""R3.5 gates for explicit, manifest-governed optional backend activation."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from mote.product.routing.squilla.ml.backend_loader import (
    SQUILLA_INFERENCE_BACKEND,
    ApprovedRoutingBackendLoader,
    RoutingBackendManifest,
)

ROOT = Path(__file__).resolve().parents[2]


def test_canonical_composition_import_does_not_load_optional_surfaces() -> None:
    script = """
import sys
import mote.product.composition.application
forbidden = {
    'mote.product.routing.squilla.ml.inference.core',
    'mote.product.routing.squilla.ml.inference.artifacts',
    'mote.product.interfaces.textual',
    'mote.runtime.interactive.chromium_window',
}
loaded = sorted(forbidden & set(sys.modules))
if loaded:
    raise SystemExit('eager optional modules: ' + ','.join(loaded))
"""
    completed = subprocess.run(
        [sys.executable, "-B", "-c", script],
        check=False,
        capture_output=True,
        text=True,
        cwd=ROOT.parent,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_loader_rejects_unapproved_manifest_before_import() -> None:
    altered = RoutingBackendManifest(
        identity=SQUILLA_INFERENCE_BACKEND.identity,
        provider_kind=SQUILLA_INFERENCE_BACKEND.provider_kind,
        module="untrusted.backend",
        factory_contract=SQUILLA_INFERENCE_BACKEND.factory_contract,
        provenance="checkout",
        capabilities=SQUILLA_INFERENCE_BACKEND.capabilities,
        generation=SQUILLA_INFERENCE_BACKEND.generation + 1,
    )
    with pytest.raises(ValueError, match="not approved"):
        ApprovedRoutingBackendLoader(altered)


def test_manifest_declares_stable_typed_activation_identity() -> None:
    manifest = SQUILLA_INFERENCE_BACKEND
    assert manifest.identity == "mote.routing.squilla.inference-core"
    assert manifest.provider_kind == "routing-inference"
    assert manifest.factory_contract == "mote.routing.squilla.core-factory.v1"
    assert manifest.provenance == "mote-builtin"
    assert manifest.generation == 1
    assert manifest.capabilities == ("lightgbm", "onnx-mlp", "bge-embedding")
