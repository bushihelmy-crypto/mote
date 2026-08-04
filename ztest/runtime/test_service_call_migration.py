from __future__ import annotations

import hashlib
import json

import pytest

from mote.contracts.model.failover import AttemptBudget
from mote.contracts.service import (
    MediaGenerationPayload,
    MediaGenerationSpec,
    MediaKind,
    ServiceCallPlannedRecord,
    ServiceExecutionSemantics,
)
from mote.runtime.service_gateway.journal import LocalServiceCallJournal
from mote.runtime.service_gateway.migration import (
    activate_candidate,
    build_v3_candidate,
    inventory_v2,
    migrate_service_call_root_v3,
)


def _legacy_source(path) -> None:
    record = ServiceCallPlannedRecord(
        service_call_id="migration-call",
        plan_id="plan",
        route_id="media.image",
        capability="media.generate.image",
        payload=MediaGenerationPayload(
            media_kind=MediaKind.IMAGE,
            item=MediaGenerationSpec(description="test", filename="a.png"),
        ),
        config_revision="config-1",
        endpoint_ids=("endpoint",),
        budget=AttemptBudget(),
        policy_id="policy",
        semantics=ServiceExecutionSemantics.IDEMPOTENT,
        idempotency_key="key",
    )
    raw = record.model_dump(mode="json")
    raw["schema_version"] = 2
    path.write_text(json.dumps(raw) + "\n", encoding="utf-8")


def test_service_call_v2_inventory_candidate_and_cutover(tmp_path) -> None:
    source = tmp_path / "source.jsonl"
    _legacy_source(source)
    inventory = inventory_v2(source)
    candidate = build_v3_candidate(source, tmp_path / "candidate.jsonl")
    evidence = tmp_path / "evidence.jsonl"
    activate_candidate(candidate, source, evidence, expected_source_digest=inventory.source_digest)
    assert json.loads(source.read_text())["schema_version"] == 3
    assert json.loads(evidence.read_text())["schema_version"] == 2


def test_service_call_migration_rejects_corrupt_stream(tmp_path) -> None:
    source = tmp_path / "source.jsonl"
    source.write_text('{"kind":"unknown","schema_version":2}\n', encoding="utf-8")
    with pytest.raises(ValueError):
        inventory_v2(source)


def test_service_call_root_cutover_includes_owner_cancel_and_manifest(tmp_path) -> None:
    root = tmp_path / "service-calls"
    root.mkdir()
    filename = hashlib.sha256(b"migration-call").hexdigest()
    stream = root / f"{filename}.jsonl"
    _legacy_source(stream)
    stream.with_suffix(".owner.json").write_text('{"generation":2}', encoding="utf-8")
    stream.with_suffix(".cancel").write_text('{"command":"cancel","schema":1}', encoding="utf-8")
    receipt = migrate_service_call_root_v3(root)
    assert receipt.stream_count == 1
    assert receipt.evidence_path.is_dir()
    journal = LocalServiceCallJournal(root)
    assert len(journal.records("migration-call")) == 1
    assert journal.cancellation_requested("migration-call") is True
    owner = json.loads(stream.with_suffix(".owner.json").read_text(encoding="utf-8"))
    assert owner["schema_version"] == 3
    assert owner["service_call_id"] == "migration-call"
