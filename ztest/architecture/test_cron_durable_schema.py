from __future__ import annotations

import json

import pytest

from mote.orchestration.automation.cron.lock import SchedulerLock
from mote.orchestration.automation.cron.store import (
    CronRevisionConflict,
    CronStoreCorruptionError,
    CronTailTornWriteError,
    CronTaskStore,
)
from mote.orchestration.automation.cron.task import CronTask, CronTriggerIntent, DurableCronTaskId, SessionCronTaskId
from mote.runtime.clock import SystemClock


def _store(tmp_path) -> CronTaskStore:
    return CronTaskStore(base_dir=str(tmp_path))


def _persisted(tmp_path) -> tuple[CronTaskStore, CronTask, dict[str, object]]:
    store = _store(tmp_path)
    task = store.add(
        CronTask.new("* * * * *", "ping", 1000, recurring=True),
        capacity_limit=50,
    )
    return store, task, json.loads(store.path.read_text(encoding="utf-8"))


def test_v1_schedule_task_and_trigger_round_trip(tmp_path) -> None:
    store, task, raw = _persisted(tmp_path)

    restored = store.load_snapshot()
    trigger = CronTriggerIntent(
        schema_version=1,
        task_id=restored.tasks[0].id,
        task_revision=restored.tasks[0].revision,
        target="session-1",
        content="ping",
        scheduled_at_ms=2000,
        fired_at_ms=2001,
        attempt=1,
    ).to_automation_trigger()

    assert raw["schema"] == "mote.cron-schedule/v3"
    assert restored.revision == 1
    assert restored.tasks[0].to_dict() == task.to_dict()
    assert type(restored.tasks[0].id) is DurableCronTaskId
    assert trigger.trigger_id == f"cron:{task.id}:0:2000"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda body: body.update(schema="mote.cron-schedule/v99"),
        lambda body: body.update(extra=True),
        lambda body: body.pop("revision"),
        lambda body: body.update(revision=True),
        lambda body: body.update(schedule_id="wrong"),
        lambda body: body["tasks"][0].update(revision="0"),
        lambda body: body["tasks"][0].update(unexpected=True),
    ],
)
def test_invalid_envelope_or_task_fails_closed_and_is_quarantined(tmp_path, mutation) -> None:
    store, _task, body = _persisted(tmp_path)
    mutation(body)
    corrupt = json.dumps(body)
    store.path.write_text(corrupt, encoding="utf-8")

    with pytest.raises(CronStoreCorruptionError):
        store.load()

    assert store.path.read_text(encoding="utf-8") == corrupt
    assert len(list(tmp_path.glob("scheduled_tasks.json.quarantine-*.json"))) == 1
    with pytest.raises(CronStoreCorruptionError):
        store.add(
            CronTask.new("* * * * *", "must not overwrite", 2000),
            capacity_limit=50,
        )
    assert store.path.read_text(encoding="utf-8") == corrupt


def test_middle_corruption_and_tail_torn_write_are_distinct(tmp_path) -> None:
    store = _store(tmp_path)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text('{"schema": nope, "tasks":[]}', encoding="utf-8")
    with pytest.raises(CronStoreCorruptionError) as middle:
        store.load()
    assert not isinstance(middle.value, CronTailTornWriteError)

    store.path.write_text('{"schema":"mote.cron-schedule/v1"', encoding="utf-8")
    with pytest.raises(CronTailTornWriteError):
        store.load()


def test_schedule_and_task_cas_prevent_lost_or_double_progression(tmp_path) -> None:
    first, task, _body = _persisted(tmp_path)
    lock = SchedulerLock(
        "durable-schema-test",
        base_dir=str(first.path.parent),
        clock_source=SystemClock(),
    )
    fence = lock.acquire()
    assert fence is not None
    stale = _store(tmp_path).load_snapshot()
    occurrence = first.claim_occurrence(
        fence=fence,
        task_id=str(task.id),
        expected_task_revision=task.revision,
        scheduled_at_ms=1500,
        observed_at_ms=2000,
        delete_on_accept=False,
    )
    dispatch = first.begin_dispatch(occurrence.occurrence_id, fence=fence, now_ms=2000)
    from mote.orchestration.automation import TriggerDisposition, TriggerReceipt

    first.settle_receipt(
        dispatch.occurrence_id,
        fence=fence,
        expected_attempt=dispatch.attempt,
        receipt=TriggerReceipt(TriggerDisposition.ACCEPTED, receipt_id="receipt-1"),
        settled_at_ms=2000,
    )

    with pytest.raises(CronRevisionConflict):
        _store(tmp_path).save(list(stale.tasks), expected_revision=stale.revision)
    with pytest.raises(CronRevisionConflict):
        first.settle_receipt(
            dispatch.occurrence_id,
            fence=fence,
            expected_attempt=dispatch.attempt,
            receipt=TriggerReceipt(TriggerDisposition.ACCEPTED, receipt_id="receipt-1"),
            settled_at_ms=2000,
        )

    current = first.load_snapshot()
    assert current.revision == 4
    assert current.tasks[0].revision == 1
    assert current.tasks[0].last_fired_at == 2000


def test_session_identity_is_not_a_durable_identity() -> None:
    task = CronTask.new("* * * * *", "local", 1000, durable=False)

    assert type(task.id) is SessionCronTaskId
    with pytest.raises(ValueError, match="session-only"):
        task.to_dict()
