#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the persistent PTY-backed ``terminal`` tool.

Drives the real shell through the tool's ``call``, using the shared
``CapRole``/``bind``/``run``/``workspace`` harness. Everything is local and
offline.

A live terminal keeps a reader task on the event loop it was started on. In
production a Role runs in one persistent loop; here we must drive a whole
multi-call scenario inside ONE ``asyncio.run`` (the conftest ``run`` opens a new
loop per call, which would orphan the reader). Each test uses a unique
``session_id`` and tears the terminal down so the module-level ``TERMINAL``
singleton does not leak across tests.
"""
from __future__ import annotations

import pytest

from metagpt.executor.dependency._terminal import TERMINAL, HeadTailBuffer
from metagpt.executor.tools.terminal import Terminal
from metagpt.executor.tool_result import ToolError

from .conftest import CapRole, bind, run


@pytest.fixture
def caprole(workspace):
    """A role whose cwd starts at the per-test workspace."""
    return CapRole(cwd=str(workspace))


# ---------------------------------------------------------------------------
# short / finishing commands (back at a prompt)
# ---------------------------------------------------------------------------


class TestShort:
    def test_short_command_output_at_prompt(self, caprole, workspace):
        tool = bind(Terminal(), caprole, session_id="t_short1")
        try:
            out = run(tool.call(input="echo hello", yield_time_ms=2000))
            assert "hello" in out
            # Finished -> back at prompt, no "still running" footer.
            assert "still running" not in out
        finally:
            TERMINAL.cleanup_session("t_short1")

    def test_exit_closes_terminal(self, caprole, workspace):
        tool = bind(Terminal(), caprole, session_id="t_short2")
        try:
            out = run(tool.call(input="exit", yield_time_ms=2000))
            # `exit` ends the shell -> the terminal is torn down. (The shell's own
            # exit code isn't captured: the prompt marker never fires after exit.)
            assert "terminal exited" in out
            assert not TERMINAL.has("t_short2")
        finally:
            TERMINAL.cleanup_session("t_short2")

    def test_false_returns_exit_code(self, caprole, workspace):
        tool = bind(Terminal(), caprole, session_id="t_short3")
        try:
            out = run(tool.call(input="false", yield_time_ms=2000))
            assert "[exit code: 1]" in out
        finally:
            TERMINAL.cleanup_session("t_short3")


# ---------------------------------------------------------------------------
# persistent state (cwd / env) across calls — same session, one loop
# ---------------------------------------------------------------------------


class TestPersistence:
    def test_cwd_persists_across_calls(self, caprole, workspace):
        sub = workspace / "sub"
        sub.mkdir()
        tool = bind(Terminal(), caprole, session_id="t_cwd")

        async def scenario():
            await tool.call(input=f"cd {sub}", yield_time_ms=2000)
            out = await tool.call(input="pwd", yield_time_ms=2000)
            assert str(sub) in out
            TERMINAL.cleanup_session("t_cwd")

        run(scenario())

    def test_env_persists_across_calls(self, caprole, workspace):
        tool = bind(Terminal(), caprole, session_id="t_env")

        async def scenario():
            await tool.call(input="export FOO=bar123", yield_time_ms=2000)
            out = await tool.call(input="echo $FOO", yield_time_ms=2000)
            assert "bar123" in out
            TERMINAL.cleanup_session("t_env")

        run(scenario())


# ---------------------------------------------------------------------------
# interactive — a foreground program holds the terminal
# ---------------------------------------------------------------------------


class TestInteractive:
    def test_python_repl_foreground_then_exit(self, caprole, workspace):
        tool = bind(Terminal(), caprole, session_id="t_py")

        async def scenario():
            started = await tool.call(input="python3 -i -q", yield_time_ms=800)
            # Python holds the terminal: no prompt marker within the window.
            assert "still running" in started

            echoed = await tool.call(input="print(40 + 2)", yield_time_ms=1000)
            assert "42" in echoed
            assert "still running" in echoed  # python still in foreground

            done = await tool.call(input="exit()", yield_time_ms=1500)
            # Back at the shell prompt (or shell exited) — no longer "running".
            assert "still running" not in done
            TERMINAL.cleanup_session("t_py")

        run(scenario())

    def test_interrupt_reclaims_hung_command(self, caprole, workspace):
        tool = bind(Terminal(), caprole, session_id="t_int")

        async def scenario():
            started = await tool.call(input="sleep 30", yield_time_ms=600)
            assert "still running" in started

            done = await tool.call(interrupt=True, yield_time_ms=1500)
            # Ctrl-C returns control to the shell prompt.
            assert "still running" not in done
            TERMINAL.cleanup_session("t_int")

        run(scenario())

    def test_interrupt_without_terminal_raises(self, caprole, workspace):
        tool = bind(Terminal(), caprole, session_id="t_noint")
        with pytest.raises(ToolError):
            run(tool.call(interrupt=True, yield_time_ms=500))


# ---------------------------------------------------------------------------
# close / cleanup
# ---------------------------------------------------------------------------


class TestCloseCleanup:
    def test_close_no_terminal(self, caprole, workspace):
        tool = bind(Terminal(), caprole, session_id="t_close0")
        out = run(tool.call(close=True))
        assert "no terminal to close" in out

    def test_close_after_use(self, caprole, workspace):
        tool = bind(Terminal(), caprole, session_id="t_close1")

        async def scenario():
            await tool.call(input="echo hi", yield_time_ms=2000)
            out = await tool.call(close=True)
            assert "terminal closed" in out
            assert not TERMINAL.has("t_close1")

        run(scenario())

    def test_cleanup_terminates_live_terminal(self, caprole, workspace):
        tool = bind(Terminal(), caprole, session_id="t_clean")

        async def scenario():
            await tool.call(input="sleep 30", yield_time_ms=500)
            assert TERMINAL.has("t_clean")
            tool.cleanup_session("t_clean")
            assert not TERMINAL.has("t_clean")
            tool.cleanup_session("t_clean")  # idempotent — must not raise

        run(scenario())


# ---------------------------------------------------------------------------
# HeadTailBuffer cap (engine unit) + large live output
# ---------------------------------------------------------------------------


class TestHeadTailBuffer:
    def test_small_output_no_marker(self):
        b = HeadTailBuffer(max_bytes=100)
        b.append(b"abc")
        assert b.render() == b"abc"
        assert b.omitted == 0

    def test_cap_keeps_head_and_tail_with_marker(self):
        b = HeadTailBuffer(max_bytes=100)
        b.append(b"H" * 60)
        b.append(b"T" * 200)
        rendered = b.render()
        assert b"omitted" in rendered
        assert rendered.startswith(b"H")
        assert rendered.rstrip(b"\n").endswith(b"T")
        assert b.omitted > 0

    def test_large_output_shows_marker(self, caprole, workspace):
        tool = bind(Terminal(), caprole, session_id="t_big")
        try:
            out = run(
                tool.call(
                    input="head -c 3000000 /dev/zero | tr '\\0' 'a'",
                    yield_time_ms=4000,
                )
            )
            assert "omitted" in out
        finally:
            TERMINAL.cleanup_session("t_big")
