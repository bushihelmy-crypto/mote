#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Shared fixtures for the real-tool test suite (``metagpt.executor.tools``).

Everything stays fully offline and deterministic — no LLM, no network, no MCP.
The real tools only ever touch:

- the local filesystem (Read/Write/Edit/NotebookEdit/Grep/Glob/Bash), which we
  point at a per-test ``tmp_path`` workspace and ``chdir`` into so relative
  paths and ``os.getcwd()`` are predictable;
- a handful of narrow Role capabilities (cwd accessors, the shared file-read
  state, ``wait_interruptible``, ``end_session``, ``ask_human``,
  ``reply_to_human``), all provided here by ``CapRole`` — a configurable fake
  Role that publishes an explicit ``tool_capabilities()`` allowlist exactly like
  the real Role, so ``BaseTool.bind`` injects only what each tool declares.

Helpers:
- ``CapRole`` — fake Role; tracks cwd + an in-memory file-read-state dict, lets
  tests script ``ask_human``/``end_session`` replies.
- ``bind`` — bind a tool to a session + role (returns the tool).
- ``run`` — drive a (bound) tool's ``async call(**kwargs)`` to completion.
- ``workspace`` — a tmp dir the test cwd is switched into.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Callable, Optional

import pytest

from metagpt.executor.base_tool import BaseTool


# ---------------------------------------------------------------------------
# Fake Role publishing a capability allowlist
# ---------------------------------------------------------------------------


class CapRole:
    """Minimal Role stand-in exposing the capabilities the real tools require.

    Each capability is a plain callable kept on the instance so tests can read
    back what a tool did (cwd writes, recorded reads) or script replies. Only
    the names listed in ``tool_capabilities()`` are visible to ``bind`` — a tool
    can never reach anything else.
    """

    def __init__(
        self,
        *,
        cwd: Optional[str] = None,
        ask_reply: str = "",
        end_output: str = "session ended",
        wait_result: Optional[tuple[float, bool]] = None,
    ) -> None:
        self._cwd = cwd or os.getcwd()
        # Shared file-read state: full_path -> mtime_ns (the real Role's readFileState).
        self.read_state: dict[str, int] = {}
        # Before-image snapshot calls: (full_path, tool) recorded for assertions.
        self.snapshots: list[tuple[str, str]] = []
        # Scriptable human/session behaviour.
        self.ask_reply = ask_reply
        self.ask_questions: list[str] = []  # records every prompt sent to ask_human
        self.end_output = end_output
        self.end_calls = 0
        # (slept_seconds, interrupted); defaults to "slept the full duration".
        self._wait_result = wait_result

    # --- cwd accessors (Bash) ---
    def get_cwd(self) -> str:
        return self._cwd

    def set_cwd(self, path: str) -> None:
        self._cwd = path

    # --- shared file-read state (Read records, Write/Edit/NotebookEdit enforce) ---
    def record_file_read(self, path: str, mtime_ns: int) -> None:
        self.read_state[path] = mtime_ns

    def get_file_read_mtime(self, path: str) -> Optional[int]:
        return self.read_state.get(path)

    # --- file-history snapshot (Write/Edit/NotebookEdit capture before-images) ---
    def record_file_snapshot(self, full_path: str, *, tool: str = "") -> None:
        self.snapshots.append((full_path, tool))

    # --- human / session ---
    async def ask_human(self, question: str) -> str:
        self.ask_questions.append(question)
        return self.ask_reply

    async def reply_to_human(self, content: str) -> str:
        return content

    async def end_session(self) -> str:
        self.end_calls += 1
        return self.end_output

    # --- sleep ---
    async def wait_interruptible(self, duration_seconds: float) -> tuple[float, bool]:
        if self._wait_result is not None:
            return self._wait_result
        return (duration_seconds, False)

    # --- the allowlist bind() consults ---
    def tool_capabilities(self) -> dict[str, Any]:
        return {
            "get_cwd": self.get_cwd,
            "set_cwd": self.set_cwd,
            "record_file_read": self.record_file_read,
            "get_file_read_mtime": self.get_file_read_mtime,
            "record_file_snapshot": self.record_file_snapshot,
            "ask_human": self.ask_human,
            "reply_to_human": self.reply_to_human,
            "end_session": self.end_session,
            "wait_interruptible": self.wait_interruptible,
        }


# ---------------------------------------------------------------------------
# Binding / driving helpers
# ---------------------------------------------------------------------------


def bind(tool: BaseTool, role: Optional[CapRole] = None, session_id: str = "sess") -> BaseTool:
    """Bind a tool to a session (+ optional role) and return it."""
    return tool.bind(session_id, role=role)


def run(coro):
    """Drive a coroutine (a tool's ``call``) to completion synchronously."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Filesystem fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A tmp directory the process cwd is switched into for the test.

    Tools resolve relative paths against ``os.getcwd()`` and relativize output
    against it, so anchoring the cwd makes those deterministic.
    """
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def role():
    """A fresh CapRole whose cwd starts at the current working directory."""
    return CapRole()


def write_file(path, content: str, *, newline: str = "") -> str:
    """Write *content* to *path* (str/Path), return the absolute path.

    ``newline=""`` disables translation so callers control byte-exact endings.
    """
    full = os.path.abspath(os.path.expanduser(str(path)))
    with open(full, "w", encoding="utf-8", newline=newline) as f:
        f.write(content)
    return full


def mark_read(role: CapRole, full_path: str) -> None:
    """Record *full_path* in the role's file-read state at its current mtime.

    Mirrors what a successful Read does, so a Write/Edit guard sees the file as
    "read this session and unchanged since".
    """
    role.record_file_read(full_path, os.stat(full_path).st_mtime_ns)
