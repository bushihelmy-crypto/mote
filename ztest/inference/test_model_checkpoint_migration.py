from __future__ import annotations

import asyncio
import json

import pytest

from mote.contracts.model import AttemptBudget, ModelCallPlannedRecord
from mote.product.config.model_checkpoint import approved_model_checkpoint_policy
from mote.product.migrations.model_checkpoint import (
    ModelCheckpointMigrationDisposition,
    inventory_model_checkpoint_sources,
    write_model_checkpoint_activation_manifest,
)
from mote.runtime.models.failover import LocalModelCallJournal


def _plan(call_id: str) -> ModelCallPlannedRecord:
    return ModelCallPlannedRecord(
        model_call_id=call_id,
        plan_id="plan",
        route_id="default",
        runtime_generation_id="runtime",
        topology_revision="topology",
        config_revision="config",
        endpoint_ids=("endpoint",),
        budget=AttemptBudget(),
    )


def test_inventory_accepts_canonical_model_call_and_retires_projection(
    tmp_path,
) -> None:
    journal = LocalModelCallJournal(
        tmp_path / ".runtime" / "model-calls",
        policy=approved_model_checkpoint_policy(),
    )
    asyncio.run(journal.append(_plan("call-1")))
    projection = tmp_path / ".agent_sessions" / "session" / "ledger" / "model-session-projections.jsonl"
    projection.parent.mkdir(parents=True)
    projection.write_text('{"projection":"rebuildable"}\n', encoding="utf-8")

    inventory = inventory_model_checkpoint_sources(tmp_path)

    assert {source.disposition for source in inventory.sources} == {
        ModelCheckpointMigrationDisposition.CANONICAL,
        ModelCheckpointMigrationDisposition.RETIRE_PROJECTION,
    }
    manifest = tmp_path / "model-checkpoint-activation.json"
    write_model_checkpoint_activation_manifest(inventory, manifest)
    assert json.loads(manifest.read_bytes())["legacy_production_reader"] == "retired"


def test_corrupt_or_unknown_model_call_blocks_activation_without_replacement(
    tmp_path,
) -> None:
    source = tmp_path / ".runtime" / "model-calls" / "bad.jsonl"
    source.parent.mkdir(parents=True)
    source.write_bytes(b'{"kind":"future","schema_version":999}\n')
    before = source.read_bytes()

    inventory = inventory_model_checkpoint_sources(tmp_path)

    assert inventory.blocked
    with pytest.raises(RuntimeError, match="forbids activation"):
        write_model_checkpoint_activation_manifest(inventory, tmp_path / "activation.json")
    assert source.read_bytes() == before
    assert not (tmp_path / "activation.json").exists()
