#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the persistent Python-kernel ``python`` tool.

Drives a real ipykernel through the tool's ``call``, using the shared
``CapRole``/``bind``/``run``/``workspace`` harness. Everything is local and
offline.

A live kernel keeps its client channels on the event loop it was started on, so
multi-call scenarios run inside ONE ``asyncio.run`` (the conftest ``run`` opens a
fresh loop per call). The live session is owned by the per-test ``CapRole``
(stored on its ``tool_sessions``, mirroring ``RoleState._tool_sessions``), so
there is no process-global singleton to leak across tests; each test still tears
its kernel down to free the subprocess.
"""
from __future__ import annotations

import pytest

from metagpt.executor.dependency._kernel import _cap_text, _strip_ansi
from metagpt.executor.tools.python import Python

from .conftest import CapRole, bind, run


def _has_kernel(role: CapRole) -> bool:
    """Whether a live kernel session is stored on the (fake) Role."""
    return role.get_tool_session("Jupyter") is not None


@pytest.fixture
def caprole(workspace):
    return CapRole(cwd=str(workspace))


# ---------------------------------------------------------------------------
# execute — output + persistent state
# ---------------------------------------------------------------------------


class TestExecute:
    def test_stdout_captured(self, caprole, workspace):
        tool = bind(Python(), caprole, session_id="k_out")

        async def scenario():
            out = await tool.call(code="print('hello')")
            assert "hello" in out
            await tool.call(close=True)

        run(scenario())

    def test_expression_repr(self, caprole, workspace):
        tool = bind(Python(), caprole, session_id="k_repr")

        async def scenario():
            out = await tool.call(code="40 + 2")
            assert "42" in out
            await tool.call(close=True)

        run(scenario())

    def test_state_persists_across_calls(self, caprole, workspace):
        tool = bind(Python(), caprole, session_id="k_state")

        async def scenario():
            await tool.call(code="x = 100")
            out = await tool.call(code="print(x + 1)")
            assert "101" in out
            await tool.call(close=True)

        run(scenario())

    def test_error_traceback_ansi_stripped(self, caprole, workspace):
        tool = bind(Python(), caprole, session_id="k_err")

        async def scenario():
            out = await tool.call(code="raise ValueError('boom')")
            assert "ValueError" in out
            assert "boom" in out
            assert "\x1b[" not in out  # ANSI stripped
            await tool.call(close=True)

        run(scenario())

    def test_cwd_seeded_from_role(self, caprole, workspace):
        tool = bind(Python(), caprole, session_id="k_cwd")

        async def scenario():
            out = await tool.call(code="import os; print(os.getcwd())")
            assert str(workspace) in out
            await tool.call(close=True)

        run(scenario())


# ---------------------------------------------------------------------------
# timeout -> interrupt + partial output, state preserved
# ---------------------------------------------------------------------------


class TestTimeout:
    def test_timeout_interrupts_and_preserves_state(self, caprole, workspace):
        tool = bind(Python(), caprole, session_id="k_to")

        async def scenario():
            await tool.call(code="y = 7")
            out = await tool.call(
                code="import time\nprint('start', flush=True)\ntime.sleep(30)",
                timeout=2,
            )
            assert "timed out" in out
            assert "start" in out
            # State survived the interrupt.
            alive = await tool.call(code="print(y)")
            assert "7" in alive
            await tool.call(close=True)

        run(scenario())


# ---------------------------------------------------------------------------
# interrupt / restart / close
# ---------------------------------------------------------------------------


class TestControl:
    def test_restart_clears_state(self, caprole, workspace):
        tool = bind(Python(), caprole, session_id="k_restart")

        async def scenario():
            await tool.call(code="z = 999")
            msg = await tool.call(restart=True)
            assert "restarted" in msg
            out = await tool.call(code="print('z' in dir())")
            assert "False" in out
            await tool.call(close=True)

        run(scenario())

    def test_interrupt_without_running_code(self, caprole, workspace):
        tool = bind(Python(), caprole, session_id="k_int")

        async def scenario():
            await tool.call(code="pass")
            msg = await tool.call(interrupt=True)
            assert "interrupted" in msg
            await tool.call(close=True)

        run(scenario())

    def test_interrupt_without_kernel_raises(self, caprole, workspace):
        from metagpt.executor.tool_result import ToolError

        tool = bind(Python(), caprole, session_id="k_noint")
        with pytest.raises(ToolError):
            run(tool.call(interrupt=True))

    def test_close_no_kernel(self, caprole, workspace):
        tool = bind(Python(), caprole, session_id="k_close0")
        out = run(tool.call(close=True))
        assert "no kernel to close" in out

    def test_close_after_use(self, caprole, workspace):
        tool = bind(Python(), caprole, session_id="k_close1")

        async def scenario():
            await tool.call(code="a = 1")
            out = await tool.call(close=True)
            assert "kernel closed" in out
            assert not _has_kernel(caprole)

        run(scenario())

    def test_cleanup_terminates_live_kernel(self, caprole, workspace):
        tool = bind(Python(), caprole, session_id="k_cleanup")

        async def scenario():
            await tool.call(code="b = 2")
            assert _has_kernel(caprole)
            tool.cleanup_session("k_cleanup")
            assert not _has_kernel(caprole)
            tool.cleanup_session("k_cleanup")  # idempotent

        run(scenario())


# ---------------------------------------------------------------------------
# helpers (unit)
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_strip_ansi(self):
        assert _strip_ansi("\x1b[31mred\x1b[0m") == "red"

    def test_cap_text_keeps_head_tail(self):
        text = "H" * 10 + "M" * 2_000_000 + "T" * 10
        capped = _cap_text(text)
        assert "omitted" in capped
        assert capped.startswith("H")
        assert capped.endswith("T")
        assert len(capped) < len(text)

    def test_cap_text_short_unchanged(self):
        assert _cap_text("short") == "short"
