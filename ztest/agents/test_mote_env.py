#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for MoteEnv — the human-channel face of the control plane."""

import types

import pytest

from mote.contracts.conversation import MessageQueue, UserMessage
from mote.orchestration.agents.environment_facade import AgentEnvironment
from mote.product.interaction.mote_env import MoteEnv


def make_env(tmp_path, **kwargs):
    return MoteEnv(
        residency_dir=tmp_path / "residency",
        sessions_dir=tmp_path / "sessions",
        **kwargs,
    )


class FakeRole:
    def __init__(self, name):
        self._session_id = f"sess-{name}"
        self.name = name
        self.role_schema = types.SimpleNamespace(name=name)
        self.state = types.SimpleNamespace(msg_buffer=MessageQueue(), addresses={name})
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


def test_mote_env_is_agent_environment(tmp_path):
    env = make_env(tmp_path)
    assert isinstance(env, MoteEnv)
    assert isinstance(env, AgentEnvironment)


def test_repr(tmp_path):
    assert repr(make_env(tmp_path)) == "MoteEnv()"


@pytest.mark.asyncio
async def test_ask_user_uses_input_hook(tmp_path):
    async def fake_input(prompt):
        return f"answer to: {prompt}"

    env = make_env(tmp_path, human_input=fake_input)
    rsp = await env.ask_user("what now?")
    assert rsp == "Human response: answer to: what now?"


@pytest.mark.asyncio
async def test_reply_to_user_acknowledges(tmp_path):
    env = make_env(tmp_path)
    rsp = await env.reply_to_user("done!")
    assert "SUCCESS" in rsp


@pytest.mark.asyncio
async def test_mote_env_routes_like_base(tmp_path):
    env = make_env(tmp_path)
    role = FakeRole("zoe")
    env.add_role(role)
    env.publish_message(UserMessage(content="hi", send_to={"zoe"}))
    await env.run_ready_turns(1)
    assert role.observed_turns == [["hi"]]
