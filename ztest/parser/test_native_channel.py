#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for :class:`metagpt.parser.native_channel.NativeToolChannel`.

Covers the five protocol hooks (output_format / tool_specs / iter_commands /
record_turn / turn_signature / is_terminal) plus the contract that this channel
is a :class:`CommandChannel`. The think round is faked via
:class:`FakeThinkEngine` (a real :class:`ThinkResult` behind ``done`` /
``join``), so no LLM is involved.
"""
from __future__ import annotations

import json

import pytest

from metagpt.common.base import CommandChannel
from metagpt.common.const import IMAGES, PDFS, TOOL_CALL_ID, TOOL_CALLS
from metagpt.parser.native_channel import NativeToolChannel

from .conftest import FakeExecutor, FakeMemory, FakeThinkEngine, collect, executed_command


def native_call(id="1", command_name="Read", args=None) -> dict:
    """Build a think-result tool-call entry (the unified IR shape)."""
    return {"id": id, "command_name": command_name, "args": args or {}}


class TestContract:
    def test_is_command_channel(self):
        assert isinstance(NativeToolChannel(), CommandChannel)

    def test_default_provider_is_openai(self):
        assert NativeToolChannel()._provider == "openai"

    def test_provider_is_stored(self):
        assert NativeToolChannel(provider="anthropic")._provider == "anthropic"

    def test_output_format_is_empty(self):
        # Native channel injects no OUTPUT prompt section.
        assert NativeToolChannel().output_format() == ""


class TestToolSpecs:
    def test_delegates_to_executor_with_provider(self):
        executor = FakeExecutor(specs=[{"name": "Read"}])
        channel = NativeToolChannel(provider="anthropic")
        specs = channel.tool_specs(executor)
        assert specs == [{"name": "Read"}]
        assert executor.provider_calls == ["anthropic"]

    def test_passes_default_provider(self):
        executor = FakeExecutor()
        NativeToolChannel().tool_specs(executor)
        assert executor.provider_calls == ["openai"]


class TestIterCommands:
    @pytest.mark.asyncio
    async def test_maps_id_name_args(self):
        engine = FakeThinkEngine(tool_calls=[native_call("call-1", "Read", {"path": "a.py"})])
        cmds = await collect(NativeToolChannel(), engine, set())
        assert cmds == [
            {
                "id": "call-1",
                "command_name": "Read",
                "args": {"path": "a.py"},
                "status": "running",
                "error_msg": "",
            }
        ]

    @pytest.mark.asyncio
    async def test_yields_multiple_in_order(self):
        engine = FakeThinkEngine(
            tool_calls=[native_call("1", "Read"), native_call("2", "Glob")]
        )
        cmds = await collect(NativeToolChannel(), engine, set())
        assert [c["command_name"] for c in cmds] == ["Read", "Glob"]

    @pytest.mark.asyncio
    async def test_none_tool_calls_yields_nothing(self):
        # XML-style result (tool_calls is None) -> the "or []" guard yields nothing.
        engine = FakeThinkEngine(content="text", tool_calls=None)
        assert await collect(NativeToolChannel(), engine, set()) == []

    @pytest.mark.asyncio
    async def test_empty_tool_calls_yields_nothing(self):
        engine = FakeThinkEngine(tool_calls=[])
        assert await collect(NativeToolChannel(), engine, set()) == []

    @pytest.mark.asyncio
    async def test_missing_id_and_args_default(self):
        # cmd has only command_name -> id None, args {}.
        engine = FakeThinkEngine(tool_calls=[{"command_name": "Glob"}])
        cmds = await collect(NativeToolChannel(), engine, set())
        assert cmds == [
            {"id": None, "command_name": "Glob", "args": {}, "status": "running", "error_msg": ""}
        ]

    @pytest.mark.asyncio
    async def test_null_args_normalized_to_empty_dict(self):
        engine = FakeThinkEngine(tool_calls=[{"id": "1", "command_name": "X", "args": None}])
        cmds = await collect(NativeToolChannel(), engine, set())
        assert cmds[0]["args"] == {}

    @pytest.mark.asyncio
    async def test_empty_valid_names_does_not_filter(self):
        # An empty set is falsy -> the filter is skipped, everything passes.
        engine = FakeThinkEngine(tool_calls=[native_call("1", "Anything")])
        cmds = await collect(NativeToolChannel(), engine, set())
        assert [c["command_name"] for c in cmds] == ["Anything"]

    @pytest.mark.asyncio
    async def test_unknown_name_filtered_out(self):
        engine = FakeThinkEngine(
            tool_calls=[native_call("1", "Read"), native_call("2", "Nope")]
        )
        cmds = await collect(NativeToolChannel(), engine, {"Read"})
        assert [c["command_name"] for c in cmds] == ["Read"]

    @pytest.mark.asyncio
    async def test_all_known_names_pass(self):
        engine = FakeThinkEngine(
            tool_calls=[native_call("1", "Read"), native_call("2", "Glob")]
        )
        cmds = await collect(NativeToolChannel(), engine, {"Read", "Glob"})
        assert [c["command_name"] for c in cmds] == ["Read", "Glob"]

    @pytest.mark.asyncio
    async def test_joins_when_not_done(self):
        engine = FakeThinkEngine(tool_calls=[native_call("1", "Read")], done=False)
        await collect(NativeToolChannel(), engine, set())
        assert engine.join_calls == 1
        assert engine.done is True

    @pytest.mark.asyncio
    async def test_does_not_join_when_done(self):
        engine = FakeThinkEngine(tool_calls=[native_call("1", "Read")], done=True)
        await collect(NativeToolChannel(), engine, set())
        assert engine.join_calls == 0


class TestRecordTurn:
    def test_records_assistant_then_tool_results(self):
        memory = FakeMemory()
        executed = [
            executed_command(id="a", name="Read", args={"path": "x"}, output="content-x"),
            executed_command(id="b", name="Glob", args={"pattern": "*.py"}, output="content-y"),
        ]
        NativeToolChannel().record_turn(memory, "I will read and glob", executed)

        # 1 assistant + 2 tool-result messages, in order.
        assert len(memory.messages) == 3
        assistant = memory.messages[0]
        assert assistant.content == "I will read and glob"
        assert assistant.metadata[TOOL_CALLS] == [
            {"id": "a", "name": "Read", "args": {"path": "x"}},
            {"id": "b", "name": "Glob", "args": {"pattern": "*.py"}},
        ]
        first_result, second_result = memory.messages[1], memory.messages[2]
        assert first_result.content == "content-x"
        assert first_result.metadata[TOOL_CALL_ID] == "a"
        assert second_result.content == "content-y"
        assert second_result.metadata[TOOL_CALL_ID] == "b"

    def test_empty_command_rsp_becomes_empty_string(self):
        memory = FakeMemory()
        NativeToolChannel().record_turn(memory, "", [executed_command(id="a")])
        assert memory.messages[0].content == ""

    def test_none_command_rsp_becomes_empty_string(self):
        memory = FakeMemory()
        NativeToolChannel().record_turn(memory, None, [executed_command(id="a")])
        assert memory.messages[0].content == ""

    def test_executed_without_id_skipped_everywhere(self):
        # Commands lacking an id can't be paired -> excluded from tool_calls and
        # produce no tool-result message.
        memory = FakeMemory()
        executed = [executed_command(id=None, name="ghost", output="ignored")]
        NativeToolChannel().record_turn(memory, "text", executed)
        assert len(memory.messages) == 1  # only the assistant message
        assert memory.messages[0].metadata[TOOL_CALLS] == []

    def test_mixed_id_and_no_id(self):
        memory = FakeMemory()
        executed = [
            executed_command(id="a", name="Read", output="r"),
            executed_command(id=None, name="ghost", output="x"),
        ]
        NativeToolChannel().record_turn(memory, "t", executed)
        # assistant + one tool-result (for the id'd one only).
        assert len(memory.messages) == 2
        assert [c["id"] for c in memory.messages[0].metadata[TOOL_CALLS]] == ["a"]
        assert memory.messages[1].metadata[TOOL_CALL_ID] == "a"

    def test_no_executed_records_only_assistant(self):
        memory = FakeMemory()
        NativeToolChannel().record_turn(memory, "just text", [])
        assert len(memory.messages) == 1
        assert memory.messages[0].metadata[TOOL_CALLS] == []

    def test_args_default_to_empty_dict_in_tool_calls(self):
        memory = FakeMemory()
        executed = [{"id": "a", "name": "Read", "output": "r"}]  # no args key
        NativeToolChannel().record_turn(memory, "t", executed)
        assert memory.messages[0].metadata[TOOL_CALLS][0]["args"] == {}


class TestRecordTurnMedia:
    def test_appends_media_message_with_images(self):
        memory = FakeMemory()
        executed = [executed_command(id="a", output="placeholder", images=["IMGDATA"])]
        NativeToolChannel().record_turn(memory, "t", executed)
        # assistant + tool-result + media message.
        assert len(memory.messages) == 3
        media = memory.messages[-1]
        assert media.metadata[IMAGES] == ["IMGDATA"]
        assert PDFS not in media.metadata

    def test_appends_media_message_with_pdfs(self):
        memory = FakeMemory()
        executed = [executed_command(id="a", output="placeholder", pdfs=["PDFDATA"])]
        NativeToolChannel().record_turn(memory, "t", executed)
        media = memory.messages[-1]
        assert media.metadata[PDFS] == ["PDFDATA"]
        assert IMAGES not in media.metadata

    def test_collects_media_across_commands(self):
        memory = FakeMemory()
        executed = [
            executed_command(id="a", images=["i1"], pdfs=["p1"]),
            executed_command(id="b", images=["i2"]),
        ]
        NativeToolChannel().record_turn(memory, "t", executed)
        media = memory.messages[-1]
        assert media.metadata[IMAGES] == ["i1", "i2"]
        assert media.metadata[PDFS] == ["p1"]

    def test_no_media_means_no_extra_message(self):
        memory = FakeMemory()
        NativeToolChannel().record_turn(memory, "t", [executed_command(id="a")])
        # assistant + tool-result, no media message.
        assert len(memory.messages) == 2

    def test_media_from_idless_command_still_collected(self):
        # Media collection is independent of pairing; an id-less command's media
        # is still gathered (the placeholder text was lost but bytes survive).
        memory = FakeMemory()
        executed = [executed_command(id=None, images=["only"])]
        NativeToolChannel().record_turn(memory, "t", executed)
        # assistant (no tool-result since no id) + media.
        assert len(memory.messages) == 2
        assert memory.messages[-1].metadata[IMAGES] == ["only"]


class TestTurnSignature:
    def test_signature_is_sorted_json_of_calls(self):
        engine = FakeThinkEngine(
            tool_calls=[native_call("1", "Read", {"b": 2, "a": 1})]
        )
        sig = NativeToolChannel().turn_signature(engine)
        assert json.loads(sig) == [{"name": "Read", "args": {"b": 2, "a": 1}}]
        # sort_keys -> "a" before "b" in the serialized args.
        assert sig.index('"a"') < sig.index('"b"')

    def test_signature_omits_id(self):
        engine = FakeThinkEngine(tool_calls=[native_call("xyz", "Read", {})])
        assert "xyz" not in NativeToolChannel().turn_signature(engine)

    def test_signature_stable_regardless_of_id(self):
        a = FakeThinkEngine(tool_calls=[native_call("1", "Read", {"p": "x"})])
        b = FakeThinkEngine(tool_calls=[native_call("999", "Read", {"p": "x"})])
        ch = NativeToolChannel()
        assert ch.turn_signature(a) == ch.turn_signature(b)

    def test_signature_empty_calls(self):
        assert NativeToolChannel().turn_signature(FakeThinkEngine(tool_calls=[])) == "[]"

    def test_signature_none_calls_treated_as_empty(self):
        assert NativeToolChannel().turn_signature(FakeThinkEngine(tool_calls=None)) == "[]"

    def test_signature_preserves_unicode(self):
        engine = FakeThinkEngine(tool_calls=[native_call("1", "Read", {"q": "你好"})])
        # ensure_ascii=False -> raw unicode, not \uXXXX escapes.
        assert "你好" in NativeToolChannel().turn_signature(engine)


class TestIsTerminal:
    def test_terminal_when_empty_calls(self):
        # Native "done": the model replied with no tool calls.
        assert NativeToolChannel().is_terminal(FakeThinkEngine(tool_calls=[])) is True

    def test_not_terminal_with_calls(self):
        engine = FakeThinkEngine(tool_calls=[native_call("1", "Read")])
        assert NativeToolChannel().is_terminal(engine) is False

    def test_none_calls_not_terminal(self):
        # tool_calls is None (XML-style) -> not the native terminal condition.
        assert NativeToolChannel().is_terminal(FakeThinkEngine(tool_calls=None)) is False
