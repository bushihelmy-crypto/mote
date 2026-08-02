#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for CronService — glue to AgentControl.send_input(TRIGGER_TURN)."""

import types
from datetime import datetime

import pytest

from mote.contracts.conversation.queue import MessageQueue
from mote.orchestration.agents.control import AgentControl
from mote.orchestration.agents.identity.path import AgentPath
from mote.orchestration.agents.identity.registry import AgentMetadata
from mote.orchestration.agents.lifecycle.runtime import AgentRuntime
from mote.orchestration.agents.messaging.mailbox import DeliveryMode
from mote.orchestration.agents.residency.store import ResidencyStore
from mote.orchestration.automation import TriggerDisposition, TriggerReceipt
from mote.orchestration.automation.cron.service import MAX_CRON_TASKS, CronService, validate_new_task
from mote.orchestration.automation.cron.store import CronOccurrence, CronOccurrenceState, CronTaskStore
from mote.orchestration.automation.cron.task import CronTask
from mote.product.automation import AgentTriggerAdapter, system_timezone_name
from mote.runtime.clock import SystemClock


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

    def dispatch_automation(self, target, content):
        from mote.contracts.conversation import UserMessage

        return self.send_input(target, UserMessage(content=content))


class FakeSink:
    def __init__(self):
        self.triggers = []

    def dispatch(self, trigger):
        self.triggers.append(trigger)
        return TriggerReceipt(TriggerDisposition.ACCEPTED, receipt_id=trigger.trigger_id)


def make_service(tmp_path, sink=None, **kwargs):
    return CronService(
        sink or FakeSink(),
        base_dir=str(tmp_path),
        default_timezone_name=system_timezone_name(),
        clock_source=SystemClock(),
        **kwargs,
    )


def occurrence(task):
    return CronOccurrence(
        occurrence_id=f"cron:{task.id}:{task.revision}:{task.created_at}",
        task_id=str(task.id),
        task_revision=task.revision,
        scheduled_at_ms=task.created_at,
        observed_at_ms=task.created_at,
        state=CronOccurrenceState.DISPATCHING,
        attempt=1,
        receipt_id=None,
        reason=None,
        next_attempt_at_ms=None,
        delete_on_accept=not task.recurring,
    )


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
    svc = make_service(tmp_path)
    for _ in range(MAX_CRON_TASKS):
        svc.create_task("* * * * *", "x", "sess")
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


# --- validate_new_task (shared control-free admission gate) ----------------


def test_validate_accepts_valid_cron():
    validate_new_task("*/5 * * * *", now_ms=0)  # no raise


def test_validate_rejects_invalid_cron():
    with pytest.raises(ValueError):
        validate_new_task("not a cron", now_ms=0)


# --- _on_fire / _is_idle ---------------------------------------------------


def test_on_fire_dispatches_structured_trigger(tmp_path):
    sink = FakeSink()
    svc = make_service(tmp_path, sink)
    task = CronTask.new("* * * * *", "do it", _ms(2026, 6, 15), target_session_id="sess")
    svc._on_fire(task, occurrence(task))
    assert len(sink.triggers) == 1
    assert sink.triggers[0].target == "sess"
    assert sink.triggers[0].content == "do it"


def test_on_fire_missing_target_does_not_raise(tmp_path):
    class RaisingSink:
        def dispatch(self, trigger):
            raise KeyError(trigger.target)

    svc = make_service(tmp_path, RaisingSink())
    task = CronTask.new("* * * * *", "do it", _ms(2026, 6, 15), target_session_id="gone")
    with pytest.raises(KeyError):
        svc._on_fire(task, occurrence(task))


def test_on_fire_no_target_falls_back_to_service_session(tmp_path):
    # A task with no explicit target fires into the session that owns the
    # scheduler (CLI-authored tasks reach the running agent).
    sink = FakeSink()
    svc = make_service(tmp_path, sink, session_id="ctrl")
    task = CronTask.new("* * * * *", "do it", _ms(2026, 6, 15))
    svc._on_fire(task, occurrence(task))
    assert len(sink.triggers) == 1
    assert sink.triggers[0].target == "ctrl"


def test_on_fire_no_target_no_service_session_noop(tmp_path):
    # No explicit target AND no owning session id -> genuine no-op.
    sink = FakeSink()
    svc = make_service(tmp_path, sink, session_id="")
    svc._session_id = ""  # override the "cron" default to prove the guard
    task = CronTask.new("* * * * *", "do it", _ms(2026, 6, 15))
    receipt = svc._on_fire(task, occurrence(task))
    assert sink.triggers == []
    assert receipt.disposition is TriggerDisposition.REJECTED


def test_agent_adapter_is_idempotent_after_acceptance(tmp_path):
    control = FakeControl({"a": FakeRuntime(active_turn=False)})
    adapter = AgentTriggerAdapter(control)
    svc = make_service(tmp_path, adapter)
    task = CronTask.new("* * * * *", "do it", _ms(2026, 6, 15), target_session_id="a")
    first = svc._on_fire(task, occurrence(task))
    assert len(control.sent) == 1
    control._rt["a"].active_turn = True
    second = svc._on_fire(task, occurrence(task))
    assert len(control.sent) == 1
    assert first == second
    assert first.receipt_id == occurrence(task).occurrence_id


def test_agent_adapter_defers_active_target(tmp_path):
    control = FakeControl({"a": FakeRuntime(active_turn=True)})
    svc = make_service(tmp_path, AgentTriggerAdapter(control))
    task = CronTask.new("* * * * *", "do it", _ms(2026, 6, 15), target_session_id="a")

    receipt = svc._on_fire(task, occurrence(task))

    assert receipt.disposition is TriggerDisposition.DEFERRED
    assert control.sent == []


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
    from mote.runtime.control.leases import InMemoryLeaseCoordinator

    residency_leases = InMemoryLeaseCoordinator()
    control = AgentControl(
        store=ResidencyStore(
            base_dir=str(tmp_path / "residency"),
            sessions_base_dir=str(tmp_path / "sessions"),
            lease_coordinator=residency_leases,
        ),
        residency_lease_coordinator=residency_leases,
        lineage_path=tmp_path / "agent-lineage.json",
    )
    runtime = AgentRuntime(FakeRole("sess"))
    control.add_agent(runtime, metadata=AgentMetadata(agent_path=AgentPath.from_string("/root/sess")))

    svc = make_service(tmp_path, AgentTriggerAdapter(control))
    task = CronTask.new("* * * * *", "ping", _ms(2026, 6, 15), target_session_id="sess")
    svc._on_fire(task, occurrence(task))

    assert not runtime.mailbox.empty()
    assert runtime.mailbox.has_trigger_turn()
    drained = runtime.mailbox.drain_for_turn()
    assert [m.content for m in drained] == ["ping"]
