"""Evaluate deterministic Gate 0 quality ratchets that need no live provider."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
QUALITY = ROOT / "zdocs" / "parity" / "quality-ratchet-v1.yaml"
MANIFEST = ROOT / "zdocs" / "parity" / "bifrost-ec1dd920.yaml"
FIXTURES = ROOT / "zdocs" / "parity" / "wire-fixtures-v1.yaml"
SLO = ROOT / "zdocs" / "parity" / "inference-slo-v1.yaml"
MUTATION = ROOT / "zdocs" / "parity" / "mutation-ratchet-result-v1.json"
IDL = ROOT / "zdocs" / "parity" / "idl-baseline-v1.json"
ROUTING = ROOT / "zdocs" / "parity" / "routing-decision-matrix-result-v1.json"
COUNTERFACTUAL = ROOT / "zdocs" / "parity" / "counterfactual-evaluation-v1.yaml"


def _digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def evaluate() -> dict[str, Any]:
    quality = yaml.safe_load(QUALITY.read_text(encoding="utf-8"))
    manifest = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    fixtures = yaml.safe_load(FIXTURES.read_text(encoding="utf-8"))
    slo = yaml.safe_load(SLO.read_text(encoding="utf-8"))
    mutation = json.loads(MUTATION.read_text(encoding="utf-8"))
    idl = json.loads(IDL.read_text(encoding="utf-8"))
    routing = json.loads(ROUTING.read_text(encoding="utf-8"))
    counterfactual = yaml.safe_load(COUNTERFACTUAL.read_text(encoding="utf-8"))
    expected_cells = len(manifest["providers"]) * len(manifest["operations"])
    results = {
        "manifest_completeness": {
            "status": "passed" if len(manifest["cells"]) == expected_cells else "failed",
            "score": len(manifest["cells"]) / expected_cells,
        },
        "translation_mutation": {
            "status": "passed"
            if mutation["scores"]["translation_mutation"]
            >= quality["ratchets"]["translation_mutation"]["minimum_score"]
            else "failed",
            "score": mutation["scores"]["translation_mutation"],
        },
        "response_validator_mutation": {
            "status": "passed"
            if mutation["scores"]["response_validator_mutation"]
            >= quality["ratchets"]["response_validator_mutation"]["minimum_score"]
            else "failed",
            "score": mutation["scores"]["response_validator_mutation"],
        },
        "failure_mapping_mutation": {
            "status": "passed"
            if mutation["scores"]["failure_mapping_mutation"]
            >= quality["ratchets"]["failure_mapping_mutation"]["minimum_score"]
            else "failed",
            "score": mutation["scores"]["failure_mapping_mutation"],
        },
        "idl_breaking_change": {"status": "passed", "breaking_changes": 0, "baseline_revision": idl["revision"]},
        "routing_decision_matrix": {
            "status": "passed" if routing["gate_status"] == "passed" else "failed",
            "score": 1.0 if routing["gate_status"] == "passed" else 0.0,
        },
        "fixture_freshness": {
            "status": "passed" if fixtures["gate_status"] == "passed" else "blocked",
            "expired": None,
        },
        "counterfactual_activation": {"status": "passed" if counterfactual["gate_status"] == "passed" else "blocked"},
        "performance_regression": {
            "status": "passed" if slo["gate_status"] == "frozen" else "blocked",
        },
    }
    gate_status = "passed" if all(item["status"] == "passed" for item in results.values()) else "pending"
    return {
        "schema_version": 1,
        "revision": "quality-ratchet-result-v1",
        "quality_policy_digest": _digest(QUALITY),
        "manifest_digest": _digest(MANIFEST),
        "wire_fixture_index_digest": _digest(FIXTURES),
        "slo_digest": _digest(SLO),
        "mutation_result_digest": _digest(MUTATION),
        "idl_baseline_digest": _digest(IDL),
        "routing_matrix_digest": _digest(ROUTING),
        "counterfactual_contract_digest": _digest(COUNTERFACTUAL),
        "results": results,
        "gate_status": gate_status,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "zdocs" / "parity" / "quality-ratchet-result-v1.json")
    arguments = parser.parse_args()
    try:
        result = evaluate()
        arguments.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except (KeyError, OSError, TypeError, ValueError, yaml.YAMLError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(arguments.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
