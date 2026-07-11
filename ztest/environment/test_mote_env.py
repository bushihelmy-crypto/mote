#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for MoteEnv — the human-channel face of the control plane."""

import types

import pytest

from mote.common.schema.messages import UserMessage
from mote.common.schema.queue import MessageQueue
from mote.environment.base_env import AgentEnvironment
from mote.environment.mote.mote_env import MoteEnv


class FakeRole:
    def __init__(self, name):
        self._session_id = f"sess-{name}"
        self.name = name
        self.role_schema = types.SimpleNamespace(name=name)
        self.state = types.SimpleNamespace(msg_buffer=MessageQueue(), addresses={name})
        self.env = None
        self.observed_turns = []

    @property
    def session_id(self):
        return self._session_id

    def set_env(self, env):
        self.env = env
        env.set_addresses(self, self.state.addresses)

    async def run(self, with_message=None):
        drained = self.state.msg_buffer.pop_all()
        self.observed_turns.append([m.content for m in drained])
        return "ok"

    def dump(self):
        return {"session_id": self._session_id}


def test_mote_env_is_agent_environment():
    env = MoteEnv()
    assert isinstance(env, MoteEnv)
    assert isinstance(env, AgentEnvironment)


def test_repr():
    assert repr(MoteEnv()) == "MoteEnv()"


@pytest.mark.asyncio
async def test_ask_human_uses_input_hook(monkeypatch):
    import mote.environment.mote.mote_env as mod

    async def fake_input(prompt):
        return f"answer to: {prompt}"

    monkeypatch.setattr(mod, "get_human_input", fake_input)
    env = MoteEnv()
    rsp = await env.ask_human("what now?")
    assert rsp == "Human response: answer to: what now?"


@pytest.mark.asyncio
async def test_reply_to_human_acknowledges():
    env = MoteEnv()
    rsp = await env.reply_to_human("done!")
    assert "SUCCESS" in rsp


@pytest.mark.asyncio
async def test_mote_env_routes_like_base():
    env = MoteEnv()
    role = FakeRole("zoe")
    env.add_role(role)
    env.publish_message(UserMessage(content="hi", send_to={"zoe"}))
    await env.run(1)
    assert role.observed_turns == [["hi"]]
