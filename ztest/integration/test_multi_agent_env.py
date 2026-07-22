#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""End-to-end multi-agent: real Roles driven inside ``MoteEnv``.

Where ``ztest/environment/test_integration.py`` proves the control-plane
plumbing with *fake* roles, these tests drop a *real* ``Role`` (scripted LLM +
real tools + real session log) onto the control plane and verify the full
delivery → observe → think → act pipeline runs through the environment, and
that several roles are scheduled independently.
"""
from __future__ import annotations

import os

import pytest

from mote.common.schema.messages import UserMessage
from mote.environment.mote.mote_env import MoteEnv

pytestmark = pytest.mark.asyncio


async def test_real_role_processes_routed_message(make_role, tmp_path):
    """A message routed through MoteEnv reaches a real Role and runs its tools."""
    target = os.path.join(str(tmp_path), "env.txt")
    role = make_role(
        name="worker",
        working_dir=str(tmp_path),
        tools=["Edit"],
        turns=[[("Edit", {"file_path": target, "old_string": "", "new_string": "from-env"})], "done"],
    )

    env = MoteEnv()
    env.add_role(role)

    env.publish_message(UserMessage(content="please write env.txt", send_to={"worker"}))
    await env.run(5)

    # The real Role's Write tool executed against the tmp workspace.
    assert os.path.exists(target)
    with open(target, encoding="utf-8") as f:
        assert f.read() == "from-env"
    # It thought at least once (the message was observed and acted on).
    assert role.scripted_llm.tool_calls_seen


async def test_unaddressed_role_stays_idle(make_role, tmp_path):
    """A message addressed elsewhere never wakes an unrelated role."""
    target = os.path.join(str(tmp_path), "idle.txt")
    role = make_role(
        name="idle_worker",
        working_dir=str(tmp_path),
        tools=["Edit"],
        turns=[[("Edit", {"file_path": target, "old_string": "", "new_string": "nope"})], "done"],
    )

    env = MoteEnv()
    env.add_role(role)

    # Addressed to a non-existent recipient -> not delivered here.
    env.publish_message(UserMessage(content="hi", send_to={"someone_else"}))
    await env.run(3)

    assert not os.path.exists(target)
    assert role.scripted_llm.tool_calls_seen == []


async def test_two_real_roles_scheduled_independently(make_role, tmp_path):
    """Two real roles each handle their own routed message."""
    a_file = os.path.join(str(tmp_path), "a_out.txt")
    b_file = os.path.join(str(tmp_path), "b_out.txt")
    role_a = make_role(
        name="alpha",
        working_dir=str(tmp_path),
        tools=["Edit"],
        turns=[[("Edit", {"file_path": a_file, "old_string": "", "new_string": "A"})], "done"],
    )
    role_b = make_role(
        name="beta",
        working_dir=str(tmp_path),
        tools=["Edit"],
        turns=[[("Edit", {"file_path": b_file, "old_string": "", "new_string": "B"})], "done"],
    )

    env = MoteEnv()
    env.add_role(role_a)
    env.add_role(role_b)

    env.publish_message(UserMessage(content="work A", send_to={"alpha"}))
    env.publish_message(UserMessage(content="work B", send_to={"beta"}))
    await env.run(6)

    assert os.path.exists(a_file)
    assert os.path.exists(b_file)
    with open(a_file, encoding="utf-8") as f:
        assert f.read() == "A"
    with open(b_file, encoding="utf-8") as f:
        assert f.read() == "B"
