#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for :class:`metagpt.parser.xml_channel.XmlCommandChannel`.

``iter_commands`` runs the *real* XML lexer (``parse_commands2`` ->
``PythonObjectParser``), so the inputs here are genuine XML command blocks in
the documented OUTPUT_SECTION shape. ``record_turn`` and ``output_format`` are
exercised against the channel directly; the think round is faked via
:class:`FakeThinkEngine`.
"""
from __future__ import annotations

import pytest

from metagpt.common.base import CommandChannel
from metagpt.common.const import IMAGES, PDFS
from metagpt.common.prompt.output import OUTPUT_SECTION
from metagpt.parser.xml_channel import XmlCommandChannel

from .conftest import FakeMemory, FakeThinkEngine, collect, executed_command


def xml_command(name: str, **args: str) -> str:
    """Render a single XML command block in the OUTPUT_SECTION shape."""
    body = "".join(f"<{k}>\n{v}\n</{k}>\n" for k, v in args.items())
    return f"<{name}>\n{body}</{name}>"


class TestContract:
    def test_is_command_channel(self):
        assert isinstance(XmlCommandChannel(), CommandChannel)

    def test_output_format_is_output_section(self):
        assert XmlCommandChannel().output_format() == OUTPUT_SECTION

    def test_tool_specs_is_none(self):
        # Text channel passes no native specs to the LLM.
        assert XmlCommandChannel().tool_specs(object()) is None


class TestIterCommands:
    @pytest.mark.asyncio
    async def test_parses_single_command(self):
        rsp = "Some thoughts...\n" + xml_command("Read", path="a.py")
        engine = FakeThinkEngine(content=rsp)
        cmds = await collect(XmlCommandChannel(), engine, {"Read"})
        assert len(cmds) == 1
        assert cmds[0]["command_name"] == "Read"
        assert cmds[0]["args"] == {"path": "a.py"}
        # XML has no provider tool-call id, and the loop fields are filled in.
        assert cmds[0]["id"] is None
        assert cmds[0]["status"] == "running"
        assert cmds[0]["error_msg"] == ""

    @pytest.mark.asyncio
    async def test_parses_multiple_commands_in_order(self):
        rsp = (
            "think\n"
            + xml_command("Read", path="a.py")
            + "\nthink more\n"
            + xml_command("Glob", pattern="*.py")
        )
        engine = FakeThinkEngine(content=rsp)
        cmds = await collect(XmlCommandChannel(), engine, {"Read", "Glob"})
        assert [c["command_name"] for c in cmds] == ["Read", "Glob"]
        assert cmds[0]["args"] == {"path": "a.py"}
        assert cmds[1]["args"] == {"pattern": "*.py"}

    @pytest.mark.asyncio
    async def test_command_with_multiple_args(self):
        rsp = xml_command("Edit", file="x.py", old="a", new="b")
        engine = FakeThinkEngine(content=rsp)
        cmds = await collect(XmlCommandChannel(), engine, {"Edit"})
        assert cmds[0]["args"] == {"file": "x.py", "old": "a", "new": "b"}

    @pytest.mark.asyncio
    async def test_unknown_command_filtered_by_valid_names(self):
        # The lexer skips function tags not in valid_names; an all-unknown
        # response parses to no commands -> "No valid commands found" -> yields none.
        rsp = xml_command("Nope", x="1")
        engine = FakeThinkEngine(content=rsp)
        assert await collect(XmlCommandChannel(), engine, {"Read"}) == []

    @pytest.mark.asyncio
    async def test_known_kept_unknown_dropped(self):
        rsp = xml_command("Read", path="a.py") + "\n" + xml_command("Nope", x="1")
        engine = FakeThinkEngine(content=rsp)
        cmds = await collect(XmlCommandChannel(), engine, {"Read"})
        assert [c["command_name"] for c in cmds] == ["Read"]

    @pytest.mark.asyncio
    async def test_empty_content_yields_nothing(self):
        assert await collect(XmlCommandChannel(), FakeThinkEngine(content=""), {"Read"}) == []

    @pytest.mark.asyncio
    async def test_none_content_yields_nothing(self):
        engine = FakeThinkEngine(content="", tool_calls=None)
        engine.result.content = None  # simulate a missing-content result
        assert await collect(XmlCommandChannel(), engine, {"Read"}) == []

    @pytest.mark.asyncio
    async def test_plain_text_without_commands_yields_nothing(self):
        # No XML command tags + ignore_text -> parser finds nothing to run.
        engine = FakeThinkEngine(content="just some prose, no commands here")
        assert await collect(XmlCommandChannel(), engine, {"Read"}) == []

    @pytest.mark.asyncio
    async def test_joins_when_not_done(self):
        rsp = xml_command("Read", path="a.py")
        engine = FakeThinkEngine(content=rsp, done=False)
        await collect(XmlCommandChannel(), engine, {"Read"})
        assert engine.join_calls == 1
        assert engine.done is True

    @pytest.mark.asyncio
    async def test_does_not_join_when_done(self):
        engine = FakeThinkEngine(content=xml_command("Read", path="a.py"), done=True)
        await collect(XmlCommandChannel(), engine, {"Read"})
        assert engine.join_calls == 0


class TestRecordTurn:
    def test_records_assistant_and_merged_outputs(self):
        memory = FakeMemory()
        executed = [
            executed_command(name="Read", output="out-1"),
            executed_command(name="Glob", output="out-2"),
        ]
        XmlCommandChannel().record_turn(memory, "<Read>...</Read>", executed)
        # XML records exactly two messages: assistant text + one merged user msg.
        assert len(memory.messages) == 2
        assert memory.messages[0].content == "<Read>...</Read>"
        assert memory.messages[1].content == "out-1\n\nout-2"

    def test_single_output_not_joined(self):
        memory = FakeMemory()
        XmlCommandChannel().record_turn(memory, "rsp", [executed_command(output="solo")])
        assert memory.messages[1].content == "solo"

    def test_no_executed_records_placeholder_user_message(self):
        memory = FakeMemory()
        XmlCommandChannel().record_turn(memory, "rsp", [])
        assert len(memory.messages) == 2
        assert "No valid commands found" in memory.messages[1].content

    def test_assistant_records_raw_command_rsp(self):
        memory = FakeMemory()
        XmlCommandChannel().record_turn(memory, "the raw text", [executed_command(output="x")])
        assert memory.messages[0].content == "the raw text"


class TestRecordTurnMedia:
    def test_appends_media_message(self):
        memory = FakeMemory()
        executed = [executed_command(output="placeholder", images=["IMG"], pdfs=["PDF"])]
        XmlCommandChannel().record_turn(memory, "rsp", executed)
        # assistant + merged-outputs + media.
        assert len(memory.messages) == 3
        media = memory.messages[-1]
        assert media.metadata[IMAGES] == ["IMG"]
        assert media.metadata[PDFS] == ["PDF"]

    def test_no_media_no_extra_message(self):
        memory = FakeMemory()
        XmlCommandChannel().record_turn(memory, "rsp", [executed_command(output="x")])
        assert len(memory.messages) == 2


class TestTerminalDefault:
    def test_is_terminal_default_false(self):
        # XML signals "done" via an End command (handled by the loop), so the
        # channel itself never reports a terminal turn.
        assert XmlCommandChannel().is_terminal(FakeThinkEngine(content="x")) is False

    def test_turn_signature_is_response_text(self):
        engine = FakeThinkEngine(content="the response")
        assert XmlCommandChannel().turn_signature(engine) == "the response"

    def test_turn_signature_empty_when_no_content(self):
        engine = FakeThinkEngine(content="")
        engine.result.content = None
        assert XmlCommandChannel().turn_signature(engine) == ""
