#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Offline tests for the slash-command dispatcher.

``SlashCommands`` is thin: it parses + formats and delegates to high-level
``Repl`` methods. Here we drive it against a ``FakeRepl`` recording surface, so
the tests assert dispatch + output without any control plane / role machinery.
"""

from __future__ import annotations

import asyncio
import types

import pytest

from metagpt.cli.commands import SlashCommands


class FakeRepl:
    """Records the high-level calls the dispatcher makes."""

    def __init__(self):
        self.notices = []
        self.exited = False
        self.current_agent_id = "aaaaaaaa-1111"
        self._agents = [("aaaaaaaa-1111", "Assistant", "idle")]
        self._sessions = []
        self.switch_result = ("bbbbbbbb-2222", "Worker")
        self.new_result = "cccccccc-3333"
        self.fork_result = "dddddddd-4444"
        self.resume_result = (True, "resumed session eeeeeeee")
        self.calls = []

    def _notice(self, text):
        self.notices.append(text)

    def request_exit(self):
        self.exited = True

    def active_agents(self):
        return self._agents

    def switch_agent(self, ref):
        self.calls.append(("switch", ref))
        return self.switch_result

    def new_agent(self, name):
        self.calls.append(("new", name))
        return self.new_result

    def fork_current(self):
        self.calls.append(("fork",))
        return self.fork_result

    def list_resumable_sessions(self):
        return self._sessions

    def resume_session_ref(self, ref):
        self.calls.append(("resume", ref))
        return self.resume_result


def _fake_session(session_id, title="", modified="2026-06-15T10:00:00"):
    return types.SimpleNamespace(
        session_id=session_id, title=title, last_prompt="", preview="", modified=modified
    )


def _run(repl, line):
    cmds = SlashCommands(repl)
    asyncio.run(cmds.handle(line))
    return "".join(repl.notices)


def text_of(repl):
    return "".join(repl.notices)


# ---------------------------------------------------------------------------
# is_command
# ---------------------------------------------------------------------------
def test_is_command():
    assert SlashCommands.is_command("/exit")
    assert SlashCommands.is_command("   /help")
    assert not SlashCommands.is_command("hello")
    assert not SlashCommands.is_command("")


# ---------------------------------------------------------------------------
# basic dispatch
# ---------------------------------------------------------------------------
def test_help_lists_commands():
    repl = FakeRepl()
    out = _run(repl, "/help")
    assert "Commands:" in out
    assert "/exit" in out
    assert "/resume" in out


def test_bare_slash_shows_help():
    repl = FakeRepl()
    out = _run(repl, "/")
    assert "Commands:" in out


def test_unknown_command():
    repl = FakeRepl()
    out = _run(repl, "/nope")
    assert "Unknown command" in out


def test_exit_requests_exit():
    repl = FakeRepl()
    _run(repl, "/exit")
    assert repl.exited is True


def test_quit_alias_exits():
    repl = FakeRepl()
    _run(repl, "/quit")
    assert repl.exited is True


# ---------------------------------------------------------------------------
# agents / switching
# ---------------------------------------------------------------------------
def test_agents_lists_with_active_marker():
    repl = FakeRepl()
    repl._agents = [
        ("aaaaaaaa-1111", "Assistant", "idle"),
        ("bbbbbbbb-2222", "Worker", "running"),
    ]
    out = _run(repl, "/agents")
    assert "Assistant" in out
    assert "Worker" in out
    assert "*" in out  # active marker on the current agent
    assert "[0]" in out and "[1]" in out


def test_agents_empty():
    repl = FakeRepl()
    repl._agents = []
    out = _run(repl, "/agents")
    assert "no agents" in out


def test_switch_success():
    repl = FakeRepl()
    out = _run(repl, "/switch 1")
    assert ("switch", "1") in repl.calls
    assert "switched to Worker" in out


def test_agent_alias_and_failure():
    repl = FakeRepl()
    repl.switch_result = None
    out = _run(repl, "/agent zzz")
    assert ("switch", "zzz") in repl.calls
    assert "no agent matching" in out


def test_agent_without_arg_shows_usage():
    repl = FakeRepl()
    out = _run(repl, "/agent")
    assert "usage:" in out


# ---------------------------------------------------------------------------
# new / fork
# ---------------------------------------------------------------------------
def test_new_agent():
    repl = FakeRepl()
    out = _run(repl, "/new Researcher")
    assert ("new", "Researcher") in repl.calls
    assert "created agent" in out


def test_new_agent_default_name():
    repl = FakeRepl()
    _run(repl, "/new")
    assert ("new", "Assistant") in repl.calls


def test_new_agent_unavailable():
    repl = FakeRepl()
    repl.new_result = None
    out = _run(repl, "/new")
    assert "cannot create" in out


def test_fork():
    repl = FakeRepl()
    out = _run(repl, "/fork")
    assert ("fork",) in repl.calls
    assert "forked into agent" in out


def test_fork_unavailable():
    repl = FakeRepl()
    repl.fork_result = None
    out = _run(repl, "/fork")
    assert "cannot fork" in out


# ---------------------------------------------------------------------------
# sessions / resume
# ---------------------------------------------------------------------------
def test_sessions_empty():
    repl = FakeRepl()
    out = _run(repl, "/sessions")
    assert "no resumable sessions" in out


def test_sessions_lists_indexed():
    repl = FakeRepl()
    repl._sessions = [
        _fake_session("11111111-aaaa", title="fix the bug"),
        _fake_session("22222222-bbbb", title="add feature"),
    ]
    out = _run(repl, "/list")  # alias
    assert "[0]" in out and "[1]" in out
    assert "fix the bug" in out
    assert "11111111" in out


def test_resume_delegates():
    repl = FakeRepl()
    out = _run(repl, "/resume 0")
    assert ("resume", "0") in repl.calls
    assert "resumed session" in out


def test_resume_without_arg():
    repl = FakeRepl()
    out = _run(repl, "/resume")
    assert "usage:" in out


def test_resume_failure_message():
    repl = FakeRepl()
    repl.resume_result = (False, "no rollout for 12345678")
    out = _run(repl, "/resume 12345678")
    assert "no rollout" in out
