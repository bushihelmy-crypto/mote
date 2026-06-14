#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for MGXEnv — the human-channel face of the control plane."""

import types

import pytest

from metagpt.common.schema.messages import UserMessage
from metagpt.common.schema.queue import MessageQueue
from metagpt.environment.base_env import AgentEnvironment
from metagpt.environment.mgx.mgx_env import MGXEnv


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


def test_mgx_env_is_agent_environment():
    env = MGXEnv()
    assert isinstance(env, MGXEnv)
    assert isinstance(env, AgentEnvironment)


def test_repr():
    assert repr(MGXEnv()) == "MGXEnv()"


@pytest.mark.asyncio
async def test_ask_human_uses_input_hook(monkeypatch):
    import metagpt.environment.mgx.mgx_env as mod

    async def fake_input(prompt):
        return f"answer to: {prompt}"

    monkeypatch.setattr(mod, "get_human_input", fake_input)
    env = MGXEnv()
    rsp = await env.ask_human("what now?")
    assert rsp == "Human response: answer to: what now?"


@pytest.mark.asyncio
async def test_reply_to_human_acknowledges():
    env = MGXEnv()
    rsp = await env.reply_to_human("done!")
    assert "SUCCESS" in rsp


@pytest.mark.asyncio
async def test_mgx_env_routes_like_base():
    env = MGXEnv()
    role = FakeRole("zoe")
    env.add_role(role)
    env.publish_message(UserMessage(content="hi", send_to={"zoe"}))
    await env.run(1)
    assert role.observed_turns == [["hi"]]
