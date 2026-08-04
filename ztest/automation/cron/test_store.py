#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for CronTaskStore — durable JSON persistence + session-only memory."""

import json

import pytest

from mote.orchestration.automation.cron.store import CronRevisionConflict, CronStoreCorruptionError, CronTaskStore
from mote.orchestration.automation.cron.task import CronTask


def make_store(tmp_path):
    return CronTaskStore(base_dir=str(tmp_path))


def test_durable_add_persists_to_disk(tmp_path):
    store = make_store(tmp_path)
    task = CronTask.new("* * * * *", "ping", 1000)
    store.add(task, capacity_limit=50)
    # A fresh store reading the same dir sees it.
    reloaded = make_store(tmp_path).list()
    assert len(reloaded) == 1
    assert reloaded[0].id == task.id
    assert reloaded[0].durable is True


def test_atomic_write_shape(tmp_path):
    store = make_store(tmp_path)
    store.add(CronTask.new("* * * * *", "ping", 1000), capacity_limit=50)
    data = json.loads(store.path.read_text())
    assert data["schema"] == "mote.cron-schedule/v3"
    assert data["revision"] == 1
    assert "tasks" in data
    assert isinstance(data["tasks"], list)
    assert data["tasks"][0]["prompt"] == "ping"


def test_session_only_not_on_disk(tmp_path):
    store = make_store(tmp_path)
    task = CronTask.new("* * * * *", "mem", 1000, durable=False)
    store.add(task, capacity_limit=50)
    assert not store.path.exists()
    # In-memory list sees it; a fresh store does not.
    assert any(t.id == task.id for t in store.list())
    assert make_store(tmp_path).list() == []


def test_list_merges_durable_and_session(tmp_path):
    store = make_store(tmp_path)
    d = CronTask.new("* * * * *", "disk", 1000)
    m = CronTask.new("* * * * *", "mem", 1000, durable=False)
    store.add(d, capacity_limit=50)
    store.add(m, capacity_limit=50)
    ids = {t.id for t in store.list()}
    assert ids == {d.id, m.id}


def test_get(tmp_path):
    store = make_store(tmp_path)
    task = CronTask.new("* * * * *", "ping", 1000)
    store.add(task, capacity_limit=50)
    assert store.get(task.id).prompt == "ping"
    assert store.get("missing") is None


def test_remove_counts_and_clears(tmp_path):
    store = make_store(tmp_path)
    d = CronTask.new("* * * * *", "disk", 1000)
    m = CronTask.new("* * * * *", "mem", 1000, durable=False)
    store.add(d, capacity_limit=50)
    store.add(m, capacity_limit=50)
    assert store.remove([d.id, m.id, "absent"]) == 2
    assert store.list() == []


def test_load_quarantines_malformed_and_fails_closed(tmp_path):
    store = make_store(tmp_path)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(
        json.dumps({"tasks": [{"id": "x"}, {"id": "y", "cron": "* * * * *", "prompt": "ok", "created_at": 1}]})
    )
    with pytest.raises(CronStoreCorruptionError):
        store.load()
    assert store.path.exists()
    assert list(tmp_path.glob("scheduled_tasks.json.quarantine-*.json"))


def test_load_missing_file(tmp_path):
    assert make_store(tmp_path).load() == []


def test_snapshot_revision_advances_without_filesystem_metadata(tmp_path):
    store = make_store(tmp_path)
    assert store.load_snapshot().revision == 0
    store.add(CronTask.new("* * * * *", "ping", 1000), capacity_limit=50)
    assert store.load_snapshot().revision == 1


def test_save_requires_matching_schedule_revision(tmp_path):
    store = make_store(tmp_path)
    task = store.add(CronTask.new("* * * * *", "ping", 1000), capacity_limit=50)
    with pytest.raises(CronRevisionConflict):
        store.save([task], expected_revision=0)


def test_noncanonical_shape_fails_closed_without_rewrite(tmp_path):
    store = make_store(tmp_path)
    task = CronTask.new("* * * * *", "legacy", 1000)
    legacy = task.to_dict()
    legacy.pop("revision")
    for key in ("last_fired_at", "recurring", "permanent", "agent_id", "target_session_id"):
        if legacy[key] in (None, False):
            legacy.pop(key)
    for key in ("timezone_name", "misfire_policy", "overlap_policy", "dst_policy"):
        legacy.pop(key)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(json.dumps({"tasks": [legacy]}), encoding="utf-8")

    original = store.path.read_text(encoding="utf-8")
    with pytest.raises(CronStoreCorruptionError):
        store.load_snapshot()
    assert store.path.read_text(encoding="utf-8") == original
