#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""End-to-end permission-engine coverage during a real ``Role.run``.

``test_cross_cutting`` proves the explicit allow/deny *rules*; here we exercise
the coarse :class:`PermissionMode` stances and the interactive ``ask`` path:

* ``acceptEdits`` / ``bypass`` auto-allow a mutating tool with no allow rule.
* ``plan`` / ``dontAsk`` block a mutating tool (the latter fails closed).
* an ``ask`` rule routes through the Role's ``request_approval`` capability ->
  the injected human interaction port; a "yes" runs the tool and a
  "no" denies it.

Only the LLM (scripted) and, for the ask tests, the human-input channel are
faked; the permission engine, tools and filesystem are all real.
"""

from __future__ import annotations

import os

import pytest

from mote.product.interaction.approvals import parse_approval_response, render_approval_prompt
from mote.runtime.tools.permission.config import PermissionConfig

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Coarse modes (no interactive channel needed)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["acceptEdits", "bypass"])
async def test_mode_auto_allows_mutating_tool(make_role, tmp_path, mode):
    target = os.path.join(str(tmp_path), "m.txt")
    role = make_role(
        working_dir=str(tmp_path),
        tools=["Edit"],
        permissions=PermissionConfig(mode=mode),
        turns=[[("Edit", {"file_path": target, "old_string": "", "new_string": "ok"})], "done"],
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
        tools=["Edit"],
        permissions=PermissionConfig(mode=mode),
        turns=[[("Edit", {"file_path": target, "old_string": "", "new_string": "blocked"})], "done"],
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
        tools=["Edit"],
        permissions=PermissionConfig(mode="bypass", deny=["Edit"]),
        turns=[[("Edit", {"file_path": target, "old_string": "", "new_string": "x"})], "done"],
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
        tools=["Edit"],
        permissions=PermissionConfig(mode="dontAsk", allow=["Edit"]),
        turns=[[("Edit", {"file_path": target, "old_string": "", "new_string": "ok"})], "done"],
    )

    await role.run(with_message="go")

    assert os.path.exists(target)


async def test_allow_rule_overrides_plan(make_role, tmp_path):
    """An ``allow`` rule lets a mutating tool run even in read-only plan mode."""
    target = os.path.join(str(tmp_path), "p.txt")
    role = make_role(
        working_dir=str(tmp_path),
        tools=["Edit"],
        permissions=PermissionConfig(mode="plan", allow=["Edit"]),
        turns=[[("Edit", {"file_path": target, "old_string": "", "new_string": "ok"})], "done"],
    )

    await role.run(with_message="go")

    assert os.path.exists(target)


# ---------------------------------------------------------------------------
# Interactive ask path (request_approval -> env.ask_user -> human channel)
# ---------------------------------------------------------------------------


def _human_input(reply: str):
    """Make the test interaction port resolve to a fixed human reply."""

    async def _fake(question):  # signature matches get_human_input(question)
        return reply

    return _fake


class _ApprovalHuman:
    def __init__(self, reply, approval_prompt, approval_parser):
        self._reply = reply
        self._approval_prompt = approval_prompt
        self._approval_parser = approval_parser

    async def request_approval(self, request, *, sent_from):
        return self._approval_parser(await self._reply(self._approval_prompt(request)))


async def test_ask_rule_approved_runs_tool(make_role, tmp_path, monkeypatch):
    human_input = _human_input("yes")
    target = os.path.join(str(tmp_path), "ask.txt")
    role = make_role(
        working_dir=str(tmp_path),
        tools=["Edit"],
        permissions=PermissionConfig(mode="default", ask=["Edit"]),
        turns=[[("Edit", {"file_path": target, "old_string": "", "new_string": "approved"})], "done"],
    )
    # request_approval needs an env channel.
    role.bind_human_interaction(_ApprovalHuman(human_input, render_approval_prompt, parse_approval_response))

    await role.run(with_message="write with approval")

    assert os.path.exists(target)
    with open(target, encoding="utf-8") as f:
        assert f.read() == "approved"


async def test_ask_rule_denied_blocks_tool(make_role, tmp_path, monkeypatch):
    human_input = _human_input("no")
    target = os.path.join(str(tmp_path), "ask.txt")
    role = make_role(
        working_dir=str(tmp_path),
        tools=["Edit"],
        permissions=PermissionConfig(mode="default", ask=["Edit"]),
        turns=[[("Edit", {"file_path": target, "old_string": "", "new_string": "rejected"})], "done"],
    )
    role.bind_human_interaction(_ApprovalHuman(human_input, render_approval_prompt, parse_approval_response))

    await role.run(with_message="write but get denied")

    assert not os.path.exists(target)
    contents = [m.content for m in role.context_manager.get()]
    assert any("PERMISSION DENIED" in c or "denied" in c.lower() for c in contents)


async def test_ask_without_env_fails_closed(make_role, tmp_path):
    """No env => no approval channel => the ask resolves to deny (fail-closed)."""
    target = os.path.join(str(tmp_path), "ask.txt")
    role = make_role(
        working_dir=str(tmp_path),
        tools=["Edit"],
        permissions=PermissionConfig(mode="default", ask=["Edit"]),
        turns=[[("Edit", {"file_path": target, "old_string": "", "new_string": "no-channel"})], "done"],
    )
    # Deliberately no human interaction capability is bound.

    await role.run(with_message="write with no approval channel")

    assert not os.path.exists(target)
