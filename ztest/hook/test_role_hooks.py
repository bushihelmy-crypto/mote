#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Role-level hook wiring: SessionStart once, UserPromptSubmit context, Stop.

Drives ``Role.run()`` with a stubbed loop so the test stays offline: the loop is
replaced with one that records nothing and returns None, while the hook seams in
run() still fire. ``hooks=None`` + no callbacks => zero hook calls (backward
compat).
"""
from __future__ import annotations

import pytest

from metagpt.common.hook.types import HookOutcome
from metagpt.roles import Role
from metagpt.roles.role_schema import RoleSchema


class _StubLoop:
    """Minimal BaseLoop stand-in: returns None, exposes latest_observed_msg."""

    latest_observed_msg = None

    async def run(self):
        return None


@pytest.fixture
def role_in_tmp(tmp_path, monkeypatch):
    from metagpt.router.llm.context import Context

    monkeypatch.setattr("metagpt.roles.session.log._default_base_dir", lambda: tmp_path)
    role = Role(name="Hooked", context=Context())
    # Replace the loop with a no-op stub so run() exercises only the hook seams.
    monkeypatch.setattr(role, "_make_loop", lambda: _StubLoop())
    return role


@pytest.mark.asyncio
async def test_no_hooks_is_zero_overhead(role_in_tmp):
    # No HookConfig and no registered callbacks => hook_manager stays None.
    assert role_in_tmp.hook_manager is None
    await role_in_tmp.run(with_message="hello")
    assert role_in_tmp.hook_manager is None


@pytest.mark.asyncio
async def test_session_start_fired_once(role_in_tmp):
    events: list[str] = []
    role_in_tmp.register_hook("SessionStart", lambda hi: events.append(hi.payload.get("source")))
    await role_in_tmp.run(with_message="one")
    await role_in_tmp.run(with_message="two")
    # SessionStart fires exactly once across multiple run() calls.
    assert events == ["startup"]


@pytest.mark.asyncio
async def test_user_prompt_submit_injects_context(role_in_tmp):
    seen_prompts: list[str] = []

    role_in_tmp.register_hook(
        "UserPromptSubmit", lambda hi: {"additionalContext": "PROJECT RULES"}
    )

    # Capture what actually got pushed into the buffer.
    pushed: list = []
    orig_put = role_in_tmp.put_message
    role_in_tmp.put_message = lambda m: (pushed.append(m), orig_put(m))[1]

    await role_in_tmp.run(with_message="do the thing")
    assert pushed, "a message should have been queued"
    assert pushed[0].content.startswith("PROJECT RULES")
    assert "do the thing" in pushed[0].content


@pytest.mark.asyncio
async def test_stop_fired_in_finally(role_in_tmp):
    fired: list[str] = []
    role_in_tmp.register_hook("Stop", lambda hi: fired.append(hi.hook_event_name))
    await role_in_tmp.run(with_message="x")
    assert fired == ["Stop"]


@pytest.mark.asyncio
async def test_hook_config_engages_manager(tmp_path, monkeypatch):
    from metagpt.router.llm.context import Context
    from metagpt.common.schema import HookConfig

    monkeypatch.setattr("metagpt.roles.session.log._default_base_dir", lambda: tmp_path)
    role = Role(role_schema=RoleSchema(name="Cfg", hooks=HookConfig()), context=Context())
    # A declared HookConfig (even empty events) engages the manager.
    assert role.hook_manager is not None
