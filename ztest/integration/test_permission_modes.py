#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""End-to-end permission-engine coverage during a real ``Role.run``.

``test_cross_cutting`` proves the explicit allow/deny *rules*; here we exercise
the coarse :class:`PermissionMode` stances and the interactive ``ask`` path:

* ``acceptEdits`` / ``bypass`` auto-allow a mutating tool with no allow rule.
* ``plan`` / ``dontAsk`` block a mutating tool (the latter fails closed).
* an ``ask`` rule routes through the Role's ``request_approval`` capability ->
  ``MoteEnv.ask_user`` -> the human-input channel; a "yes" runs the tool and a
  "no" denies it.

Only the LLM (scripted) and, for the ask tests, the human-input channel are
faked; the permission engine, tools and filesystem are all real.
"""
from __future__ import annotations

import os

import pytest

from mote.common.schema import PermissionConfig
from mote.environment.mote.mote_env import MoteEnv

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Coarse modes (no interactive channel needed)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["acceptEdits", "bypass"])
async def test_mode_auto_allows_mutating_tool(make_role, tmp_path, mode):
    target = os.path.join(str(tmp_path), "m.txt")
    role = make_role(
        working_dir=str(tmp_path),
        tools=["Write"],
        permissions=PermissionConfig(mode=mode),
        turns=[[("Write", {"file_path": target, "content": "ok"})], "done"],
    )

    await role.run(with_message="write under " + mode)

    assert os.path.exists(target)
    contents = [m.content for m in role.context_manager.get()]
    assert not any('code="TOOL_PERMISSION_DENIED"' in c for c in contents)


@pytest.mark.parametrize("mode", ["plan", "dontAsk"])
async def test_mode_blocks_mutating_tool(make_role, tmp_path, mode):
    target = os.path.join(str(tmp_path), "m.txt")
    role = make_role(
        working_dir=str(tmp_path),
        tools=["Write"],
        permissions=PermissionConfig(mode=mode),
        turns=[[("Write", {"file_path": target, "content": "blocked"})], "done"],
    )

    await role.run(with_message="write under " + mode)

    # plan = read-only preview; dontAsk = fail-closed on the default ask.
    assert not os.path.exists(target)
    contents = [m.content for m in role.context_manager.get()]
    assert any('code="TOOL_PERMISSION_DENIED"' in c for c in contents)


# ---------------------------------------------------------------------------
# Rule precedence vs mode
# ---------------------------------------------------------------------------


async def test_deny_rule_is_bypass_immune(make_role, tmp_path):
    """A ``deny`` rule blocks even under ``bypass`` (deny rules are immune)."""
    target = os.path.join(str(tmp_path), "p.txt")
    role = make_role(
        working_dir=str(tmp_path),
        tools=["Write"],
        permissions=PermissionConfig(mode="bypass", deny=["Write"]),
        turns=[[("Write", {"file_path": target, "content": "x"})], "done"],
    )

    await role.run(with_message="go")

    assert not os.path.exists(target)
    contents = [m.content for m in role.context_manager.get()]
    assert any('code="TOOL_PERMISSION_DENIED"' in c for c in contents)


async def test_allow_rule_overrides_dont_ask(make_role, tmp_path):
    """An explicit ``allow`` rule lets the tool run despite the dontAsk default."""
    target = os.path.join(str(tmp_path), "p.txt")
    role = make_role(
        working_dir=str(tmp_path),
        tools=["Write"],
        permissions=PermissionConfig(mode="dontAsk", allow=["Write"]),
        turns=[[("Write", {"file_path": target, "content": "ok"})], "done"],
    )

    await role.run(with_message="go")

    assert os.path.exists(target)


async def test_allow_rule_overrides_plan(make_role, tmp_path):
    """An ``allow`` rule lets a mutating tool run even in read-only plan mode."""
    target = os.path.join(str(tmp_path), "p.txt")
    role = make_role(
        working_dir=str(tmp_path),
        tools=["Write"],
        permissions=PermissionConfig(mode="plan", allow=["Write"]),
        turns=[[("Write", {"file_path": target, "content": "ok"})], "done"],
    )

    await role.run(with_message="go")

    assert os.path.exists(target)


# ---------------------------------------------------------------------------
# Interactive ask path (request_approval -> env.ask_user -> human channel)
# ---------------------------------------------------------------------------


def _patch_human_input(monkeypatch, reply: str) -> None:
    """Make ``MoteEnv.ask_user`` resolve to a fixed human reply."""
    import mote.environment.mote.mote_env as mote_env

    async def _fake(question):  # signature matches get_human_input(question)
        return reply

    monkeypatch.setattr(mote_env, "get_human_input", _fake)


async def test_ask_rule_approved_runs_tool(make_role, tmp_path, monkeypatch):
    _patch_human_input(monkeypatch, "yes")
    target = os.path.join(str(tmp_path), "ask.txt")
    role = make_role(
        working_dir=str(tmp_path),
        tools=["Write"],
        permissions=PermissionConfig(mode="default", ask=["Write"]),
        turns=[[("Write", {"file_path": target, "content": "approved"})], "done"],
    )
    # request_approval needs an env channel.
    env = MoteEnv()
    env.add_role(role)

    await role.run(with_message="write with approval")

    assert os.path.exists(target)
    with open(target, encoding="utf-8") as f:
        assert f.read() == "approved"


async def test_ask_rule_denied_blocks_tool(make_role, tmp_path, monkeypatch):
    _patch_human_input(monkeypatch, "no")
    target = os.path.join(str(tmp_path), "ask.txt")
    role = make_role(
        working_dir=str(tmp_path),
        tools=["Write"],
        permissions=PermissionConfig(mode="default", ask=["Write"]),
        turns=[[("Write", {"file_path": target, "content": "rejected"})], "done"],
    )
    env = MoteEnv()
    env.add_role(role)

    await role.run(with_message="write but get denied")

    assert not os.path.exists(target)
    contents = [m.content for m in role.context_manager.get()]
    assert any("PERMISSION DENIED" in c or "denied" in c.lower() for c in contents)


async def test_ask_without_env_fails_closed(make_role, tmp_path):
    """No env => no approval channel => the ask resolves to deny (fail-closed)."""
    target = os.path.join(str(tmp_path), "ask.txt")
    role = make_role(
        working_dir=str(tmp_path),
        tools=["Write"],
        permissions=PermissionConfig(mode="default", ask=["Write"]),
        turns=[[("Write", {"file_path": target, "content": "no-channel"})], "done"],
    )
    # Deliberately NOT added to an env: state.env is None.

    await role.run(with_message="write with no approval channel")

    assert not os.path.exists(target)
