"""Aggregate Gate 0 evidence without manufacturing approval authority."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
PARITY = ROOT / "zdocs" / "parity"
OUTPUT = PARITY / "gate-zero-status-v1.json"
APPROVAL = PARITY / "GATE_0_APPROVED.yaml"

CHECKS = {
    "provider_certification": ("provider-certification-v1.yaml", "gate_status", "passed"),
    "dependency_supply_chain": ("dependency-plan-v1.yaml", "gate_status", "passed"),
    "wire_fixtures": ("wire-fixtures-v1.yaml", "gate_status", "passed"),
    "fault_matrix": ("fault-matrix-v1.yaml", "gate_status", "passed"),
    "quality_ratchet": ("quality-ratchet-result-v1.json", "gate_status", "passed"),
    "slo": ("inference-slo-v1.yaml", "gate_status", "frozen"),
}

EVIDENCE = {
    "parity_manifest": ("bifrost-ec1dd920.yaml",),
    "provider_certification": ("provider-certification-v1.yaml",),
    "failure_and_execution_contracts": ("canonical-failure-v2.yaml", "execution-contracts-v1.yaml"),
    "config_and_network": (
        "inference-config-v1.schema.json",
        "inference-config-semantics-v1.yaml",
        "private-network-policy-v1.yaml",
    ),
    "external_idl_and_rbac": ("idl-baseline-v1.json", "admin-rbac-v1.yaml"),
    "wire_fixtures": ("wire-fixtures-v1.yaml",),
    "shared_deployment": (
        "shared-daemon-v1.md",
        "shared-sqlite-semantics-v1.yaml",
        "deployment-distribution-scope-v1.yaml",
    ),
    "dependency_supply_chain": (
        "dependency-plan-v1.yaml",
        "inference-sbom-v1.cdx.json",
        "dependency-review-v1.yaml",
        "dependency-platform-matrix-v1.yaml",
    ),
    "external_release_inputs": ("external-release-inputs-v1.yaml",),
    "translation_replay_routing": (
        "translation-profiles-v1.yaml",
        "reasoning-replay-v1.yaml",
        "routing-decision-v1.yaml",
        "counterfactual-evaluation-v1.yaml",
    ),
    "validation_and_inspection": ("response-validator-v1.yaml", "traffic-inspector-v1.yaml"),
    "quality_and_reuse": ("quality-ratchet-result-v1.json", "infrastructure-reuse-audit-v1.yaml"),
    "backup_and_recovery": (
        "recovery-contracts-v1.yaml",
        "gateway-daemon-backup-v1.schema.json",
        "mote-application-backup-v1.schema.json",
        "mote-recovery-set-v1.schema.json",
    ),
    "reconciliation": ("reconciliation-v1.md", "reconciliation-actions-v1.yaml"),
    "observability_and_operations": (
        "observability-operations-v1.yaml",
        "operations/alert-catalog-v1.yaml",
        "operations/readiness-v1.md",
        "release-cli-upgrade-v1.yaml",
    ),
    "workgraph_slo_and_faults": ("inference-workgraph-v1.yaml", "inference-slo-v1.yaml", "fault-matrix-v1.yaml"),
}


def _read(path: Path) -> dict[str, Any]:
    if path.suffix == ".json":
        return json.loads(path.read_text(encoding="utf-8"))
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def aggregate() -> dict[str, Any]:
    checks = {}
    for name, (relative, field, expected) in CHECKS.items():
        path = PARITY / relative
        document = _read(path)
        actual = document.get(field)
        checks[name] = {
            "status": "passed" if actual == expected else "blocked",
            "expected": expected,
            "actual": actual,
            "artifact": relative,
            "digest": _digest(path),
        }
    ready = all(check["status"] == "passed" for check in checks.values())
    evidence = {}
    evidence_complete = True
    for name, relatives in EVIDENCE.items():
        artifacts = {}
        for relative in relatives:
            path = PARITY / relative
            present = path.is_file() and path.stat().st_size > 0
            evidence_complete = evidence_complete and present
            artifacts[relative] = {
                "present": present,
                "digest": _digest(path) if present else None,
            }
        evidence[name] = artifacts
    ready = ready and evidence_complete
    return {
        "schema_version": 1,
        "revision": "gate-zero-status-v1",
        "checks": checks,
        "evidence": evidence,
        "evidence_complete": evidence_complete,
        "ready_for_architecture_approval": ready,
        "approval_record_present": APPROVAL.is_file(),
        "gate_status": "approved" if ready and APPROVAL.is_file() else "blocked",
    }


def main() -> int:
    document = aggregate()
    OUTPUT.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(OUTPUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
