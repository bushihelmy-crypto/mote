#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the DeepSeek provider (DeepSeekLLM) and its DSML tool-call salvage.

Two layers are covered:

* ``router.llm.dsml`` — the pure DSML decoder functions, exercised against the
  exact wire format observed from real gateway leaks (fullwidth ``｜｜DSML｜｜``
  markers, ``string="true"`` / ``string="false"`` parameter coercion, multiple
  invokes per block, malformed-input tolerance).
* ``DeepSeekLLM`` — the read-side override that salvages leaked DSML ONLY when
  the structured ``tool_calls`` come back empty, leaving the well-formed path
  untouched. Responses are hand-built ``SimpleNamespace`` objects mirroring the
  OpenAI ``ChatCompletion`` shape the inherited methods parse — no network.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from mote.common.config.config.llm_config import LLMConfig, LLMType
from mote.router.llm.deepseek_api import DeepSeekLLM
from mote.router.llm.dsml import contains_dsml, parse_dsml_tool_calls
from mote.router.llm.llm_provider_registry import LLM_REGISTRY, create_llm_instance, resolve_api_type

# Fullwidth vertical line (U+FF5C), doubled — the real DSML separator.
_BAR = "\uff5c\uff5c"


def _dsml(invokes: str) -> str:
    return f"<{_BAR}DSML{_BAR}tool_calls>\n{invokes}\n</{_BAR}DSML{_BAR}tool_calls>"


def _invoke(name: str, params: str) -> str:
    return f'<{_BAR}DSML{_BAR}invoke name="{name}">\n{params}\n</{_BAR}DSML{_BAR}invoke>'


def _param(name: str, value: str, string: str = "true") -> str:
    return f'<{_BAR}DSML{_BAR}parameter name="{name}" string="{string}">{value}</{_BAR}DSML{_BAR}parameter>'


# -- decoder: contains_dsml --------------------------------------------------
def test_contains_dsml_detects_marker():
    assert contains_dsml(_dsml(_invoke("X", "")))


def test_contains_dsml_false_on_plain_text():
    assert not contains_dsml("just a normal answer")
    assert not contains_dsml("")
    assert not contains_dsml(None)  # type: ignore[arg-type]


# -- decoder: parse_dsml_tool_calls ------------------------------------------
def test_parse_single_invoke_string_param():
    # Mirrors log line ~35661: GetNodeState(task_id="bg_1").
    content = "Let me check progress.\n" + _dsml(_invoke("GetNodeState", _param("task_id", "bg_1", "true")))
    calls, remaining = parse_dsml_tool_calls(content)
    assert calls == [{"id": None, "name": "GetNodeState", "arguments": {"task_id": "bg_1"}}]
    # The DSML block is stripped; the surrounding prose survives (trimmed).
    assert remaining == "Let me check progress."


def test_parse_string_false_coerces_number():
    # Mirrors log line ~33171: Sleep(duration_seconds=10) as a raw literal.
    content = _dsml(_invoke("Sleep", _param("duration_seconds", "10", "false")))
    calls, _ = parse_dsml_tool_calls(content)
    assert calls[0]["arguments"] == {"duration_seconds": 10}
    assert isinstance(calls[0]["arguments"]["duration_seconds"], int)


def test_parse_string_false_bool_and_null():
    params = _param("flag", "true", "false") + "\n" + _param("opt", "null", "false")
    content = _dsml(_invoke("Cfg", params))
    calls, _ = parse_dsml_tool_calls(content)
    assert calls[0]["arguments"] == {"flag": True, "opt": None}


def test_parse_string_false_falls_back_to_raw_on_bad_json():
    # Non-JSON literal under string="false" degrades to the raw text, never raises.
    content = _dsml(_invoke("Cfg", _param("x", "not json", "false")))
    calls, _ = parse_dsml_tool_calls(content)
    assert calls[0]["arguments"] == {"x": "not json"}


def test_parse_multiple_invokes_in_one_block():
    body = _invoke("A", _param("p", "1")) + "\n" + _invoke("B", _param("q", "2"))
    calls, _ = parse_dsml_tool_calls(_dsml(body))
    assert [c["name"] for c in calls] == ["A", "B"]
    assert calls[0]["arguments"] == {"p": "1"}
    assert calls[1]["arguments"] == {"q": "2"}


def test_parse_multiple_params_in_one_invoke():
    params = _param("a", "x") + "\n" + _param("b", "y")
    calls, _ = parse_dsml_tool_calls(_dsml(_invoke("T", params)))
    assert calls[0]["arguments"] == {"a": "x", "b": "y"}


def test_parse_no_dsml_returns_text_unchanged():
    calls, remaining = parse_dsml_tool_calls("plain text only")
    assert calls == []
    assert remaining == "plain text only"


def test_parse_malformed_block_returns_empty():
    # Opening marker present (so contains_dsml is True) but no closing invoke —
    # nothing parses, the original content is returned untouched.
    broken = f"<{_BAR}DSML{_BAR}tool_calls>\ngarbage without closing"
    calls, remaining = parse_dsml_tool_calls(broken)
    assert calls == []
    assert remaining == broken


# -- DeepSeekLLM response fakes ----------------------------------------------
def _function(name, arguments):
    return SimpleNamespace(name=name, arguments=arguments)


def _tool_call(id, name, arguments):
    return SimpleNamespace(id=id, type="function", function=_function(name, arguments))


def _message(content=None, tool_calls=None):
    return SimpleNamespace(content=content, tool_calls=tool_calls)


def _rsp(content=None, tool_calls=None):
    return SimpleNamespace(choices=[SimpleNamespace(message=_message(content, tool_calls))])


def _make_llm() -> DeepSeekLLM:
    config = LLMConfig(api_type=LLMType.DEEPSEEK, model="deepseek-v4-pro", api_key="sk-test")
    return DeepSeekLLM(config)


# -- DeepSeekLLM: salvage on empty tool_calls --------------------------------
def test_salvage_when_tool_calls_empty():
    llm = _make_llm()
    leaked = "Let me check progress.\n" + _dsml(_invoke("GetNodeState", _param("task_id", "bg_1")))
    rsp = _rsp(content=leaked, tool_calls=None)
    calls = llm.get_choice_tool_calls(rsp)
    assert len(calls) == 1
    assert calls[0]["name"] == "GetNodeState"
    assert calls[0]["arguments"] == {"task_id": "bg_1"}
    # An id is minted (DSML carries none) so downstream tool/result pairing works.
    assert calls[0]["id"] == "dsml_0"


def test_salvage_strips_dsml_from_text():
    llm = _make_llm()
    leaked = "Let me check progress.\n" + _dsml(_invoke("GetNodeState", _param("task_id", "bg_1")))
    rsp = _rsp(content=leaked, tool_calls=None)
    # The visible text has the DSML block removed.
    assert llm.get_choice_text(rsp) == "Let me check progress."


def test_salvage_mints_distinct_ids_for_multiple_calls():
    llm = _make_llm()
    body = _invoke("A", _param("p", "1")) + "\n" + _invoke("B", _param("q", "2"))
    rsp = _rsp(content=_dsml(body), tool_calls=None)
    calls = llm.get_choice_tool_calls(rsp)
    assert [c["id"] for c in calls] == ["dsml_0", "dsml_1"]


# -- DeepSeekLLM: well-formed path untouched ---------------------------------
def test_structured_tool_calls_take_precedence():
    llm = _make_llm()
    rsp = _rsp(
        content="",
        tool_calls=[_tool_call("call_1", "Bash", json.dumps({"command": "ls"}))],
    )
    calls = llm.get_choice_tool_calls(rsp)
    assert calls == [{"id": "call_1", "name": "Bash", "arguments": {"command": "ls"}}]


def test_text_preserved_when_structured_calls_present():
    llm = _make_llm()
    # Even if DSML somehow co-occurs with real tool_calls, the salvage path is
    # skipped and the content is returned verbatim.
    rsp = _rsp(
        content="some text",
        tool_calls=[_tool_call("call_1", "Bash", json.dumps({"command": "ls"}))],
    )
    assert llm.get_choice_text(rsp) == "some text"


def test_plain_text_response_not_disturbed():
    llm = _make_llm()
    rsp = _rsp(content="Just a normal answer.", tool_calls=None)
    assert llm.get_choice_tool_calls(rsp) == []
    assert llm.get_choice_text(rsp) == "Just a normal answer."


def test_empty_calls_and_no_dsml_returns_empty():
    llm = _make_llm()
    rsp = _rsp(content="no markup here", tool_calls=None)
    assert llm.get_choice_tool_calls(rsp) == []


# -- provider routing --------------------------------------------------------
def test_deepseek_registered():
    assert LLM_REGISTRY.get_provider(LLMType.DEEPSEEK) is DeepSeekLLM


def test_resolve_api_type_by_model_name():
    # api_type stays openai (shared gateway), but the deepseek model name routes
    # to the DeepSeek provider so the salvage is available.
    config = LLMConfig(api_type=LLMType.OPENAI, model="deepseek-v4-pro", api_key="x")
    assert resolve_api_type(config) == LLMType.DEEPSEEK


def test_resolve_api_type_explicit():
    config = LLMConfig(api_type=LLMType.DEEPSEEK, model="whatever", api_key="x")
    assert resolve_api_type(config) == LLMType.DEEPSEEK


def test_create_llm_instance_for_deepseek():
    config = LLMConfig(api_type=LLMType.OPENAI, model="deepseek-v4-pro", api_key="x")
    llm = create_llm_instance(config)
    assert isinstance(llm, DeepSeekLLM)


def test_non_deepseek_openai_model_unaffected():
    config = LLMConfig(api_type=LLMType.OPENAI, model="gpt-4o", api_key="x")
    assert resolve_api_type(config) == LLMType.OPENAI
