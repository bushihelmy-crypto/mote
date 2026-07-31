import json
from pathlib import Path

import yaml

from zdocs.parity.measure_inference_slo import _nearest_rank

ROOT = Path(__file__).resolve().parents[2]
SLO = ROOT / "zdocs" / "parity" / "inference-slo-v1.yaml"
CANDIDATE = ROOT / "zdocs" / "parity" / "inference-slo-candidate-v1.json"
SHARED_RPC_CANDIDATE = ROOT / "zdocs" / "parity" / "shared-rpc-slo-candidate-v1.json"


def test_nearest_rank_quantiles_are_deterministic():
    values = list(range(1, 1001))
    assert _nearest_rank(values, 0.5) == 500
    assert _nearest_rank(values, 0.99) == 990
    assert _nearest_rank(values, 0.999) == 999


def test_slo_protocol_cannot_claim_frozen_with_unmeasured_dimensions():
    slo = yaml.safe_load(SLO.read_text(encoding="utf-8"))
    all_measured = all(dimension["status"] == "measured" for dimension in slo["dimensions"].values())
    assert (slo["gate_status"] == "frozen") is all_measured


def test_candidate_result_is_explicitly_non_authoritative():
    if not CANDIDATE.exists():
        return
    candidate = json.loads(CANDIDATE.read_text(encoding="utf-8"))
    assert candidate["status"] == "candidate_not_frozen"
    assert candidate["quantile_method"] == "nearest_rank"
    assert candidate["measured_iterations"] >= 1000
    assert set(candidate["environment"]) == {
        "platform",
        "machine",
        "processor",
        "python",
        "implementation",
        "cpu_count",
    }
    assert {
        "generation_acquire_release_us",
        "queue_enqueue_dequeue_us",
        "permit_canonicalize_us",
        "permit_sign_verify_us",
        "receipt_transaction_ms",
        "event_persistence_ms",
    } <= set(candidate["dimensions"])


def test_shared_rpc_candidate_is_explicitly_non_authoritative():
    candidate = json.loads(SHARED_RPC_CANDIDATE.read_text(encoding="utf-8"))
    measurement = candidate["shared_rpc_hop_ms"]
    assert candidate["status"] == "candidate_not_frozen"
    assert measurement["unit"] == "ms"
    assert measurement["samples"] >= 1000
    assert measurement["p50"] <= measurement["p99"] <= measurement["p99_9"]


def test_slo_protocol_references_both_candidate_artifacts():
    slo = yaml.safe_load(SLO.read_text(encoding="utf-8"))
    assert slo["candidate_result"] == {
        "status": "candidate_not_frozen",
        "local_execution": CANDIDATE.name,
        "shared_rpc": SHARED_RPC_CANDIDATE.name,
    }
