#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the CronTask model + jitter config defaults."""

from mote.orchestration.environment.scheduling.task import DEFAULT_CRON_JITTER_CONFIG, CronJitterConfig, CronTask


def test_new_mints_8_hex_id():
    task = CronTask.new("* * * * *", "ping", 1000)
    assert len(task.id) == 8
    int(task.id, 16)  # parses as hex


def test_new_defaults():
    task = CronTask.new("* * * * *", "ping", 1000)
    assert task.recurring is False
    assert task.permanent is False
    assert task.durable is True
    assert task.last_fired_at is None
    assert task.agent_id is None
    assert task.target_session_id is None


def test_to_dict_omits_none_and_defaults():
    task = CronTask.new("* * * * *", "ping", 1000)
    d = task.to_dict()
    assert d == {"id": task.id, "cron": "* * * * *", "prompt": "ping", "created_at": 1000}


def test_to_dict_includes_set_fields():
    task = CronTask.new(
        "* * * * *",
        "ping",
        1000,
        recurring=True,
        agent_id="agt",
        target_session_id="sess",
    )
    task.last_fired_at = 2000
    d = task.to_dict()
    assert d["recurring"] is True
    assert d["last_fired_at"] == 2000
    assert d["agent_id"] == "agt"
    assert d["target_session_id"] == "sess"
    assert "permanent" not in d


def test_round_trip():
    task = CronTask.new(
        "0 9 * * *",
        "review PRs",
        12345,
        recurring=True,
        permanent=True,
        target_session_id="root",
    )
    task.last_fired_at = 67890
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


def test_default_jitter_config_values():
    cfg = DEFAULT_CRON_JITTER_CONFIG
    assert isinstance(cfg, CronJitterConfig)
    assert cfg.recurring_frac == 0.1
    assert cfg.recurring_cap_ms == 15 * 60 * 1000
    assert cfg.one_shot_max_ms == 90 * 1000
    assert cfg.one_shot_minute_mod == 30
    assert cfg.recurring_max_age_ms == 7 * 24 * 60 * 60 * 1000
