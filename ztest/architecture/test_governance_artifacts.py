"""Deterministic governance artifact freshness and authority checks."""

import json
from pathlib import Path

from mote.contracts.composition.gates import GateEnforcement, GateResult
from mote.product.composition.gates import GATE_DECLARATIONS
from ztest.architecture.gate_status import execute_gate
from ztest.architecture.governance_artifact import build_governance_artifact

ROOT = Path(__file__).resolve().parents[2]
ARTIFACT = ROOT / "zdocs" / "architecture" / "dynamic-boundary-governance-v1.json"


def test_gate_declarations_are_unique_and_executable() -> None:
    gates = build_governance_artifact()["gates"]
    assert len({gate["gate_id"] for gate in gates}) == len(gates)
    assert all((gate["checker_status"] == "present") == (gate["fixed_command"] is not None) for gate in gates)


def test_committed_governance_artifact_is_fresh() -> None:
    assert json.loads(ARTIFACT.read_text(encoding="utf-8")) == build_governance_artifact()


def test_gate_execution_derives_result_and_preserves_raw_output(tmp_path) -> None:
    declaration = GATE_DECLARATIONS[0]
    executable = type(declaration)(
        gate_id=declaration.gate_id,
        authority=declaration.authority,
        checker_id=declaration.checker_id,
        checker_status=declaration.checker_status,
        fixed_command="printf architecture-evidence",
        declaration_owner=declaration.declaration_owner,
        final_hard_prerequisite=declaration.final_hard_prerequisite,
        evidence_schema=declaration.evidence_schema,
    )
    status = execute_gate(
        executable,
        source_paths=(ROOT / "ztest/architecture/test_local_imports.py",),
        enforcement=GateEnforcement.REPORT,
        evidence_directory=tmp_path,
    )
    assert status.result is GateResult.PASS
    assert "architecture-evidence" in (tmp_path / "ARCH-LOCAL-IMPORT.txt").read_text(encoding="utf-8")
