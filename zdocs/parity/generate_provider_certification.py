"""Generate the Gate 0 live-resource inventory from the parity manifest."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
from typing import Any

import yaml

REQUIRED_STATUSES = {"supported", "conditional", "provider_managed"}


def _digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def generate(parity_path: Path) -> dict[str, Any]:
    parity = yaml.safe_load(parity_path.read_text(encoding="utf-8"))
    resources = []
    for cell in parity["cells"]:
        if cell["status"] not in REQUIRED_STATUSES:
            continue
        if not {"current_embedded", "current_shared"} & set(cell["release_scope"]):
            continue
        provider = cell["provider"]
        operation = cell["operation"]
        resources.append(
            {
                "cell_id": f"{provider}.{operation}",
                "provider": provider,
                "operation": operation,
                "credential_class": cell["auth"]["credential_class"],
                "account_project_region_capabilities": ["operation_enabled", "test_region_approved"],
                "test_model_or_deployment": "TO_BE_PROVISIONED",
                "estimated_test_budget": {"currency": "USD", "per_run": 0, "monthly_cap": 0},
                "side_effects": ["provider_resource"] if cell["side_effect"] else [],
                "cleanup": {
                    "procedure": "delete_by_certification_run_id" if cell["side_effect"] else "not_applicable",
                    "deadline": "PT24H" if cell["side_effect"] else "not_applicable",
                    "verifier": "provider_receipt_reconciliation" if cell["side_effect"] else "not_applicable",
                },
                "fixture_freshness": {"recorded_at": None, "max_age_days": 30, "schema_digest": None},
                "outage_evidence": {
                    "required_fields": ["incident_id", "observed_at", "region", "model", "probe_digest"],
                    "max_age_hours": 24,
                },
                "latest_live_certification_digest": None,
                "conditional_capability_rule": (
                    "provider_capability_probe" if cell["status"] == "conditional" else None
                ),
                "resource_owner": "UNASSIGNED",
                "resource_ready": False,
                "required_suites": ["offline_protocol", "recorded_contract", "live_certification"],
            }
        )
    return {
        "schema_version": 1,
        "parity_manifest_digest": _digest(parity_path),
        "baseline_commit": parity["baseline"]["commit"],
        "release_scope": ["current_embedded", "current_shared"],
        "resources": resources,
        "gate_status": "awaiting_resource_provisioning",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--parity", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    document = generate(args.parity.resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


if __name__ == "__main__":
    main()
