#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Offline tests for the rich rendering layer.

All output is captured by injecting ``Console(file=StringIO(), force_terminal=True)``
into :class:`ConsoleRenderer`; assertions check for substrings / control-sequence
presence, never pixel-level layout.
"""

from __future__ import annotations

import io
import types

import pytest

from metagpt.cli import render
from metagpt.cli.render import ConsoleRenderer, build_renderer


def _make_renderer(width: int = 100):
    from rich.console import Console

    out = io.StringIO()
    console = Console(file=out, force_terminal=True, width=width)
    return ConsoleRenderer(console), out


def _hook_input(event: str, payload: dict):
    return types.SimpleNamespace(hook_event_name=event, payload=payload)


# ---------------------------------------------------------------------------
# _pre_tool
# ---------------------------------------------------------------------------
def test_pre_tool_bash_shows_name_and_command():
    r, out = _make_renderer()
    r._pre_tool({"tool_name": "Bash", "tool_input": {"command": "ls -la /tmp"}})
    text = out.getvalue()
    assert "Bash" in text
    # The bash lexer colorizes whitespace, so check tokens individually.
    assert "ls" in text
    assert "-la" in text
    assert "/tmp" in text


def test_pre_tool_write_shows_path_and_content():
    r, out = _make_renderer()
    r._pre_tool(
        {
            "tool_name": "Write",
            "tool_input": {"file_path": "scraper.py", "content": "import os\nprint(os.getcwd())"},
        }
    )
    text = out.getvalue()
    assert "Write" in text
    assert "scraper.py" in text
    assert "import" in text


def test_pre_tool_unknown_tool_falls_back_to_json():
    r, out = _make_renderer()
    r._pre_tool({"tool_name": "MysteryTool", "tool_input": {"foo": "bar", "n": 3}})
    text = out.getvalue()
    assert "MysteryTool" in text
    assert "foo" in text
    assert "bar" in text


def test_pre_tool_ask_user_question_is_skipped():
    r, out = _make_renderer()
    r._pre_tool({"tool_name": "AskUserQuestion", "tool_input": {"questions": []}})
    assert out.getvalue() == ""


def test_pre_tool_title_only_when_no_body():
    r, out = _make_renderer()
    # Bash with no command -> title-only panel, must not crash.
    r._pre_tool({"tool_name": "Bash", "tool_input": {}})
    assert "Bash" in out.getvalue()


# ---------------------------------------------------------------------------
# _post_tool
# ---------------------------------------------------------------------------
def test_post_tool_success_shows_check_and_summary():
    r, out = _make_renderer()
    r._post_tool({"tool_name": "Bash", "tool_response": "total 8\nfile-a\nfile-b"})
    text = out.getvalue()
    assert "✓" in text
    assert "total 8" in text


def test_post_tool_failure_shows_cross():
    r, out = _make_renderer()
    r._post_tool({"tool_name": "Bash", "tool_response": "[PERMISSION DENIED] not allowed"})
    text = out.getvalue()
    assert "✗" in text
    assert "PERMISSION DENIED" in text


def test_post_tool_error_prefix_is_failure():
    r, out = _make_renderer()
    r._post_tool({"tool_name": "Read", "tool_response": "Error: file not found"})
    assert "✗" in out.getvalue()


def test_post_tool_empty_output_success():
    r, out = _make_renderer()
    r._post_tool({"tool_name": "Write", "tool_response": ""})
    text = out.getvalue()
    assert "✓" in text
    assert "no output" in text


# ---------------------------------------------------------------------------
# stream (live Markdown)
# ---------------------------------------------------------------------------
def test_stream_renders_accumulated_text():
    r, out = _make_renderer()
    r.stream("hello")
    r.stream(" world")
    r.end_stream()  # finalize the Live region so the last frame flushes
    text = out.getvalue()
    assert "hello" in text
    assert "world" in text
    # Rendered as one Markdown paragraph, not split across the two increments.
    assert "hello\n world" not in text


def test_stream_renders_markdown_formatting():
    r, out = _make_renderer()
    for tok in ("# Title", "\n\nsome ", "**bold**", " text"):
        r.stream(tok)
    r.end_stream()
    text = out.getvalue()
    assert "Title" in text
    assert "bold" in text


def test_end_stream_is_idempotent_and_noop_when_idle():
    r, out = _make_renderer()
    r.end_stream()  # no active stream -> harmless
    r.stream("hi")
    r.end_stream()
    r.end_stream()  # second call is a no-op
    assert "hi" in out.getvalue()


def test_other_output_finalizes_active_stream():
    # Any non-stream output (here a tool panel) must close the live region first.
    r, out = _make_renderer()
    r.stream("thinking")
    r._pre_tool({"tool_name": "Bash", "tool_input": {"command": "ls"}})
    assert r._live is None  # stream finalized by _pre_tool
    text = out.getvalue()
    assert "thinking" in text
    assert "Bash" in text


# ---------------------------------------------------------------------------
# on_hook dispatch
# ---------------------------------------------------------------------------
def test_on_hook_dispatches_pre_tool():
    r, out = _make_renderer()
    result = r.on_hook(_hook_input("PreToolUse", {"tool_name": "Bash", "tool_input": {"command": "echo hi"}}))
    assert result is None
    assert "Bash" in out.getvalue()


def test_on_hook_dispatches_post_tool():
    r, out = _make_renderer()
    r.on_hook(_hook_input("PostToolUse", {"tool_name": "Bash", "tool_response": "ok"}))
    assert "✓" in out.getvalue()


def test_on_hook_ignores_unknown_event():
    r, out = _make_renderer()
    r.on_hook(_hook_input("SessionStart", {"source": "startup"}))
    assert out.getvalue() == ""


# ---------------------------------------------------------------------------
# build_renderer
# ---------------------------------------------------------------------------
def test_build_renderer_returns_instance_when_rich_available():
    r = build_renderer(out=io.StringIO())
    assert isinstance(r, ConsoleRenderer)


def test_build_renderer_returns_none_without_rich(monkeypatch):
    monkeypatch.setattr(render, "_HAS_RICH", False)
    assert build_renderer() is None
    assert build_renderer(out=io.StringIO()) is None


# ---------------------------------------------------------------------------
# basic output helpers
# ---------------------------------------------------------------------------
def test_assistant_renders_markdown():
    r, out = _make_renderer()
    r.assistant("# Title\n\nsome **bold** text")
    text = out.getvalue()
    assert "Title" in text
    assert "bold" in text


def test_notice_and_prompt_emit_text():
    r, out = _make_renderer()
    r.notice("(Press Ctrl-C again)")
    r.prompt("> ")
    text = out.getvalue()
    assert "Press Ctrl-C again" in text
    assert ">" in text
