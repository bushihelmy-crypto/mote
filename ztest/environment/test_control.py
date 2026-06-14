#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for AgentControl — the multi-agent control plane."""

import types

import pytest

from metagpt.common.schema.messages import UserMessage
from metagpt.common.schema.queue import MessageQueue
from metagpt.environment.agent_path import AgentPath
from metagpt.environment.control import AgentControl, format_completion_notification
from metagpt.environment.exceptions import AgentLimitReached, AgentNotFound, AgentNotKnown
from metagpt.environment.mailbox import DeliveryMode, InterAgentCommunication
from metagpt.environment.registry import AgentMetadata
from metagpt.environment.runtime import AgentRuntime, AgentStatus
from metagpt.environment.store import ResidencyStore


class FakeRole:
    def __init__(self, session_id):
        self._session_id = session_id
        self.state = types.SimpleNamespace(msg_buffer=MessageQueue())
        self.observed_turns = []

    @property
    def session_id(self):
        return self._session_id

    async def run(self, with_message=None):
        drained = self.state.msg_buffer.pop_all()
        self.observed_turns.append([m.content for m in drained])
        return "ok"

    def dump(self):
        return {"session_id": self._session_id}


def fake_role_loader(role_dump):
    return FakeRole(role_dump.get("session_id", "?"))


def make_runtime(session_id, *, status=AgentStatus.IDLE):
    rt = AgentRuntime(FakeRole(session_id))
    rt.status = status
    return rt


def make_control(tmp_path, **kwargs):
    return AgentControl(
        store=ResidencyStore(base_dir=str(tmp_path)),
        role_loader=fake_role_loader,
        **kwargs,
    )


@pytest.fixture
def control(tmp_path):
    return make_control(tmp_path)


def test_add_agent_registers_in_map_and_scheduler(control):
    rt = make_runtime("a")
    control.add_agent(rt, metadata=AgentMetadata(agent_path=AgentPath.from_string("/root/a")))
    assert control.get_runtime("a") is rt
    assert control.scheduler.get_runtime("a") is rt
    assert control.registry.agent_id_for_path(AgentPath.from_string("/root/a")) == "a"


def test_register_session_root(control):
    control.register_session_root("root-1", None)
    assert control.registry.agent_id_for_path(AgentPath.root()) == "root-1"


def test_get_status_known_and_unknown(control):
    rt = make_runtime("a", status=AgentStatus.RUNNING)
    control.add_agent(rt)
    assert control.get_status("a") == AgentStatus.RUNNING
    assert control.get_status("ghost") == AgentStatus.NOT_FOUND


@pytest.mark.asyncio
async def test_send_input_trigger_runs_turn(control):
    rt = make_runtime("a")
    control.add_agent(rt)
    control.send_input("a", UserMessage("hello"))
    turns = await control.run(1)
    assert turns == 1
    assert rt.role.observed_turns == [["hello"]]


@pytest.mark.asyncio
async def test_send_input_queue_only_defers(control):
    rt = make_runtime("a")
    control.add_agent(rt)
    control.send_input("a", UserMessage("later"), mode=DeliveryMode.QUEUE_ONLY)
    turns = await control.run(1)
    assert turns == 0
    assert rt.mailbox.has_pending()


def test_send_input_unknown_agent_raises(control):
    with pytest.raises(AgentNotFound):
        control.send_input("ghost", UserMessage("x"))


def test_send_input_respects_execution_limit(tmp_path):
    control = make_control(tmp_path, max_threads=1)
    rt = make_runtime("a")
    control.add_agent(rt)
    # occupy the single execution slot
    guard = control.limiter.guard()
    with pytest.raises(AgentLimitReached):
        control.send_input("a", UserMessage("x"))
    guard.release()
    control.send_input("a", UserMessage("x"))  # capacity freed


def test_send_communication_records_last_task_message(control):
    rt = make_runtime("a")
    control.add_agent(rt, metadata=AgentMetadata(agent_path=AgentPath.from_string("/root/a")))
    comm = InterAgentCommunication.new(
        author=AgentPath.root(),
        recipient=AgentPath.from_string("/root/a"),
        content="do the thing",
        trigger_turn=True,
    )
    control.send_inter_agent_communication("a", comm)
    meta = control.registry.agent_metadata_for_thread("a")
    assert meta.last_task_message == "do the thing"
    assert rt.mailbox.has_trigger_turn()


def test_resolve_reference_by_session_id(control):
    rt = make_runtime("sess-xyz")
    control.add_agent(rt)
    assert control.resolve_agent_reference("sess-xyz") == "sess-xyz"


def test_resolve_reference_by_path(control):
    rt = make_runtime("a")
    control.add_agent(rt, metadata=AgentMetadata(agent_path=AgentPath.from_string("/root/researcher")))
    assert control.resolve_agent_reference("/root/researcher") == "a"
    # relative to root
    assert control.resolve_agent_reference("researcher") == "a"


def test_resolve_reference_by_nickname(control):
    rt = make_runtime("a")
    control.add_agent(
        rt,
        metadata=AgentMetadata(agent_path=AgentPath.from_string("/root/a"), agent_nickname="Plato"),
    )
    assert control.resolve_agent_reference("Plato") == "a"


def test_resolve_reference_unknown_raises(control):
    with pytest.raises(AgentNotKnown):
        control.resolve_agent_reference("nobody")


@pytest.mark.asyncio
async def test_rehydrate_on_send_to_evicted_agent(control):
    # Materialize an agent to disk without keeping it live.
    role = FakeRole("evicted")
    role.state.msg_buffer.push(UserMessage("old-buffered"))
    rt = AgentRuntime(role)
    await control.store.materialize(rt)
    assert control.get_runtime("evicted") is None

    # Sending to it rehydrates from disk.
    control.send_input("evicted", UserMessage("wake up"))
    restored = control.get_runtime("evicted")
    assert restored is not None
    assert not control.store.has("evicted")  # forgotten after load
    turns = await control.run(1)
    assert turns == 1
    # both the restored buffer and the new message were delivered
    assert restored.role.observed_turns == [["old-buffered", "wake up"]]


@pytest.mark.asyncio
async def test_completion_watcher_notifies_parent(control):
    parent = make_runtime("parent")
    child = make_runtime("child", status=AgentStatus.RUNNING)
    control.add_agent(parent, metadata=AgentMetadata(agent_path=AgentPath.root()))
    control.add_agent(child, metadata=AgentMetadata(agent_path=AgentPath.from_string("/root/child")))

    task = control.start_completion_watcher(
        "child",
        "parent",
        child_path=AgentPath.from_string("/root/child"),
        parent_path=AgentPath.root(),
    )
    # child finishes
    child.status = AgentStatus.COMPLETED
    await task
    # parent received a queue-only notification (no trigger)
    assert parent.mailbox.has_pending()
    assert not parent.mailbox.has_trigger_turn()
    drained = parent.mailbox.drain_for_turn()
    assert "finished with status" in drained[0].content


@pytest.mark.asyncio
async def test_interrupt_unknown_agent(control):
    assert await control.interrupt("ghost") == AgentStatus.NOT_FOUND


@pytest.mark.asyncio
async def test_interrupt_idle_agent_marks_interrupted(control):
    rt = make_runtime("a", status=AgentStatus.RUNNING)
    control.add_agent(rt)
    status = await control.interrupt("a")
    assert status == AgentStatus.INTERRUPTED


def test_format_completion_notification():
    msg = format_completion_notification("researcher", AgentStatus.COMPLETED)
    assert "researcher" in msg
    assert "completed" in msg
