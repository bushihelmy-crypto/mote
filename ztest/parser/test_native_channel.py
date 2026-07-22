#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for :class:`mote.parser.native_channel.NativeToolChannel`.

Covers the protocol hooks (prompt_vars / tool_specs / iter_commands /
record_turn / turn_signature / is_terminal) plus the contract that this channel
is a :class:`CommandChannel`. The think round is faked via
:class:`FakeThinkEngine` (a real :class:`ThinkResult` behind ``done`` /
``join``), so no LLM is involved.
"""
from __future__ import annotations

import json

import pytest

from mote.common.base import CommandChannel
from mote.common.const import IMAGES, PDFS, TOOL_CALL_ID, TOOL_CALLS, TOOL_REFERENCES, TOOL_RESULT_RESOURCE_PATH
from mote.parser.native_channel import FINAL_OUTPUT_TOOL_NAME, NativeToolChannel

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

    def test_structured_output_negotiation_reports_native_schema_downgrade(self):
        from mote.common.schema import OutputBindingKind

        decision = NativeToolChannel().output_binding_decision(is_text=False)

        assert decision.binding.kind is OutputBindingKind.NATIVE_TOOL
        assert decision.downgrade_reasons == ("native_schema_not_supported",)
        assert decision.capabilities.protocol == "native"
        assert decision.capabilities.provider == "openai"

    def test_for_llm_profiles_the_actual_routed_transport(self):
        from types import SimpleNamespace

        from mote.common.config.config.llm_config import LLMConfig

        llm = SimpleNamespace(
            config=LLMConfig(
                model="claude-sonnet-4",
                api_type="anthropic",
                base_url="https://api.anthropic.com",
            ),
            model="claude-sonnet-4",
        )

        routed = NativeToolChannel(provider="openai", model="gpt-4o").for_llm(llm)
        capabilities = routed.output_capabilities()

        assert capabilities.provider == "anthropic"
        assert capabilities.model == "claude-sonnet-4"

    def test_schema_incompatible_with_provider_downgrades_to_semantic_tool(self):
        from types import SimpleNamespace

        def reject_schema(_schema):
            raise ValueError("unsupported schema")

        llm = SimpleNamespace(
            config=SimpleNamespace(model="gpt-4o", api_type="openai"),
            model="gpt-4o",
            supports_native_schema_output=lambda: True,
            native_schema_request=reject_schema,
        )

        routed = NativeToolChannel(output_is_text=False).for_llm(
            llm,
            output_schema={
                "type": "object",
                "additionalProperties": {"type": "integer"},
            },
        )
        decision = routed.output_binding_decision(is_text=False)

        assert decision.binding.kind.value == "native_tool"
        assert decision.downgrade_reasons == ("native_schema_not_supported",)

    def test_prompt_vars_command_guide_is_empty(self):
        # Native tools reach the model via the API ``tools=`` param and a turn
        # ends simply by making no tool call, so the system prompt needs no
        # "# Using commands" mechanics at all — command_guide is filled empty
        # (not a literal placeholder), never teaching the XML <end></end> marker.
        guide = NativeToolChannel().prompt_vars()["command_guide"]
        assert guide == ""

    def test_prompt_vars_covers_required_keys(self):
        from mote.common.base.command_channel import PROMPT_VAR_KEYS

        assert set(NativeToolChannel().prompt_vars()) >= set(PROMPT_VAR_KEYS)

    def test_prompt_vars_tool_usage_guide_is_empty(self):
        # Native tools reach the model via the API ``tools=`` param, so the
        # system prompt needs no catalog orientation (mirrors wants_tool_catalog
        # False). ${tool_usage_guide} is filled empty, not a literal placeholder.
        assert NativeToolChannel().prompt_vars()["tool_usage_guide"] == ""

    def test_react_result_is_plain_outputs(self):
        # Native finishes via a plain-text reply (_finish), so the published
        # react result keeps the outputs verbatim — no XML orchestration phrasing.
        assert NativeToolChannel().react_result("OUT") == "OUT"

    def test_lower_renders_ctl_finish_without_end_marker(self):
        # The CTL_FINISH symbol must lower to a plain-English turn-end, never the
        # XML <end></end> marker, so symbolized prose can't leak it to native.
        from mote.common.prompt.refs import CTL_FINISH

        out = NativeToolChannel().lower(f"Only {CTL_FINISH} when done.")
        assert "<end>" not in out
        assert "tool" in out.lower()

    def test_lower_renders_capability_symbols_as_plain_text(self):
        from mote.common.prompt.refs import CAP_READ

        out = NativeToolChannel().lower(f"Use {CAP_READ} first.")
        assert "Editor.read" not in out
        assert "read tool" in out


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

    def test_structured_contract_adds_provider_native_final_output_tool(self):
        from pydantic import BaseModel

        from mote.common.schema import OutputContractId
        from mote.roles.output_contract import OutputContract, TypeAdapterOutputDecoder

        class Report(BaseModel):
            count: int

        contract = OutputContract(
            OutputContractId("test", "report", "1"),
            TypeAdapterOutputDecoder(Report),
        )
        specs = NativeToolChannel(provider="openai").tool_specs(FakeExecutor(specs=[]), contract)

        final = specs[-1]["function"]
        assert final["name"] == FINAL_OUTPUT_TOOL_NAME
        output_schema = final["parameters"]["properties"]["output"]
        assert output_schema["properties"]["count"]["type"] == "integer"


class TestModelTurn:
    @pytest.mark.asyncio
    async def test_native_schema_content_lowers_to_final_candidate(self):
        channel = NativeToolChannel(output_is_text=False, supports_native_schema=True)

        turn = await channel.model_turn(FakeThinkEngine(content='{"count": 7}', tool_calls=[]))

        assert turn.final_candidates[0].raw == '{"count": 7}'
        assert turn.final_candidates[0].representation == "native_schema"

    @pytest.mark.asyncio
    async def test_final_output_wire_tool_becomes_candidate(self):
        engine = FakeThinkEngine(
            tool_calls=[
                native_call(
                    "candidate-1",
                    FINAL_OUTPUT_TOOL_NAME,
                    {"output": {"count": 7}},
                )
            ]
        )

        turn = await NativeToolChannel(output_is_text=False).model_turn(engine)

        assert len(turn.final_candidates) == 1
        assert turn.final_candidates[0].candidate_id == "candidate-1"
        assert turn.final_candidates[0].raw == {"count": 7}
        assert all(action.kind != "tool_call" for action in turn.actions)

    @pytest.mark.asyncio
    async def test_plain_text_is_not_completion_for_structured_contract(self):
        engine = FakeThinkEngine(content='{"count": 7}', tool_calls=[])

        turn = await NativeToolChannel(output_is_text=False).model_turn(engine)

        assert turn.final_candidates == []
        assert turn.actions[0].kind == "text"

    @pytest.mark.asyncio
    async def test_rejected_output_tool_records_paired_correction_result(self):
        from mote.common.schema import CorrectionFeedback, FinalCandidateAction, ValidationIssue

        memory = FakeMemory()
        candidate = FinalCandidateAction(
            candidate_id="candidate-1",
            raw={"count": "bad"},
            representation="native_output_tool",
        )
        feedback = CorrectionFeedback(
            summary="Correct the output.",
            issues=(ValidationIssue(("count",), "int_parsing", "Expected integer"),),
        )

        await NativeToolChannel().record_output_candidate(memory, "", candidate, accepted=False, feedback=feedback)

        assert memory.messages[0].metadata[TOOL_CALLS][0]["name"] == FINAL_OUTPUT_TOOL_NAME
        assert memory.messages[1].metadata[TOOL_CALL_ID] == "candidate-1"
        assert "count [int_parsing]" in memory.messages[1].content


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
        engine = FakeThinkEngine(tool_calls=[native_call("1", "Read"), native_call("2", "Glob")])
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
            {
                "id": None,
                "command_name": "Glob",
                "args": {},
                "status": "running",
                "error_msg": "",
            }
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
        engine = FakeThinkEngine(tool_calls=[native_call("1", "Read"), native_call("2", "Nope")])
        cmds = await collect(NativeToolChannel(), engine, {"Read"})
        assert [c["command_name"] for c in cmds] == ["Read"]

    @pytest.mark.asyncio
    async def test_all_known_names_pass(self):
        engine = FakeThinkEngine(tool_calls=[native_call("1", "Read"), native_call("2", "Glob")])
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
    @pytest.mark.asyncio
    async def test_records_assistant_then_tool_results(self):
        memory = FakeMemory()
        executed = [
            executed_command(id="a", name="Read", args={"path": "x"}, output="content-x"),
            executed_command(id="b", name="Glob", args={"pattern": "*.py"}, output="content-y"),
        ]
        await NativeToolChannel().record_turn(memory, "I will read and glob", executed)

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

    @pytest.mark.asyncio
    async def test_empty_command_rsp_becomes_empty_string(self):
        memory = FakeMemory()
        await NativeToolChannel().record_turn(memory, "", [executed_command(id="a")])
        assert memory.messages[0].content == ""

    @pytest.mark.asyncio
    async def test_none_command_rsp_becomes_empty_string(self):
        memory = FakeMemory()
        await NativeToolChannel().record_turn(memory, None, [executed_command(id="a")])
        assert memory.messages[0].content == ""

    @pytest.mark.asyncio
    async def test_executed_without_id_skipped_everywhere(self):
        # Commands lacking an id can't be paired -> excluded from tool_calls and
        # produce no tool-result message.
        memory = FakeMemory()
        executed = [executed_command(id=None, name="ghost", output="ignored")]
        await NativeToolChannel().record_turn(memory, "text", executed)
        assert len(memory.messages) == 1  # only the assistant message
        assert memory.messages[0].metadata[TOOL_CALLS] == []

    @pytest.mark.asyncio
    async def test_mixed_id_and_no_id(self):
        memory = FakeMemory()
        executed = [
            executed_command(id="a", name="Read", output="r"),
            executed_command(id=None, name="ghost", output="x"),
        ]
        await NativeToolChannel().record_turn(memory, "t", executed)
        # assistant + one tool-result (for the id'd one only).
        assert len(memory.messages) == 2
        assert [c["id"] for c in memory.messages[0].metadata[TOOL_CALLS]] == ["a"]
        assert memory.messages[1].metadata[TOOL_CALL_ID] == "a"

    @pytest.mark.asyncio
    async def test_no_executed_records_only_assistant(self):
        memory = FakeMemory()
        await NativeToolChannel().record_turn(memory, "just text", [])
        assert len(memory.messages) == 1
        assert memory.messages[0].metadata[TOOL_CALLS] == []

    @pytest.mark.asyncio
    async def test_args_default_to_empty_dict_in_tool_calls(self):
        memory = FakeMemory()
        executed = [{"id": "a", "name": "Read", "output": "r"}]  # no args key
        await NativeToolChannel().record_turn(memory, "t", executed)
        assert memory.messages[0].metadata[TOOL_CALLS][0]["args"] == {}

    @pytest.mark.asyncio
    async def test_resource_path_stamped_onto_tool_result_metadata(self):
        # A reconstructable read carries resource_path -> the channel stamps it
        # onto the tool_result message metadata for ContextVisibility to key off.
        memory = FakeMemory()
        executed = [executed_command(id="a", name="Read", output="content", resource_path="/f/a.txt")]
        await NativeToolChannel().record_turn(memory, "t", executed)
        assert memory.messages[1].metadata[TOOL_RESULT_RESOURCE_PATH] == "/f/a.txt"

    @pytest.mark.asyncio
    async def test_no_resource_path_leaves_metadata_unset(self):
        # A result without resource_path (e.g. a dedup stub) must stay untagged,
        # so it never registers as a file's latest visible read.
        memory = FakeMemory()
        executed = [executed_command(id="a", name="Read", output="unchanged")]
        await NativeToolChannel().record_turn(memory, "t", executed)
        assert TOOL_RESULT_RESOURCE_PATH not in memory.messages[1].metadata


class TestRecordArgsLimiter:
    """record_call runs recorded args through the injected size limiter.

    The limiter (``executor.persist_large_args`` in production) persists a giant
    tool-call arg before the assistant message enters memory — the arguments twin
    of the result-output cap. Here a fake limiter proves the seam: it is applied
    to the args that land in ``tool_calls`` metadata, uses the call id, does NOT
    mutate the caller's ``executed`` args, and is a no-op when unset.
    """

    @pytest.mark.asyncio
    async def test_limiter_applied_to_recorded_args(self):
        seen: list = []

        def limiter(tool_name, args, call_id):
            seen.append((tool_name, args, call_id))
            return "<persisted-output>...pointer..."

        memory = FakeMemory()
        channel = NativeToolChannel(args_limiter=limiter)
        executed = [executed_command(id="a", name="Edit", args={"new_string": "BIG"}, output="ok")]
        await channel.record_turn(memory, "t", executed)

        # The recorded args are the limiter's return (the envelope string),
        # which AIMessage.to_dict accepts verbatim as the arguments string.
        assert memory.messages[0].metadata[TOOL_CALLS][0]["args"] == "<persisted-output>...pointer..."
        # Called with the tool name (lets it specialize per tool), the original
        # args, + the call id (id names the on-disk file).
        assert seen == [("Edit", {"new_string": "BIG"}, "a")]

    @pytest.mark.asyncio
    async def test_limiter_does_not_mutate_execution_args(self):
        # record_call copies args into a NEW list, so the caller's executed entry
        # (the value the loop passes to run_command) is never rewritten.
        memory = FakeMemory()
        channel = NativeToolChannel(args_limiter=lambda name, args, cid: "SPILLED")
        entry = executed_command(id="a", name="Edit", args={"new_string": "BIG"}, output="ok")
        executed = [entry]
        await channel.record_turn(memory, "t", executed)
        assert entry["args"] == {"new_string": "BIG"}

    @pytest.mark.asyncio
    async def test_small_args_returned_unchanged_by_identity_limiter(self):
        # A limiter that leaves small args alone (the real one returns the
        # original object under threshold) records them verbatim.
        memory = FakeMemory()
        channel = NativeToolChannel(args_limiter=lambda name, args, cid: args)
        executed = [executed_command(id="a", name="Read", args={"path": "x"}, output="r")]
        await channel.record_turn(memory, "t", executed)
        assert memory.messages[0].metadata[TOOL_CALLS][0]["args"] == {"path": "x"}

    @pytest.mark.asyncio
    async def test_no_limiter_records_args_verbatim(self):
        # Default (no executor wired / tests): args pass through untouched.
        memory = FakeMemory()
        executed = [executed_command(id="a", name="Read", args={"path": "x"}, output="r")]
        await NativeToolChannel().record_turn(memory, "t", executed)
        assert memory.messages[0].metadata[TOOL_CALLS][0]["args"] == {"path": "x"}


class TestRecordTurnMedia:
    @pytest.mark.asyncio
    async def test_appends_media_message_with_images(self):
        memory = FakeMemory()
        executed = [executed_command(id="a", output="placeholder", images=["IMGDATA"])]
        await NativeToolChannel().record_turn(memory, "t", executed)
        # assistant + tool-result + media message.
        assert len(memory.messages) == 3
        media = memory.messages[-1]
        assert media.metadata[IMAGES] == ["IMGDATA"]
        assert PDFS not in media.metadata

    @pytest.mark.asyncio
    async def test_appends_media_message_with_pdfs(self):
        memory = FakeMemory()
        executed = [executed_command(id="a", output="placeholder", pdfs=["PDFDATA"])]
        await NativeToolChannel().record_turn(memory, "t", executed)
        media = memory.messages[-1]
        assert media.metadata[PDFS] == ["PDFDATA"]
        assert IMAGES not in media.metadata

    @pytest.mark.asyncio
    async def test_collects_media_across_commands(self):
        memory = FakeMemory()
        executed = [
            executed_command(id="a", images=["i1"], pdfs=["p1"]),
            executed_command(id="b", images=["i2"]),
        ]
        await NativeToolChannel().record_turn(memory, "t", executed)
        media = memory.messages[-1]
        assert media.metadata[IMAGES] == ["i1", "i2"]
        assert media.metadata[PDFS] == ["p1"]

    @pytest.mark.asyncio
    async def test_no_media_means_no_extra_message(self):
        memory = FakeMemory()
        await NativeToolChannel().record_turn(memory, "t", [executed_command(id="a")])
        # assistant + tool-result, no media message.
        assert len(memory.messages) == 2

    @pytest.mark.asyncio
    async def test_media_from_idless_command_still_collected(self):
        # Media collection is independent of pairing; an id-less command's media
        # is still gathered (the placeholder text was lost but bytes survive).
        memory = FakeMemory()
        executed = [executed_command(id=None, images=["only"])]
        await NativeToolChannel().record_turn(memory, "t", executed)
        # assistant (no tool-result since no id) + media.
        assert len(memory.messages) == 2
        assert memory.messages[-1].metadata[IMAGES] == ["only"]


class TestToolReferencesGating:
    """The SearchTools discovery seam is CAPABILITY-gated on the record side.

    A SearchTools result carries ``data={"tool_references": [...]}``. That is
    stamped onto the recorded ToolMessage (so the wire renders it as
    ``tool_reference`` / ``tool_search`` blocks the API expands) ONLY on a
    transport that actually does server-side tool search — i.e. a capable model
    on the anthropic / openai_responses envelope. An INCAPABLE native model runs
    the client-side SPLIT path and CANNOT expand those blocks, so the stamp must
    be suppressed there (it discovers via RoleState + the reminder-tail menu).
    This keeps the record side aligned with ``native_specs`` byte-for-byte.
    """

    def _search_result(self, refs):
        return {
            "id": "s1",
            "name": "SearchTools",
            "args": {"query": "img"},
            "output": "revealed: ConvertImage",
            "success": True,
            "data": {"tool_references": refs},
        }

    @pytest.mark.asyncio
    async def test_capable_anthropic_stamps_references(self):
        memory = FakeMemory()
        channel = NativeToolChannel(provider="anthropic", model="opus-4")
        await channel.record_results(memory, [self._search_result(["ConvertImage"])])
        assert memory.messages[0].metadata[TOOL_REFERENCES] == ["ConvertImage"]

    @pytest.mark.asyncio
    async def test_capable_openai_responses_stamps_references(self):
        memory = FakeMemory()
        channel = NativeToolChannel(provider="openai_responses", model="gpt-5.4")
        await channel.record_results(memory, [self._search_result(["ConvertImage"])])
        assert memory.messages[0].metadata[TOOL_REFERENCES] == ["ConvertImage"]

    @pytest.mark.asyncio
    async def test_old_anthropic_suppresses_references(self):
        # Old Claude runs SPLIT — cannot expand tool_reference blocks, so no stamp.
        memory = FakeMemory()
        channel = NativeToolChannel(provider="anthropic", model="claude-3-5-sonnet")
        await channel.record_results(memory, [self._search_result(["ConvertImage"])])
        assert TOOL_REFERENCES not in memory.messages[0].metadata

    @pytest.mark.asyncio
    async def test_openai_chat_completions_suppresses_references(self):
        # The Chat Completions "openai" envelope has no server-side path even on a
        # capable model, so the stamp is suppressed (SPLIT).
        memory = FakeMemory()
        channel = NativeToolChannel(provider="openai", model="gpt-5.4")
        await channel.record_results(memory, [self._search_result(["ConvertImage"])])
        assert TOOL_REFERENCES not in memory.messages[0].metadata

    @pytest.mark.asyncio
    async def test_no_model_suppresses_references(self):
        # Capability unknown → defensive SPLIT, no stamp.
        memory = FakeMemory()
        channel = NativeToolChannel(provider="anthropic")
        await channel.record_results(memory, [self._search_result(["ConvertImage"])])
        assert TOOL_REFERENCES not in memory.messages[0].metadata

    @pytest.mark.asyncio
    async def test_non_search_data_ignored_on_capable(self):
        # Only the tool_references key is read; any other data shape → no stamp.
        memory = FakeMemory()
        channel = NativeToolChannel(provider="anthropic", model="opus-4")
        entry = executed_command(id="a", name="Read", output="content")
        entry["data"] = {"something_else": 1}
        await channel.record_results(memory, [entry])
        assert TOOL_REFERENCES not in memory.messages[0].metadata


class TestTurnSignature:
    def test_signature_is_sorted_json_of_calls(self):
        engine = FakeThinkEngine(tool_calls=[native_call("1", "Read", {"b": 2, "a": 1})])
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


class TestModelTurn:
    @pytest.mark.asyncio
    async def test_terminal_when_empty_calls(self):
        # Native "done": the model replied with no tool calls.
        turn = await NativeToolChannel().model_turn(FakeThinkEngine(content="done", tool_calls=[]))
        assert turn.final_candidates[0].raw == "done"

    @pytest.mark.asyncio
    async def test_not_terminal_with_calls(self):
        engine = FakeThinkEngine(tool_calls=[native_call("1", "Read")])
        turn = await NativeToolChannel().model_turn(engine)
        assert not turn.final_candidates
        assert turn.actions[0].kind == "tool_call"

    @pytest.mark.asyncio
    async def test_none_calls_not_terminal(self):
        # tool_calls is None (XML-style) -> not the native terminal condition.
        turn = await NativeToolChannel().model_turn(FakeThinkEngine(content="xml", tool_calls=None))
        assert not turn.final_candidates

    @pytest.mark.asyncio
    async def test_joins_before_reading_pending_result(self):
        # When the think task is still running, is_terminal must join first so it
        # reads *this* round's result rather than a stale one.
        engine = FakeThinkEngine(tool_calls=[], done=False)
        assert (await NativeToolChannel().model_turn(engine)).final_candidates
        assert engine.join_calls == 1

    @pytest.mark.asyncio
    async def test_done_engine_is_not_joined(self):
        # Already-finished round: no wasted join, just read the result.
        engine = FakeThinkEngine(tool_calls=[], done=True)
        assert (await NativeToolChannel().model_turn(engine)).final_candidates
        assert engine.join_calls == 0

    @pytest.mark.asyncio
    async def test_pending_with_calls_joins_then_not_terminal(self):
        # Still running + has calls -> join first, then report non-terminal.
        engine = FakeThinkEngine(tool_calls=[native_call("1", "Read")], done=False)
        assert not (await NativeToolChannel().model_turn(engine)).final_candidates
        assert engine.join_calls == 1

    @pytest.mark.asyncio
    async def test_pending_none_calls_joins_then_not_terminal(self):
        # None tool_calls (XML-style) while pending -> join, still not terminal.
        engine = FakeThinkEngine(tool_calls=None, done=False)
        assert not (await NativeToolChannel().model_turn(engine)).final_candidates
        assert engine.join_calls == 1
