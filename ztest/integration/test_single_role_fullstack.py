#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""End-to-end: a real ``Role.run`` driving real tools via a scripted LLM.

These exercise the whole think→act stack (Role → ReActLoop → ThinkEngine +
ToolExecutor → ContextManager → native command channel) with only the LLM
faked. The scripted LLM emits provider-native ``tool_calls``; the loop runs
them against the real filesystem tools rooted at a tmp workspace, then a final
no-tool_calls turn terminates the loop.
"""
from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.asyncio


async def test_write_then_read_then_finish(make_role, tmp_path):
    """Write a file, read it back, then finish — all via real tools."""
    target = os.path.join(str(tmp_path), "hello.txt")
    role = make_role(
        working_dir=str(tmp_path),
        tools=["Read", "Write"],
        turns=[
            [("Write", {"file_path": target, "content": "hello world"})],
            [("Read", {"file_path": target})],
            "All done.",  # terminal: no tool_calls
        ],
    )

    rsp = await role.run(with_message="please create hello.txt")

    # The Write tool really hit the disk.
    assert os.path.exists(target)
    with open(target, encoding="utf-8") as f:
        assert f.read() == "hello world"

    # The loop terminated on the plain-text turn and surfaced it as the reply.
    assert rsp is not None
    assert "All done." in rsp.content

    # Exactly one think round per scripted turn fired: Write, Read, terminal.
    # (is_terminal now joins the think task before reading its result, so the
    # finishing turn is detected in its own round — no wasted extra think.)
    assert len(role.scripted_llm.tool_calls_seen) == 3
    # The executor's native tool specs were handed to the LLM each round.
    assert role.scripted_llm.tools_seen[0]  # non-empty native specs


async def test_history_records_assistant_and_tool_results(make_role, tmp_path):
    """The native channel records the assistant turn + paired tool results."""
    target = os.path.join(str(tmp_path), "note.txt")
    role = make_role(
        working_dir=str(tmp_path),
        tools=["Write"],
        turns=[
            [("Write", {"file_path": target, "content": "abc"})],
            "finished",
        ],
    )

    await role.run(with_message="write a note")

    messages = role.context_manager.get()
    contents = [m.content for m in messages]
    # The user requirement, an assistant turn, and a tool-result message are all
    # in the stored history.
    assert any("write a note" in c for c in contents)
    # The Write tool's success output is recorded as a tool result.
    assert any("note.txt" in c or "abc" in c or "ok" in c.lower() for c in contents)


async def test_edit_existing_file_end_to_end(make_role, tmp_path):
    """Read-then-Edit on a real file mutates it on disk."""
    target = os.path.join(str(tmp_path), "code.py")
    with open(target, "w", encoding="utf-8") as f:
        f.write("x = 1\n")

    role = make_role(
        working_dir=str(tmp_path),
        tools=["Read", "Edit"],
        turns=[
            [("Read", {"file_path": target})],
            [("Edit", {"file_path": target, "old_string": "x = 1", "new_string": "x = 2"})],
            "edited",
        ],
    )

    await role.run(with_message="bump x to 2")

    with open(target, encoding="utf-8") as f:
        assert "x = 2" in f.read()


async def test_unknown_tool_call_is_filtered_by_valid_names(make_role, tmp_path):
    """A tool_call outside the role's declared tools is dropped, not executed."""
    target = os.path.join(str(tmp_path), "ok.txt")
    role = make_role(
        working_dir=str(tmp_path),
        tools=["Write"],  # Bash NOT declared
        turns=[
            # Bash is filtered out (not in valid_names); Write runs.
            [
                ("Bash", {"command": "echo nope"}),
                ("Write", {"file_path": target, "content": "yes"}),
            ],
            "done",
        ],
    )

    await role.run(with_message="go")

    assert os.path.exists(target)
    with open(target, encoding="utf-8") as f:
        assert f.read() == "yes"


async def test_no_news_short_circuits(make_role, tmp_path):
    """With no observed message the loop never thinks."""
    role = make_role(working_dir=str(tmp_path), tools=["Write"], turns=["unused"])

    rsp = await role.run()  # no with_message, empty buffer

    assert rsp is None
    assert role.scripted_llm.tool_calls_seen == []


async def test_multiple_tool_calls_in_one_turn(make_role, tmp_path):
    """Two tool calls emitted in a single think round both execute, in order."""
    a = os.path.join(str(tmp_path), "a.txt")
    b = os.path.join(str(tmp_path), "b.txt")
    role = make_role(
        working_dir=str(tmp_path),
        tools=["Write"],
        turns=[
            [
                ("Write", {"file_path": a, "content": "AAA"}),
                ("Write", {"file_path": b, "content": "BBB"}),
            ],
            "both written",
        ],
    )

    await role.run(with_message="write two files")

    assert open(a, encoding="utf-8").read() == "AAA"
    assert open(b, encoding="utf-8").read() == "BBB"
    # Two scripted turns -> two think rounds (the tool-call turn + the terminal).
    assert len(role.scripted_llm.tool_calls_seen) == 2


async def test_failure_mid_turn_skips_rest_then_replans(make_role, tmp_path):
    """A failing tool aborts the rest of its turn; the model replans next round.

    Reading a non-existent file fails, so the same-turn Write is recorded as
    ``[SKIPPED]`` (never hits disk). The next round retries the Write, which
    succeeds — exactly the replan-after-failure contract, end-to-end.
    """
    missing = os.path.join(str(tmp_path), "nope.txt")
    out = os.path.join(str(tmp_path), "out.txt")
    role = make_role(
        working_dir=str(tmp_path),
        tools=["Read", "Write"],
        turns=[
            # Read fails -> the same-turn Write is SKIPPED, not executed.
            [
                ("Read", {"file_path": missing}),
                ("Write", {"file_path": out, "content": "skipped-content"}),
            ],
            # Replan: the Write now runs and lands "final" (not the skipped text).
            [("Write", {"file_path": out, "content": "final"})],
            "done",
        ],
    )

    await role.run(with_message="read then write")

    # The first-round Write never ran; the replanned write produced the content.
    assert open(out, encoding="utf-8").read() == "final"
    contents = [m.content for m in role.context_manager.get()]
    assert any("[SKIPPED]" in c for c in contents)
    assert len(role.scripted_llm.tool_calls_seen) == 3


async def test_glob_then_grep_search_end_to_end(make_role, tmp_path):
    """Real Glob + Grep run against a seeded workspace and surface their hits."""
    foo = os.path.join(str(tmp_path), "foo.py")
    bar = os.path.join(str(tmp_path), "bar.py")
    with open(foo, "w", encoding="utf-8") as f:
        f.write("def hello():\n    return 42\n")
    with open(bar, "w", encoding="utf-8") as f:
        f.write("x = 1\n")

    role = make_role(
        working_dir=str(tmp_path),
        tools=["Glob", "Grep"],
        turns=[
            [("Glob", {"pattern": "*.py"})],
            [("Grep", {"pattern": "hello", "path": str(tmp_path), "output_mode": "content"})],
            "done",
        ],
    )

    await role.run(with_message="find the hello function")

    blob = "\n".join(m.content for m in role.context_manager.get())
    # Glob listed the python files; Grep located the match inside foo.py.
    assert "foo.py" in blob
    assert "hello" in blob


async def test_empty_terminal_text_finishes_cleanly(make_role, tmp_path):
    """A terminal turn with empty content finishes with an empty reply."""
    target = os.path.join(str(tmp_path), "e.txt")
    role = make_role(
        working_dir=str(tmp_path),
        tools=["Write"],
        turns=[
            [("Write", {"file_path": target, "content": "x"})],
            "",  # terminal turn carrying no text
        ],
    )

    rsp = await role.run(with_message="write then stop silently")

    assert os.path.exists(target)
    assert rsp is not None
    assert rsp.content == ""
