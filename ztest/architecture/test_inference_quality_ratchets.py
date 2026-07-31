import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
PARITY = ROOT / "zdocs" / "parity"


def test_mutation_ratchets_kill_every_frozen_mutant():
    result = json.loads((PARITY / "mutation-ratchet-result-v1.json").read_text(encoding="utf-8"))
    assert result["gate_status"] == "passed"
    assert all(mutation["status"] == "killed" for mutation in result["mutations"])
    assert all(score == 1.0 for score in result["scores"].values())


def test_routing_matrix_is_pure_and_covers_required_branches():
    policy = yaml.safe_load((PARITY / "quality-ratchet-v1.yaml").read_text(encoding="utf-8"))
    result = json.loads((PARITY / "routing-decision-matrix-result-v1.json").read_text(encoding="utf-8"))
    assert result["gate_status"] == "passed"
    assert set(result["results"]) >= set(policy["ratchets"]["routing_decision_matrix"]["required_branches"])
    assert result["results"]["dry_run"] == {
        "permit_signatures": 0,
        "selected_route_id": "standard",
        "state_commits": 0,
        "wire_requests": 0,
    }


def test_counterfactual_activation_remains_blocked_without_dataset_and_signature():
    contract = yaml.safe_load((PARITY / "counterfactual-evaluation-v1.yaml").read_text(encoding="utf-8"))
    latest = contract["latest_evaluation"]
    complete = (
        latest["dataset_digest"] is not None
        and latest["sample_size"] >= contract["activation_thresholds"]["minimum_sample_size"]
        and latest["cohort_balance_ratio"] is not None
        and latest["cohort_balance_ratio"] <= contract["activation_thresholds"]["cohort_balance_max_ratio"]
        and latest["confidence_interval"] is not None
        and latest["approval"] is not None
    )
    assert (contract["gate_status"] == "passed") is complete
