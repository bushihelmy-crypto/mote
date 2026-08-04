import json

import pytest

from mote.orchestration.automation.cron.migration import (
    activate_candidate,
    build_v3_candidate,
    inventory_v2,
    prepare_activation,
)
from mote.orchestration.automation.cron.store import CronTaskStore
from mote.orchestration.automation.cron.task import CronTask


def _v2_source(store: CronTaskStore) -> bytes:
    task = CronTask.new("* * * * *", "migrate", 1).to_dict()
    task["id"] = "01234567"
    body = {
        "schema": "mote.cron-schedule/v2",
        "schedule_id": store.load_snapshot().schedule_id,
        "revision": 7,
        "tasks": [task],
        "occurrences": [],
    }
    return (json.dumps(body, sort_keys=True, separators=(",", ":")) + "\n").encode()


def test_v2_inventory_candidate_readback_and_atomic_cutover(tmp_path) -> None:
    store = CronTaskStore(str(tmp_path))
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_bytes(_v2_source(store))
    inventory = inventory_v2(store.path)
    candidate = build_v3_candidate(store.path, tmp_path / "scheduled_tasks.v3-candidate.json")
    assert candidate.snapshot.revision == 7
    assert len(candidate.snapshot.tasks[0].id) == 32
    evidence = tmp_path / "migration-evidence" / "scheduled_tasks.v2.json"
    receipt = activate_candidate(
        candidate,
        store.path,
        evidence,
        expected_source_digest=inventory.source_digest,
        activation_manifest_path=tmp_path / "cron-activation-v3.json",
        activation_generation="cron-v3-g001",
        prepare_receipts=prepare_activation(candidate, "cron-v3-g001"),
    )
    assert receipt.legacy_v2_exited
    assert store.load_snapshot().tasks[0].prompt == "migrate"
    assert inventory_v2(evidence) == inventory


def test_v2_inventory_fails_closed_on_extra_fields(tmp_path) -> None:
    store = CronTaskStore(str(tmp_path))
    store.path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.loads(_v2_source(store))
    raw["extra"] = True
    store.path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="strict v2"):
        inventory_v2(store.path)


def test_cutover_rejects_incomplete_activation_cohort(tmp_path) -> None:
    store = CronTaskStore(str(tmp_path))
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_bytes(_v2_source(store))
    inventory = inventory_v2(store.path)
    candidate = build_v3_candidate(store.path, tmp_path / "candidate.json")
    with pytest.raises(ValueError, match="cohort"):
        activate_candidate(
            candidate,
            store.path,
            tmp_path / "evidence.json",
            expected_source_digest=inventory.source_digest,
            activation_manifest_path=tmp_path / "manifest.json",
            activation_generation="cron-v3-g001",
            prepare_receipts=(),
        )
