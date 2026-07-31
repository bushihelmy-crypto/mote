#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for :class:`mote.kernel.commands.xml.channel.XmlCommandChannel`.

``iter_commands`` runs the *real* XML lexer (``parse_commands2`` ->
``PythonObjectParser``), so the inputs here are genuine XML command blocks in
the documented OUTPUT_SECTION shape. ``record_turn`` is exercised against the
channel directly; the think round is faked via :class:`FakeThinkEngine`.
"""
from __future__ import annotations

import pytest

from mote.contracts.conversation.fields import IMAGES, PDFS
from mote.kernel.commands.channel import CommandChannel
from mote.kernel.commands.prompts import XML_COMMAND_GUIDE, XML_TOOL_USAGE_GUIDE
from mote.kernel.commands.xml.channel import XmlCommandChannel
from mote.runtime.models.media_projection import build_media_materializer
from mote.runtime.tools.tool_result import ToolMedia
from mote.ztest.artifact_fakes import ArtifactTestResolver, artifact_media

from .conftest import FakeMemory, FakeThinkEngine, apply_projection, collect, executed_command


def xml_command(name: str, **args: str) -> str:
    """Render a single XML command block in the OUTPUT_SECTION shape."""
    body = "".join(f"<{k}>\n{v}\n</{k}>\n" for k, v in args.items())
    return f"<{name}>\n{body}</{name}>"


class TestContract:
    def test_is_command_channel(self):
        assert isinstance(XmlCommandChannel(), CommandChannel)

    def test_structured_output_uses_prompted_json_binding(self):
        from mote.contracts.output import OutputBindingKind

        binding = XmlCommandChannel().output_binding(is_text=False)

        assert binding.kind is OutputBindingKind.PROMPTED_JSON

    def test_structured_output_binding_explains_both_downgrades(self):
        decision = XmlCommandChannel().output_binding_decision(is_text=False)

        assert decision.downgrade_reasons == (
            "native_schema_not_supported",
            "semantic_tool_not_supported",
        )
        assert decision.capabilities.protocol == "xml"

    def test_prompt_vars_command_guide_is_xml_guide_with_end_marker(self):
        guide = XmlCommandChannel().prompt_vars()["command_guide"]
        assert guide == XML_COMMAND_GUIDE
        assert "<end></end>" in guide

    def test_prompt_vars_covers_required_keys(self):
        from mote.kernel.commands.channel import PROMPT_VAR_KEYS

        assert set(XmlCommandChannel().prompt_vars()) >= set(PROMPT_VAR_KEYS)

    def test_prompt_vars_tool_usage_guide_is_static_orientation(self):
        # XML fills ${tool_usage_guide} with the static catalog orientation; the
        # volatile catalog LIST itself rides the per-turn reminder, not here.
        guide = XmlCommandChannel().prompt_vars()["tool_usage_guide"]
        assert guide == XML_TOOL_USAGE_GUIDE
        assert "# Using tools" in guide

    def test_react_result_carries_orchestration_phrasing(self):
        # XML overrides react_result with the <end></end>-era "mark finished"
        # contract, embedding the joined outputs.
        result = XmlCommandChannel().react_result("OUT")
        assert "please mark my task as finished" in result
        assert "OUT" in result


class TestJoinCommandOutputs:
    def test_joins_executed_outputs_with_blank_lines(self):
        from mote.kernel.commands.channel import join_command_outputs

        executed = [executed_command(output="a"), executed_command(output="b")]
        assert join_command_outputs(executed) == "a\n\nb"

    def test_empty_yields_no_commands_notice(self):
        from mote.kernel.commands.channel import NO_VALID_COMMANDS, join_command_outputs

        assert join_command_outputs([]) == NO_VALID_COMMANDS

    def test_lower_renders_ctl_finish_as_end_marker(self):
        # Under XML, the CTL_FINISH symbol materializes the <end></end> mechanic.
        from mote.kernel.commands.symbols import CTL_FINISH

        out = XmlCommandChannel().lower(f"Only {CTL_FINISH} when done.")
        assert "<end></end>" in out

    def test_lower_renders_capability_symbols_as_dotted_names(self):
        from mote.kernel.commands.symbols import CAP_READ

        out = XmlCommandChannel().lower(f"Use {CAP_READ} first.")
        assert "Editor.read" in out

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
        rsp = "think\n" + xml_command("Read", path="a.py") + "\nthink more\n" + xml_command("Glob", pattern="*.py")
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
        assert engine.join_calls == 0
        assert engine.done is False

    @pytest.mark.asyncio
    async def test_does_not_join_when_done(self):
        engine = FakeThinkEngine(content=xml_command("Read", path="a.py"), done=True)
        await collect(XmlCommandChannel(), engine, {"Read"})
        assert engine.join_calls == 0


class TestRecordTurn:
    @pytest.mark.asyncio
    async def test_records_assistant_and_merged_outputs(self):
        memory = FakeMemory()
        executed = [
            executed_command(name="Read", output="out-1"),
            executed_command(name="Glob", output="out-2"),
        ]
        await apply_projection(memory, XmlCommandChannel().project_turn("<Read>...</Read>", executed))
        # XML records exactly two messages: assistant text + one merged user msg.
        assert len(memory.messages) == 2
        assert memory.messages[0].content == "<Read>...</Read>"
        assert memory.messages[1].content == "out-1\n\nout-2"

    @pytest.mark.asyncio
    async def test_single_output_not_joined(self):
        memory = FakeMemory()
        await apply_projection(memory, XmlCommandChannel().project_turn("rsp", [executed_command(output="solo")]))
        assert memory.messages[1].content == "solo"

    @pytest.mark.asyncio
    async def test_no_executed_records_placeholder_user_message(self):
        memory = FakeMemory()
        await apply_projection(memory, XmlCommandChannel().project_turn("rsp", []))
        assert len(memory.messages) == 2
        assert "No valid commands found" in memory.messages[1].content

    @pytest.mark.asyncio
    async def test_assistant_records_raw_command_rsp(self):
        memory = FakeMemory()
        await apply_projection(memory, XmlCommandChannel().project_turn("the raw text", [executed_command(output="x")]))
        assert memory.messages[0].content == "the raw text"


class TestRecordTurnMedia:
    @pytest.mark.asyncio
    async def test_appends_media_message(self):
        memory = FakeMemory()
        executed = [
            executed_command(
                output="placeholder",
                media=[
                    artifact_media("image", "IMG"),
                    artifact_media("pdf", "PDF"),
                ],
            )
        ]
        channel = XmlCommandChannel(media_materializer=build_media_materializer(ArtifactTestResolver()))
        await apply_projection(memory, channel.project_turn("rsp", executed))
        # assistant + merged-outputs + media.
        assert len(memory.messages) == 3
        media = memory.messages[-1]
        assert media.metadata[IMAGES] == ["SU1H"]
        assert media.metadata[PDFS] == ["UERG"]

    @pytest.mark.asyncio
    async def test_no_media_no_extra_message(self):
        memory = FakeMemory()
        await apply_projection(memory, XmlCommandChannel().project_turn("rsp", [executed_command(output="x")]))
        assert len(memory.messages) == 2


class TestTerminalDefault:
    @pytest.mark.asyncio
    async def test_xml_text_is_not_a_final_candidate(self):
        # Plain XML response text is not completion without the protocol marker.
        turn = await XmlCommandChannel().model_turn(FakeThinkEngine(content="x").result)
        assert not turn.final_candidates
        assert turn.actions[0].kind == "text"

    @pytest.mark.asyncio
    async def test_end_marker_becomes_semantic_final_candidate(self):
        turn = await XmlCommandChannel().model_turn(FakeThinkEngine(content="final answer\n<end></end>").result)

        assert len(turn.final_candidates) == 1
        assert turn.final_candidates[0].raw == "final answer"
        assert turn.final_candidates[0].representation == "xml_end"
        assert all(action.kind != "tool_call" for action in turn.actions)

    @pytest.mark.asyncio
    async def test_xml_command_is_normalized_without_channel_tool_filtering(self):
        turn = await XmlCommandChannel().model_turn(FakeThinkEngine(content=xml_command("Read", path="a.py")).result)

        calls = [action for action in turn.actions if action.kind == "tool_call"]
        assert len(calls) == 1
        assert calls[0].name == "Read"
        assert calls[0].arguments == {"path": "a.py"}

    @pytest.mark.asyncio
    async def test_end_marker_with_tool_call_is_preserved_for_policy_rejection(self):
        turn = await XmlCommandChannel().model_turn(
            FakeThinkEngine(content=xml_command("Read", path="a.py") + "\n<end></end>").result
        )

        assert len(turn.final_candidates) == 1
        assert any(action.kind == "tool_call" for action in turn.actions)

    def test_turn_signature_is_response_text(self):
        engine = FakeThinkEngine(content="the response")
        assert XmlCommandChannel().turn_signature(engine.result) == "the response"

    def test_turn_signature_empty_when_no_content(self):
        engine = FakeThinkEngine(content="")
        engine.result.content = None
        assert XmlCommandChannel().turn_signature(engine.result) == ""
