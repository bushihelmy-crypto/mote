#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for AgentEnvironment — the BaseEnvironment face over the control plane."""

import types

import pytest

from mote.contracts.conversation import MessageQueue, UserMessage
from mote.contracts.conversation.fields import MESSAGE_ROUTE_TO_ALL
from mote.orchestration.agents.environment_facade import AgentEnvironment


def make_env(tmp_path, **kwargs):
    return AgentEnvironment(
        residency_dir=tmp_path / "residency",
        sessions_dir=tmp_path / "sessions",
        **kwargs,
    )


class FakeRole:
    """Duck-typed Role for env membership/routing tests."""

    def __init__(self, name, *, addresses=None):
        self._session_id = f"sess-{name}"
        self.name = name
        self.role_schema = types.SimpleNamespace(name=name)
        self.state = types.SimpleNamespace(
            msg_buffer=MessageQueue(),
            addresses=set(addresses or {name}),
        )
        self.env = None
        self.agent_control = None
        self.observed_turns = []

    @property
    def session_id(self):
        return self._session_id

    def set_env(self, env):
        self.env = env
        env.set_addresses(self, self.state.addresses)

    def bind_agent_control(self, control):
        self.agent_control = control

    async def run(self, with_message=None):
        drained = self.state.msg_buffer.pop_all()
        self.observed_turns.append([m.content for m in drained])
        return "ok"

    def dump(self):
        return {"session_id": self._session_id}


def test_add_role_registers_and_wires_env(tmp_path):
    env = make_env(tmp_path)
    role = FakeRole("alice")
    returned = env.add_role(role)
    assert returned is role
    assert role.env is env
    assert "alice" in env.roles
    assert env.get_role("alice") is role
    assert env.role_names() == ["alice"]


def test_add_roles_bulk(tmp_path):
    env = make_env(tmp_path)
    a, b = FakeRole("a"), FakeRole("b")
    env.add_roles([a, b])
    assert set(env.role_names()) == {"a", "b"}


def test_desc_field(tmp_path):
    env = make_env(tmp_path, desc="my world")
    assert env.desc == "my world"


def test_publish_message_routes_to_mailbox(tmp_path):
    env = make_env(tmp_path)
    role = FakeRole("bob")
    env.add_role(role)
    msg = UserMessage(content="hello bob", send_to={"bob"})
    assert env.publish_message(msg) is True
    # delivered turn-atomically into bob's mailbox (not yet drained)
    runtime = env.control.get_runtime(role.session_id)
    assert not runtime.mailbox.empty()


@pytest.mark.asyncio
async def test_publish_then_run_delivers_turn_atomically(tmp_path):
    env = make_env(tmp_path)
    role = FakeRole("carol")
    env.add_role(role)
    env.publish_message(UserMessage(content="task", send_to={"carol"}))
    turns = await env.run_ready_turns(1)
    assert turns == 1
    assert role.observed_turns == [["task"]]


@pytest.mark.asyncio
async def test_publish_to_all_broadcasts(tmp_path):
    env = make_env(tmp_path)
    a, b = FakeRole("a"), FakeRole("b")
    env.add_role(a)
    env.add_role(b)
    env.publish_message(UserMessage(content="hey", send_to={MESSAGE_ROUTE_TO_ALL}))
    await env.run_ready_turns(1)
    assert a.observed_turns == [["hey"]]
    assert b.observed_turns == [["hey"]]


def test_publish_empty_message_returns_false(tmp_path):
    env = make_env(tmp_path)
    assert env.publish_message(None) is False


def test_publish_to_unknown_recipient_is_noop(tmp_path):
    env = make_env(tmp_path)
    env.add_role(FakeRole("a"))
    # nobody is addressed "ghost" -> no recipients, still returns True
    assert env.publish_message(UserMessage(content="x", send_to={"ghost"})) is True


def test_set_addresses_updates_routing(tmp_path):
    env = make_env(tmp_path)
    role = FakeRole("dave", addresses={"dave"})
    env.add_role(role)
    env.set_addresses(role, {"dave", "captain"})
    env.publish_message(UserMessage(content="aye", send_to={"captain"}))
    runtime = env.control.get_runtime(role.session_id)
    assert not runtime.mailbox.empty()


@pytest.mark.asyncio
async def test_quiescent_after_drain(tmp_path):
    env = make_env(tmp_path)
    role = FakeRole("e")
    env.add_role(role)
    env.publish_message(UserMessage(content="one", send_to={"e"}))
    assert not env.quiescent()
    await env.run_ready_turns(10)
    assert env.quiescent()
