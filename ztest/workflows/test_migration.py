from __future__ import annotations

import json

import pytest

from mote.orchestration.workflows.migration import activate_candidate, build_v3_candidate, inventory_v2


def _source(path) -> None:
    path.write_text(
        json.dumps(
            {
                "schema": "mote.workflow-reconciliation/v2",
                "effects": [],
                "deliveries": [],
                "governance_cancellations": [],
            }
        ),
        encoding="utf-8",
    )


def test_workflow_v2_inventory_candidate_and_cutover(tmp_path) -> None:
    source = tmp_path / "reconciliation.json"
    candidate_path = tmp_path / "candidate.json"
    evidence = tmp_path / "evidence.json"
    _source(source)
    inventory = inventory_v2(source)
    candidate = build_v3_candidate(source, candidate_path)
    activate_candidate(candidate, source, evidence, expected_source_digest=inventory.source_digest)
    assert json.loads(source.read_text())["schema"] == "mote.workflow-reconciliation/v3"
    assert json.loads(evidence.read_text())["schema"] == "mote.workflow-reconciliation/v2"


def test_workflow_migration_rejects_corruption_and_changed_preimage(tmp_path) -> None:
    source = tmp_path / "reconciliation.json"
    source.write_text('{"schema":"unknown"}', encoding="utf-8")
    with pytest.raises(ValueError):
        inventory_v2(source)

    _source(source)
    inventory = inventory_v2(source)
    candidate = build_v3_candidate(source, tmp_path / "candidate.json")
    source.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError):
        activate_candidate(
            candidate,
            source,
            tmp_path / "evidence.json",
            expected_source_digest=inventory.source_digest,
        )
