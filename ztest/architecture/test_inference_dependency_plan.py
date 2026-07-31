import json
from pathlib import Path

import yaml

from zdocs.parity.verify_dependency_plan import verify

ROOT = Path(__file__).resolve().parents[2]
PLAN = ROOT / "zdocs" / "parity" / "dependency-plan-v1.yaml"
LOCK = ROOT / "requirements" / "inference.lock"
SBOM = ROOT / "zdocs" / "parity" / "inference-sbom-v1.cdx.json"
PLATFORMS = ROOT / "zdocs" / "parity" / "dependency-platform-matrix-v1.yaml"
REVIEW = ROOT / "zdocs" / "parity" / "dependency-review-v1.yaml"


def test_inference_dependency_lock_and_sbom_match_frozen_plan(tmp_path):
    generated = tmp_path / "inference-sbom-v1.cdx.json"
    document = verify(PLAN, LOCK, generated)
    assert document["bomFormat"] == "CycloneDX"
    assert document["specVersion"] == "1.6"
    assert json.loads(SBOM.read_text(encoding="utf-8")) == document


def test_every_dependency_has_all_gate_zero_supply_chain_fields():
    plan = yaml.safe_load(PLAN.read_text(encoding="utf-8"))
    required = {
        "name",
        "version_range",
        "license",
        "purpose",
        "optional",
        "release_scope",
        "import_layer",
        "owner",
        "cve_policy",
        "alternatives_considered",
        "sbom_component",
        "native_binary",
        "platforms",
    }
    for component in plan["components"]:
        assert required <= component.keys()
        assert component["release_scope"]
        assert component["platforms"]


def test_platform_matrix_covers_every_planned_component_and_target():
    plan = yaml.safe_load(PLAN.read_text(encoding="utf-8"))
    matrix = yaml.safe_load(PLATFORMS.read_text(encoding="utf-8"))
    assert set(matrix["components"]) == {component["name"] for component in plan["components"]}
    assert set(matrix["python_versions"]) == {"3.9.2", "3.10", "3.11", "3.12"}
    assert set(matrix["platforms"]) == {"linux_x86_64", "linux_aarch64"}


def test_dependency_gate_cannot_pass_without_signed_reviews():
    plan = yaml.safe_load(PLAN.read_text(encoding="utf-8"))
    matrix = yaml.safe_load(PLATFORMS.read_text(encoding="utf-8"))
    review = yaml.safe_load(REVIEW.read_text(encoding="utf-8"))
    reviews_passed = all(
        review[name]["status"] == "passed" for name in ("license_review", "cve_review", "platform_review")
    )
    assert (plan["gate_status"] == "passed") is (
        matrix["gate_status"] == "passed" and review["gate_status"] == "passed" and reviews_passed
    )
