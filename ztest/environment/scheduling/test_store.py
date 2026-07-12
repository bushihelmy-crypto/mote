#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for CronTaskStore — durable JSON persistence + session-only memory."""

import json

from metagpt.environment.scheduling.store import CronTaskStore
from metagpt.environment.scheduling.task import CronTask


def make_store(tmp_path):
    return CronTaskStore(base_dir=str(tmp_path))


def test_durable_add_persists_to_disk(tmp_path):
    store = make_store(tmp_path)
    task = CronTask.new("* * * * *", "ping", 1000)
    store.add(task)
    # A fresh store reading the same dir sees it.
    reloaded = make_store(tmp_path).list()
    assert len(reloaded) == 1
    assert reloaded[0].id == task.id
    assert reloaded[0].durable is True


def test_atomic_write_shape(tmp_path):
    store = make_store(tmp_path)
    store.add(CronTask.new("* * * * *", "ping", 1000))
    data = json.loads(store.path.read_text())
    assert "tasks" in data
    assert isinstance(data["tasks"], list)
    assert data["tasks"][0]["prompt"] == "ping"


def test_session_only_not_on_disk(tmp_path):
    store = make_store(tmp_path)
    task = CronTask.new("* * * * *", "mem", 1000, durable=False)
    store.add(task)
    assert not store.path.exists()
    # In-memory list sees it; a fresh store does not.
    assert any(t.id == task.id for t in store.list())
    assert make_store(tmp_path).list() == []


def test_list_merges_durable_and_session(tmp_path):
    store = make_store(tmp_path)
    d = CronTask.new("* * * * *", "disk", 1000)
    m = CronTask.new("* * * * *", "mem", 1000, durable=False)
    store.add(d)
    store.add(m)
    ids = {t.id for t in store.list()}
    assert ids == {d.id, m.id}


def test_get(tmp_path):
    store = make_store(tmp_path)
    task = CronTask.new("* * * * *", "ping", 1000)
    store.add(task)
    assert store.get(task.id).prompt == "ping"
    assert store.get("missing") is None


def test_remove_counts_and_clears(tmp_path):
    store = make_store(tmp_path)
    d = CronTask.new("* * * * *", "disk", 1000)
    m = CronTask.new("* * * * *", "mem", 1000, durable=False)
    store.add(d)
    store.add(m)
    assert store.remove([d.id, m.id, "absent"]) == 2
    assert store.list() == []


def test_load_skips_malformed(tmp_path):
    store = make_store(tmp_path)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(
        json.dumps({"tasks": [{"id": "x"}, {"id": "y", "cron": "* * * * *", "prompt": "ok", "created_at": 1}]})
    )
    tasks = store.load()
    assert len(tasks) == 1
    assert tasks[0].prompt == "ok"


def test_load_missing_file(tmp_path):
    assert make_store(tmp_path).load() == []


def test_mtime_none_when_absent(tmp_path):
    store = make_store(tmp_path)
    assert store.mtime() is None
    store.add(CronTask.new("* * * * *", "ping", 1000))
    assert store.mtime() is not None
