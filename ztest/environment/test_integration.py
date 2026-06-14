#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Integration tests for the control-plane environment.

Covers the two end-to-end scenarios from the plan's Verification section:
  * a message published into ``MGXEnv`` is delivered turn-atomically and a reply
    is routed back to the sender through the control plane;
  * residency: with capacity 1, the LRU agent is materialized to disk + evicted,
    then rehydrates transparently when a message is routed to it.
"""

import types

import pytest

from metagpt.common.schema.messages import UserMessage
from metagpt.common.schema.queue import MessageQueue
from metagpt.environment.mgx.mgx_env import MGXEnv
from metagpt.environment.runtime import AgentRuntime
from metagpt.environment.store import ResidencyStore


class ReplyingRole:
    """Fake Role that, on each turn, drains its buffer and replies to the sender."""

    def __init__(self, name, *, reply_to=None):
        self._session_id = f"sess-{name}"
        self.name = name
        self.role_schema = types.SimpleNamespace(name=name)
        self.state = types.SimpleNamespace(msg_buffer=MessageQueue(), addresses={name})
        self.env = None
        self.observed_turns = []
        self._reply_to = reply_to  # name to reply to, or None

    @property
    def session_id(self):
        return self._session_id

    def set_env(self, env):
        self.env = env
        env.set_addresses(self, self.state.addresses)

    async def run(self, with_message=None):
        drained = self.state.msg_buffer.pop_all()
        self.observed_turns.append([m.content for m in drained])
        if self._reply_to and drained:
            self.env.publish_message(
                UserMessage(content=f"reply from {self.name}", send_to={self._reply_to})
            )
        return "ok"

    def dump(self):
        return {"session_id": self._session_id}


def _loader_for(roles_by_session):
    def _load(role_dump):
        return roles_by_session[role_dump["session_id"]]

    return _load


@pytest.mark.asyncio
async def test_message_roundtrip_through_control_plane():
    env = MGXEnv()
    worker = ReplyingRole("worker", reply_to="boss")
    boss = ReplyingRole("boss")
    env.add_role(worker)
    env.add_role(boss)

    # boss asks worker to do something
    env.publish_message(UserMessage(content="please work", send_to={"worker"}))

    # pump until quiescent: turn 1 worker runs+replies, turn 2 boss receives reply
    await env.run(10)
    assert env.quiescent()

    assert worker.observed_turns == [["please work"]]
    assert boss.observed_turns == [["reply from worker"]]


@pytest.mark.asyncio
async def test_residency_evicts_lru_then_rehydrates(tmp_path):
    env = MGXEnv()
    # Wire the control plane with a store + loader that returns our roles.
    roles_by_session = {}
    store = ResidencyStore(base_dir=str(tmp_path))
    env.control._store = store
    env.control._residency._store = store
    env.control._role_loader = _loader_for(roles_by_session)

    a = ReplyingRole("a")
    b = ReplyingRole("b")
    roles_by_session[a.session_id] = a
    roles_by_session[b.session_id] = b
    env.add_role(a)
    env.add_role(b)

    # Drive both to a final (COMPLETED) status so they become unloadable. The
    # send order (a then b) makes ``a`` the least-recently-used resident.
    env.publish_message(UserMessage(content="warm a", send_to={"a"}))
    env.publish_message(UserMessage(content="warm b", send_to={"b"}))
    await env.run(5)
    assert env.control.residency.residents() == [a.session_id, b.session_id]

    # Reserve a slot with capacity 2: with 2 residents this must evict exactly
    # the LRU (a) to disk, keeping the most-recently-used (b) resident.
    slot = await env.control.residency.reserve_slot(2)
    slot.rollback()

    assert store.has(a.session_id)  # LRU materialized to disk
    assert env.control.get_runtime(a.session_id) is None  # evicted from live map
    assert "a" not in env.roles  # evicted agents omitted from roles view
    assert env.control.get_runtime(b.session_id) is not None  # b stays resident

    # Routing a message to the evicted agent rehydrates it transparently.
    env.publish_message(UserMessage(content="wake up a", send_to={"a"}))
    restored = env.control.get_runtime(a.session_id)
    assert restored is not None
    assert not store.has(a.session_id)  # forgotten after load

    await env.run(5)
    assert a.observed_turns[-1] == ["wake up a"]


def test_residency_reserve_returns_runtime_type(tmp_path):
    # sanity: AgentRuntime is what add_role wraps roles in
    env = MGXEnv()
    role = ReplyingRole("solo")
    env.add_role(role)
    rt = env.control.get_runtime(role.session_id)
    assert isinstance(rt, AgentRuntime)
