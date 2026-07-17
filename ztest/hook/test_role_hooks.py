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

from mote.common.hook.types import HookOutcome
from mote.roles import Role
from mote.roles.role_schema import RoleSchema


class _StubLoop:
    """Minimal BaseLoop stand-in: returns None, exposes latest_observed_msg."""

    latest_observed_msg = None

    async def run(self):
        return None


@pytest.fixture
def role_in_tmp(tmp_path, monkeypatch):
    from mote.router.llm.context import Context

    monkeypatch.setattr("mote.session.log._default_base_dir", lambda: tmp_path)
    role = Role(name="Hooked", context=Context())
    # Replace the loop with a no-op stub so run() exercises only the hook seams.
    # run() builds its loop via the graph's ``loop_factory``; seed that slot with
    # a factory yielding the stub (the DI seam), so make_loop() returns it.
    role._components._graph.seed("loop_factory", lambda: _StubLoop())
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

    role_in_tmp.register_hook("UserPromptSubmit", lambda hi: {"additionalContext": "PROJECT RULES"})

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
    from mote.common.schema import HookConfig
    from mote.router.llm.context import Context

    monkeypatch.setattr("mote.session.log._default_base_dir", lambda: tmp_path)
    role = Role(role_schema=RoleSchema(name="Cfg", hooks=HookConfig()), context=Context())
    # A declared HookConfig (even empty events) engages the manager.
    assert role.hook_manager is not None


@pytest.mark.asyncio
async def test_global_hooks_json_engages_manager(tmp_path, monkeypatch):
    """A global ``~/.mote/hooks.json`` engages the hook layer for a Role that
    declares no per-Role ``HookConfig`` and registers no callbacks."""
    import json

    import mote.common.const.paths as paths
    from mote.router.llm.context import Context

    monkeypatch.setattr("mote.session.log._default_base_dir", lambda: tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    (home / "hooks.json").write_text(
        json.dumps(
            {"hooks": {"PreToolUse": [{"matcher": "Bash", "handlers": [{"type": "command", "command": "exit 2"}]}]}}
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(paths, "CONFIG_ROOT", home)

    role = Role(role_schema=RoleSchema(name="Global"), context=Context())
    # hooks=None + no callbacks, yet the global file engages the layer.
    assert role.role_schema.hooks is None
    assert role.hook_manager is not None

    # And the loaded command hook actually fires: a matched Bash tool blocks
    # (the `exit 2` handler signals deny).
    outcome = await role.hook_manager.fire("PreToolUse", {"tool_name": "Bash"})
    assert outcome.behavior == "deny"

    # An unmatched tool selects no handler → EMPTY (no block).
    outcome_read = await role.hook_manager.fire("PreToolUse", {"tool_name": "Read"})
    assert outcome_read.behavior != "deny"
