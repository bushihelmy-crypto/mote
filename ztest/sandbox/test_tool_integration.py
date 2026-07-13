#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Integration tests: the three execution tools thread the SandboxRuntime through.

Rather than spinning up a real bwrap sandbox (which may be unavailable), these
use a recording fake runtime that captures the command/argv it was asked to wrap
and returns a harmless passthrough. This proves the wiring — Bash -> aexecute ->
wrap_command, Terminal -> TerminalSession.start -> wrap_exec, Python ->
KernelSession.start -> wrap_exec — without depending on the host backend.

A CapRole publishing the fake runtime via the ``get_sandbox_runtime`` capability
exercises the real bind() path the production Role uses.
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

from mote.executor.dependency._kernel import KernelSession
from mote.executor.dependency._terminal import TerminalSession
from mote.executor.tools.bash import Bash
from mote.ztest.executor.tools.conftest import CapRole, bind, run


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A tmp dir the process cwd is switched into (mirrors the tools conftest)."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


class RecordingRuntime:
    """A fake SandboxRuntime that records wrap calls and passes commands through.

    ``wrap_command`` prefixes a marker into the env so a test can confirm the
    command actually went through the runtime; ``wrap_exec`` returns the argv
    unchanged (so a real shell/kernel still launches) but records the call.
    """

    def __init__(self) -> None:
        self.wrap_command_calls: list[str] = []
        self.wrap_exec_calls: list[list[str]] = []
        self.wrap_exec_extra_writable: list[list[str] | None] = []

    async def wrap_command(self, command, *, cwd=None, env=None):
        self.wrap_command_calls.append(command)
        out_env = dict(env or {})
        out_env["SANDBOX_MARKER"] = "1"
        return command, out_env

    async def wrap_exec(self, argv, *, cwd=None, env=None, extra_writable=None):
        self.wrap_exec_calls.append(list(argv))
        self.wrap_exec_extra_writable.append(extra_writable)
        return list(argv), dict(env or {})


# --- Bash -> aexecute -> wrap_command --------------------------------------


def test_bash_threads_runtime_to_aexecute(workspace):
    rt = RecordingRuntime()
    role = CapRole(cwd=str(workspace), sandbox_runtime=rt)
    tool = bind(Bash(), role=role)
    out = run(tool.call(command="echo hello")).output
    assert "hello" in out
    # The command went through the runtime's wrap_command (aexecute appends a
    # cwd-sync probe, so match the prefix rather than exact equality).
    assert len(rt.wrap_command_calls) == 1
    assert rt.wrap_command_calls[0].startswith("echo hello")


def test_bash_without_runtime_runs_unsandboxed(workspace):
    role = CapRole(cwd=str(workspace), sandbox_runtime=None)
    tool = bind(Bash(), role=role)
    out = run(tool.call(command="echo plain")).output
    assert "plain" in out


# --- Terminal -> TerminalSession.start -> wrap_exec ------------------------


def test_terminal_session_wraps_shell_argv(workspace):
    rt = RecordingRuntime()

    async def scenario():
        session = TerminalSession(session_key="t1", cwd=str(workspace), sandbox_runtime=rt)
        await session.start()
        try:
            # The shell argv was routed through wrap_exec exactly once.
            assert len(rt.wrap_exec_calls) == 1
            argv = rt.wrap_exec_calls[0]
            assert argv[0] in (os.environ.get("SHELL"), "/bin/bash")
            assert "-i" in argv
            # Sanity: the live shell actually runs a command.
            text, code, at_prompt, closed = await session.feed("echo hi", 5000)
            assert "hi" in text
        finally:
            session.shutdown()

    asyncio.run(scenario())


def test_terminal_session_without_runtime(workspace):
    async def scenario():
        session = TerminalSession(session_key="t2", cwd=str(workspace), sandbox_runtime=None)
        await session.start()
        try:
            text, code, at_prompt, closed = await session.feed("echo bare", 5000)
            assert "bare" in text
        finally:
            session.shutdown()

    asyncio.run(scenario())


# --- Python kernel -> KernelSession.start -> wrap_exec ---------------------


def test_kernel_session_wraps_launch_cmd(workspace):
    rt = RecordingRuntime()

    async def scenario():
        session = KernelSession(session_key="k1", cwd=str(workspace), sandbox_runtime=rt)
        await session.start()
        try:
            # The ipykernel launch command was routed through wrap_exec.
            assert len(rt.wrap_exec_calls) == 1
            argv = rt.wrap_exec_calls[0]
            assert sys.executable in argv
            assert "ipykernel_launcher" in argv
            # Sandboxed kernels use ipc:// (unix sockets, netns-immune) instead
            # of the historical loopback TCP.
            assert session._km.transport == "ipc"
            # The connection file is PRE-RESOLVED to a concrete .json path inside
            # the ephemeral socket dir (no ``{connection_file}`` placeholder — the
            # netns launcher would never substitute a brace nested in its token).
            assert "-f" in argv
            conn = argv[argv.index("-f") + 1]
            assert conn.endswith(".json")
            assert "{connection_file}" not in argv
            assert session._sock_dir is not None
            assert conn.startswith(session._sock_dir)
            # The socket dir is declared writable so bwrap bind-mounts it (host
            # client + sandboxed kernel share the same socket inodes).
            assert rt.wrap_exec_extra_writable[0] == [session._sock_dir]
            # Sanity: the kernel actually executes code (ipc + absolute prefix is
            # verified to work on the host).
            text, timed_out = await session.execute("print(2 + 2)", 30)
            assert "4" in text
            assert not timed_out
            sock_dir = session._sock_dir
        finally:
            await session.shutdown()
        # The ephemeral socket dir is cleaned up at teardown.
        assert not os.path.exists(sock_dir)

    asyncio.run(scenario())
