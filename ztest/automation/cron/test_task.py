#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the CronTask model + jitter config defaults."""

from dataclasses import FrozenInstanceError, replace

import pytest

from mote.orchestration.automation.cron.task import (
    DEFAULT_CRON_JITTER_CONFIG,
    CronJitterConfig,
    CronTask,
    DurableCronTaskId,
    SessionCronTaskId,
)


def test_new_mints_128_bit_hex_id():
    task = CronTask.new("* * * * *", "ping", 1000)
    assert len(task.id) == 32
    int(task.id, 16)  # parses as hex


def test_new_defaults():
    task = CronTask.new("* * * * *", "ping", 1000)
    assert task.recurring is False
    assert task.permanent is False
    assert task.durable is True
    assert task.last_fired_at is None
    assert task.agent_id is None
    assert task.target_session_id is None
    assert task.revision == 0
    assert type(task.id) is DurableCronTaskId


def test_to_dict_has_exact_canonical_shape():
    task = CronTask.new("* * * * *", "ping", 1000)
    d = task.to_dict()
    assert d == {
        "id": str(task.id),
        "revision": 0,
        "cron": "* * * * *",
        "prompt": "ping",
        "prompt_artifact_ref": None,
        "created_at": 1000,
        "last_fired_at": None,
        "recurring": False,
        "permanent": False,
        "agent_id": None,
        "target_session_id": None,
        "timezone_name": "UTC",
        "misfire_policy": "fire_once",
        "overlap_policy": "forbid",
        "dst_policy": "earliest_fold_skip_gap",
    }


def test_to_dict_includes_set_fields():
    task = CronTask.new(
        "* * * * *",
        "ping",
        1000,
        recurring=True,
        agent_id="agt",
        target_session_id="sess",
    )
    task = replace(task, last_fired_at=2000)
    d = task.to_dict()
    assert d["recurring"] is True
    assert d["last_fired_at"] == 2000
    assert d["agent_id"] == "agt"
    assert d["target_session_id"] == "sess"
    assert d["permanent"] is False


def test_round_trip():
    task = CronTask.new(
        "0 9 * * *",
        "review PRs",
        12345,
        recurring=True,
        permanent=True,
        target_session_id="root",
    )
    task = replace(task, last_fired_at=67890)
    restored = CronTask.from_dict(task.to_dict())
    # durable defaults True on disk read.
    assert restored.id == task.id
    assert restored.cron == task.cron
    assert restored.prompt == task.prompt
    assert restored.created_at == task.created_at
    assert restored.last_fired_at == task.last_fired_at
    assert restored.recurring is True
    assert restored.permanent is True
    assert restored.target_session_id == "root"
    assert type(restored.id) is DurableCronTaskId


def test_session_task_has_distinct_identity_and_cannot_be_serialized():
    task = CronTask.new("* * * * *", "ping", 1000, durable=False)
    assert type(task.id) is SessionCronTaskId
    with pytest.raises(ValueError, match="session-only"):
        task.to_dict()


def test_cron_task_snapshot_is_immutable() -> None:
    task = CronTask.new("* * * * *", "prompt", 1)

    with pytest.raises(FrozenInstanceError):
        task.revision = 2


def test_default_jitter_config_values():
    cfg = DEFAULT_CRON_JITTER_CONFIG
    assert isinstance(cfg, CronJitterConfig)
    assert cfg.recurring_frac == 0.1
    assert cfg.recurring_cap_ms == 15 * 60 * 1000
    assert cfg.one_shot_max_ms == 90 * 1000
    assert cfg.one_shot_minute_mod == 30
    assert cfg.recurring_max_age_ms == 7 * 24 * 60 * 60 * 1000
