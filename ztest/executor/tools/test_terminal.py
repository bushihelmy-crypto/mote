#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the persistent PTY-backed ``terminal`` tool.

Drives the real shell through the tool's ``call``, using the shared
``CapRole``/``bind``/``run``/``workspace`` harness. Everything is local and
offline.

A live terminal keeps a reader task on the event loop it was started on. In
production a Role runs in one persistent loop; here we must drive a whole
multi-call scenario inside ONE ``asyncio.run`` (the conftest ``run`` opens a new
loop per call, which would orphan the reader). The live session is owned by the
per-test ``CapRole`` through its ``RuntimeHost``, so there is no process-global
singleton to leak across tests; each test still tears its terminal down to free
the subprocess.
"""
from __future__ import annotations

import pytest

from mote.product.toolsets.builtin.terminal import Terminal
from mote.runtime.interactive.terminal.driver import HeadTailBuffer, TerminalRuntimeDriver
from mote.runtime.tools.tool_result import ToolError

from .conftest import CapRole, bind, run


def _has_terminal(role: CapRole) -> bool:
    """Whether the fake Role owns a live managed terminal runtime."""
    return any(item.ref.readable == "terminal:default" for item in role.runtime_host.list())


@pytest.fixture
def caprole(workspace):
    """A role whose cwd starts at the per-test workspace."""
    return CapRole(cwd=str(workspace))


# ---------------------------------------------------------------------------
# short / finishing commands (back at a prompt)
# ---------------------------------------------------------------------------


class TestShort:
    def test_empty_poll_without_activity_does_not_advance_revision(self, caprole, workspace):
        tool = bind(Terminal(), caprole, session_id="t_poll")

        async def scenario():
            await tool.call(input="true", yield_time_ms=2000)
            before = caprole.runtime_host.descriptor("terminal:default").revision
            await tool.call(input="", yield_time_ms=250)
            after = caprole.runtime_host.descriptor("terminal:default").revision
            assert after == before
            await tool.cleanup_session("t_poll")

        run(scenario())

    def test_short_command_output_at_prompt(self, caprole, workspace):
        tool = bind(Terminal(), caprole, session_id="t_short1")
        try:
            out = run(tool.call(input="echo hello", yield_time_ms=2000))
            assert "hello" in out
            # Finished -> back at prompt, no "still running" footer.
            assert "still running" not in out
        finally:
            run(tool.cleanup_session("t_short1"))

    def test_exit_closes_terminal(self, caprole, workspace):
        tool = bind(Terminal(), caprole, session_id="t_short2")
        try:
            out = run(tool.call(input="exit", yield_time_ms=2000))
            # `exit` ends the shell -> the terminal is torn down. (The shell's own
            # exit code isn't captured: the prompt marker never fires after exit.)
            assert "terminal exited" in out
            assert not _has_terminal(caprole)
        finally:
            run(tool.cleanup_session("t_short2"))

    def test_false_returns_exit_code(self, caprole, workspace):
        tool = bind(Terminal(), caprole, session_id="t_short3")
        try:
            out = run(tool.call(input="false", yield_time_ms=2000))
            assert "[exit code: 1]" in out
        finally:
            run(tool.cleanup_session("t_short3"))


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
            await tool.cleanup_session("t_cwd")

        run(scenario())

    def test_env_persists_across_calls(self, caprole, workspace):
        tool = bind(Terminal(), caprole, session_id="t_env")

        async def scenario():
            await tool.call(input="export FOO=bar123", yield_time_ms=2000)
            out = await tool.call(input="echo $FOO", yield_time_ms=2000)
            assert "bar123" in out
            await tool.cleanup_session("t_env")

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
            await tool.cleanup_session("t_py")

        run(scenario())

    def test_interrupt_reclaims_hung_command(self, caprole, workspace):
        tool = bind(Terminal(), caprole, session_id="t_int")

        async def scenario():
            started = await tool.call(input="sleep 30", yield_time_ms=600)
            assert "still running" in started

            done = await tool.call(interrupt=True, yield_time_ms=1500)
            # Ctrl-C returns control to the shell prompt.
            assert "still running" not in done
            await tool.cleanup_session("t_int")

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
            assert not _has_terminal(caprole)

        run(scenario())

    def test_cleanup_terminates_live_terminal(self, caprole, workspace):
        tool = bind(Terminal(), caprole, session_id="t_clean")

        async def scenario():
            await tool.call(input="sleep 30", yield_time_ms=500)
            assert _has_terminal(caprole)
            await tool.cleanup_session("t_clean")
            assert not _has_terminal(caprole)
            await tool.cleanup_session("t_clean")  # idempotent — must not raise

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
            run(tool.cleanup_session("t_big"))


# ---------------------------------------------------------------------------
# terminal-state capture / restore (session resume)
# ---------------------------------------------------------------------------


class TestStateCaptureRestore:
    def test_capture_records_cwd_and_env_diff(self, caprole, workspace):
        """After cd + export, the Runtime checkpoint carries cwd + env diff."""
        sub = workspace / "sub"
        sub.mkdir()
        tool = bind(Terminal(), caprole, session_id="t_cap")

        async def scenario():
            await tool.call(input=f"cd {sub} && export CAP_FOO=cap_bar", yield_time_ms=2000)
            await tool.cleanup_session("t_cap")

        run(scenario())
        state = caprole.latest_runtime_state("terminal", "terminal-state+json@1")
        env = state["env"]
        assert state["cwd"] == str(sub)
        assert env.get("CAP_FOO") == "cap_bar"
        # Noise keys must be filtered out of the diff.
        assert "PWD" not in env and "SHLVL" not in env and "_" not in env

    def test_restore_state_reseeds_new_shell(self, caprole, workspace):
        """restore_state injects cwd/env into a fresh shell (no user command rerun)."""
        sub = workspace / "restored"
        sub.mkdir()
        tool = bind(Terminal(), caprole, session_id="t_restore")

        async def scenario():
            await tool._ensure_runtime()
            async with caprole.runtime_host.access("terminal:default", mode="write", owner_id="test:restore") as access:
                access.commit()
                driver = access.driver
                assert isinstance(driver, TerminalRuntimeDriver)
                await driver.session.restore_state(str(sub), {"REZ_FOO": "rez_bar"}, [])
            pwd = await tool.call(input="pwd", yield_time_ms=2000)
            val = await tool.call(input="echo $REZ_FOO", yield_time_ms=2000)
            assert str(sub) in pwd
            assert "rez_bar" in val
            await tool.cleanup_session("t_restore")

        run(scenario())

    def test_pending_restore_applied_on_ensure_session(self, caprole, workspace):
        """Runtime creation consumes the pending restore and re-seeds the shell."""
        sub = workspace / "pending"
        sub.mkdir()
        caprole.stage_runtime_checkpoint(
            "terminal",
            "terminal-state+json@1",
            {"cwd": str(sub), "env": {"PEND_FOO": "pend_bar"}, "unset": []},
        )
        tool = bind(Terminal(), caprole, session_id="t_pending")

        async def scenario():
            await tool._ensure_runtime()  # applies pending restore once
            pwd = await tool.call(input="pwd", yield_time_ms=2000)
            val = await tool.call(input="echo $PEND_FOO", yield_time_ms=2000)
            assert str(sub) in pwd
            assert "pend_bar" in val
            await tool.cleanup_session("t_pending")

        run(scenario())

    def test_restore_value_is_quoted_no_injection(self, caprole, workspace):
        """A value with shell metacharacters is taken literally (no $(...) eval)."""
        tool = bind(Terminal(), caprole, session_id="t_quote")

        async def scenario():
            await tool._ensure_runtime()
            async with caprole.runtime_host.access("terminal:default", mode="write", owner_id="test:quote") as access:
                access.commit()
                driver = access.driver
                assert isinstance(driver, TerminalRuntimeDriver)
                await driver.session.restore_state("", {"INJ": "$(echo pwned)"}, [])
            out = await tool.call(input='echo "$INJ"', yield_time_ms=2000)
            assert "$(echo pwned)" in out
            assert "pwned" not in out.replace("$(echo pwned)", "")
            await tool.cleanup_session("t_quote")

        run(scenario())
