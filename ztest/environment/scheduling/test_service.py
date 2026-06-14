#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for CronService — glue to AgentControl.send_input(TRIGGER_TURN)."""

import types
from datetime import datetime

import pytest

from metagpt.common.schema.queue import MessageQueue
from metagpt.environment.agent_path import AgentPath
from metagpt.environment.control import AgentControl
from metagpt.environment.mailbox import DeliveryMode
from metagpt.environment.registry import AgentMetadata
from metagpt.environment.runtime import AgentRuntime
from metagpt.environment.scheduling.service import MAX_CRON_TASKS, CronService
from metagpt.environment.scheduling.store import CronTaskStore
from metagpt.environment.scheduling.task import CronTask


def _ms(year, month, day, hour=0, minute=0, second=0):
    return int(datetime(year, month, day, hour, minute, second).timestamp() * 1000)


# --- fakes -----------------------------------------------------------------


class FakeRuntime:
    def __init__(self, active_turn=False):
        self.active_turn = active_turn


class FakeControl:
    """Minimal AgentControl stand-in recording send_input calls."""

    def __init__(self, runtimes=None, *, raise_on=None):
        self.session_id = "ctrl"
        self._rt = runtimes or {}
        self._raise_on = raise_on
        self.sent = []

    def runtimes(self):
        return dict(self._rt)

    def send_input(self, agent_id, message, *, mode=DeliveryMode.TRIGGER_TURN):
        if self._raise_on is not None and agent_id == self._raise_on:
            raise KeyError(agent_id)
        self.sent.append((agent_id, message, mode))


def make_service(tmp_path, control=None, **kwargs):
    return CronService(control or FakeControl(), base_dir=str(tmp_path), **kwargs)


# --- task management -------------------------------------------------------


def test_create_task_registers(tmp_path):
    svc = make_service(tmp_path)
    task = svc.create_task("*/5 * * * *", "ping", "sess", recurring=True)
    assert isinstance(task, CronTask)
    assert task.target_session_id == "sess"
    assert task.recurring is True
    assert len(svc.list_tasks()) == 1


def test_create_rejects_invalid_cron(tmp_path):
    svc = make_service(tmp_path)
    with pytest.raises(ValueError):
        svc.create_task("not a cron", "ping", "sess")


def test_create_rejects_at_cap(tmp_path):
    store = CronTaskStore(base_dir=str(tmp_path))
    for _ in range(MAX_CRON_TASKS):
        store.add(CronTask.new("* * * * *", "x", _ms(2026, 6, 15)))
    svc = make_service(tmp_path)
    with pytest.raises(ValueError):
        svc.create_task("* * * * *", "overflow", "sess")


def test_list_filter_by_agent(tmp_path):
    svc = make_service(tmp_path)
    svc.create_task("* * * * *", "a", "sess", durable=False, agent_id="agt1")
    svc.create_task("* * * * *", "b", "sess", durable=False, agent_id="agt2")
    assert len(svc.list_tasks(agent_id="agt1")) == 1
    assert len(svc.list_tasks()) == 2


def test_delete_tasks(tmp_path):
    svc = make_service(tmp_path)
    task = svc.create_task("* * * * *", "ping", "sess")
    assert svc.delete_tasks([task.id]) == 1
    assert svc.list_tasks() == []


# --- _on_fire / _is_idle ---------------------------------------------------


def test_on_fire_sends_trigger_turn(tmp_path):
    control = FakeControl()
    svc = make_service(tmp_path, control)
    task = CronTask.new("* * * * *", "do it", _ms(2026, 6, 15), target_session_id="sess")
    svc._on_fire(task)
    assert len(control.sent) == 1
    agent_id, message, mode = control.sent[0]
    assert agent_id == "sess"
    assert message.content == "do it"
    assert mode is DeliveryMode.TRIGGER_TURN


def test_on_fire_missing_target_does_not_raise(tmp_path):
    control = FakeControl(raise_on="gone")
    svc = make_service(tmp_path, control)
    task = CronTask.new("* * * * *", "do it", _ms(2026, 6, 15), target_session_id="gone")
    svc._on_fire(task)  # best-effort: swallows
    assert control.sent == []


def test_on_fire_no_target_noop(tmp_path):
    control = FakeControl()
    svc = make_service(tmp_path, control)
    task = CronTask.new("* * * * *", "do it", _ms(2026, 6, 15))
    svc._on_fire(task)
    assert control.sent == []


def test_is_idle_reflects_active_turn(tmp_path):
    control = FakeControl({"a": FakeRuntime(active_turn=False)})
    svc = make_service(tmp_path, control)
    assert svc._is_idle() is True
    control._rt["a"].active_turn = True
    assert svc._is_idle() is False


# --- integration with a real AgentControl ----------------------------------


class FakeRole:
    def __init__(self, session_id):
        self._session_id = session_id
        self.state = types.SimpleNamespace(msg_buffer=MessageQueue())

    @property
    def session_id(self):
        return self._session_id

    async def run(self, with_message=None):
        return "ok"

    def dump(self):
        return {"session_id": self._session_id}


def test_fire_delivers_to_real_runtime_mailbox(tmp_path):
    control = AgentControl()
    runtime = AgentRuntime(FakeRole("sess"))
    control.add_agent(runtime, metadata=AgentMetadata(agent_path=AgentPath.from_string("/root/sess")))

    svc = make_service(tmp_path, control)
    task = CronTask.new("* * * * *", "ping", _ms(2026, 6, 15), target_session_id="sess")
    svc._on_fire(task)

    assert runtime.mailbox.has_pending()
    assert runtime.mailbox.has_trigger_turn()
    drained = runtime.mailbox.drain_for_turn()
    assert [m.content for m in drained] == ["ping"]
