import json
from pathlib import Path

from zdocs.parity.gate_zero_status import EVIDENCE, aggregate

ROOT = Path(__file__).resolve().parents[2]
STATUS = ROOT / "zdocs" / "parity" / "gate-zero-status-v1.json"


def test_gate_zero_status_is_deterministic_and_fail_closed():
    stored = json.loads(STATUS.read_text(encoding="utf-8"))
    current = aggregate()
    assert stored == current
    ready = all(check["status"] == "passed" for check in current["checks"].values()) and current["evidence_complete"]
    assert current["ready_for_architecture_approval"] is ready
    assert (current["gate_status"] == "approved") is (ready and current["approval_record_present"])


def test_gate_zero_binds_every_required_evidence_group():
    current = aggregate()
    assert set(current["evidence"]) == set(EVIDENCE)
    assert current["evidence_complete"] is all(
        artifact["present"] for group in current["evidence"].values() for artifact in group.values()
    )
    for group in current["evidence"].values():
        for artifact in group.values():
            if artifact["present"]:
                assert artifact["digest"].startswith("sha256:")
