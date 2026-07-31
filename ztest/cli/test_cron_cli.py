#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for ``mote.product.cli cron`` — imperative CRUD over the scheduled-task store."""

import pytest

from mote.orchestration.automation.cron.store import CronTaskStore
from mote.product.entrypoints.cron import cli as cron_cli


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Point the CLI's store at a temp dir (never touch the real ~/.mote)."""
    monkeypatch.setattr(
        cron_cli,
        "CronTaskStore",
        lambda **_kwargs: CronTaskStore(base_dir=str(tmp_path)),
    )
    return CronTaskStore(base_dir=str(tmp_path))


def test_add_persists_and_reports(store, capsys):
    rc = cron_cli.main(["add", "*/5 * * * *", "ping", "--recurring"])
    assert rc == 0
    tasks = store.list()
    assert len(tasks) == 1
    assert tasks[0].prompt == "ping"
    assert tasks[0].recurring is True
    assert tasks[0].id in capsys.readouterr().out


def test_add_with_explicit_session(store, capsys):
    cron_cli.main(["add", "0 9 * * *", "standup", "--session", "sess-42"])
    assert store.list()[0].target_session_id == "sess-42"


def test_add_default_target_is_none(store):
    cron_cli.main(["add", "0 9 * * *", "standup"])
    assert store.list()[0].target_session_id is None


def test_add_rejects_invalid_cron(store, capsys):
    rc = cron_cli.main(["add", "nope", "x"])
    assert rc == 1
    assert "error" in capsys.readouterr().out
    assert store.list() == []


def test_list_empty(store, capsys):
    rc = cron_cli.main(["list"])
    assert rc == 0
    assert "no scheduled tasks" in capsys.readouterr().out


def test_list_shows_tasks(store, capsys):
    cron_cli.main(["add", "* * * * *", "hello world", "--session", "s1"])
    capsys.readouterr()  # clear the add line
    cron_cli.main(["list"])
    out = capsys.readouterr().out
    assert "hello world" in out
    assert "s1" in out


def test_list_marks_active_session_default(store, capsys):
    cron_cli.main(["add", "* * * * *", "hi"])
    capsys.readouterr()
    cron_cli.main(["list"])
    assert "active session" in capsys.readouterr().out


def test_rm_removes(store, capsys):
    cron_cli.main(["add", "* * * * *", "x"])
    task_id = store.list()[0].id
    capsys.readouterr()
    rc = cron_cli.main(["rm", task_id])
    assert rc == 0
    assert "removed 1" in capsys.readouterr().out
    assert store.list() == []


def test_rm_unknown_id_reports_zero(store, capsys):
    rc = cron_cli.main(["rm", "deadbeef"])
    assert rc == 0
    assert "removed 0" in capsys.readouterr().out
