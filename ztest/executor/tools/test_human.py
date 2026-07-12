#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the human-interaction tools (``metagpt.executor.tools.human``).

Covers AskHuman + ReplyToHuman (thin delegations to the ask_human /
reply_to_human Role capabilities) and AskUserQuestion (the CC port that renders
multiple-choice questions into one text prompt, sends it through the human text
channel, then parses the reply back to per-question answers). CapRole fakes the
human channel with a scripted reply.
"""
from __future__ import annotations

import pytest

from metagpt.executor.tool_result import ToolError
from metagpt.executor.tools.human import AskHuman, ReplyToHuman, AskUserQuestion

from .conftest import CapRole, bind, run


def _call(tool, **kwargs):
    return run(tool.call(**kwargs))


class TestAskHuman:
    def test_returns_human_reply(self, workspace):
        role = CapRole(ask_reply="the answer is 42")
        tool = bind(AskHuman(), role)
        out = _call(tool, question="what is the answer?")
        assert out == "the answer is 42"
        assert role.ask_questions == ["what is the answer?"]


class TestReplyToHuman:
    def test_echoes_content(self, workspace):
        role = CapRole()
        tool = bind(ReplyToHuman(), role)
        out = _call(tool, content="here is my reply")
        assert out == "here is my reply"


# --- AskUserQuestion ---------------------------------------------------------


def _q(question, header, options, multiSelect=False):
    return {
        "question": question,
        "header": header,
        "options": [{"label": l, "description": d} for l, d in options],
        "multiSelect": multiSelect,
    }


class TestAskUserQuestionRender:
    def test_prompt_lists_options_and_other(self, workspace):
        role = CapRole(ask_reply="1")
        tool = bind(AskUserQuestion(), role)
        _call(tool, questions=[_q("Pick a color", "Color", [("Red", "warm"), ("Blue", "cool")])])
        prompt = role.ask_questions[0]
        assert "Pick a color" in prompt
        assert "1. Red" in prompt
        assert "2. Blue" in prompt
        # An "Other" free-text choice is auto-appended.
        assert "3. Other" in prompt


class TestAskUserQuestionSingle:
    def test_numeric_selection_resolves_to_label(self, workspace):
        role = CapRole(ask_reply="2")
        tool = bind(AskUserQuestion(), role)
        out = _call(tool, questions=[_q("Pick", "P", [("Red", ""), ("Blue", "")])])
        assert '"Pick"="Blue"' in out
        assert "User has answered your questions" in out

    def test_free_text_answer(self, workspace):
        role = CapRole(ask_reply="Green please")
        tool = bind(AskUserQuestion(), role)
        out = _call(tool, questions=[_q("Pick", "P", [("Red", ""), ("Blue", "")])])
        assert '"Pick"="Green please"' in out

    def test_multiselect_numbers_join_labels(self, workspace):
        role = CapRole(ask_reply="1,3")
        tool = bind(AskUserQuestion(), role)
        out = _call(
            tool,
            questions=[_q("Toppings", "T", [("Cheese", ""), ("Ham", ""), ("Olives", "")], multiSelect=True)],
        )
        assert '"Toppings"="Cheese, Olives"' in out


class TestAskUserQuestionMulti:
    def test_lines_pair_with_questions_in_order(self, workspace):
        # Two questions; reply has one line per question.
        role = CapRole(ask_reply="1\n2")
        tool = bind(AskUserQuestion(), role)
        out = _call(
            tool,
            questions=[
                _q("Color?", "C", [("Red", ""), ("Blue", "")]),
                _q("Size?", "S", [("Small", ""), ("Large", "")]),
            ],
        )
        assert '"Color?"="Red"' in out
        assert '"Size?"="Large"' in out


class TestAskUserQuestionGuards:
    def test_too_many_options_rejected(self, workspace):
        role = CapRole(ask_reply="1")
        tool = bind(AskUserQuestion(), role)
        bad = _q("Pick", "P", [("a", ""), ("b", ""), ("c", ""), ("d", ""), ("e", "")])
        with pytest.raises(ToolError, match="invalid questions"):
            _call(tool, questions=[bad])

    def test_too_few_options_rejected(self, workspace):
        role = CapRole(ask_reply="1")
        tool = bind(AskUserQuestion(), role)
        bad = _q("Pick", "P", [("only", "")])
        with pytest.raises(ToolError, match="invalid questions"):
            _call(tool, questions=[bad])

    def test_empty_questions_rejected(self, workspace):
        role = CapRole(ask_reply="1")
        tool = bind(AskUserQuestion(), role)
        with pytest.raises(ToolError, match="invalid questions"):
            _call(tool, questions=[])


# --- Pure-helper unit tests --------------------------------------------------


class TestResolveAnswer:
    def test_strips_echoed_qn_prefix(self):
        from metagpt.common.schema import AskUserQuestionItem

        q = AskUserQuestionItem.model_validate(_q("Pick", "P", [("Red", ""), ("Blue", "")]))
        # Human echoes "Q1: Blue" — the prefix is stripped, free text kept.
        assert AskUserQuestion._resolve_answer(q, "Q1: Blue") == "Blue"

    def test_other_number_yields_raw(self):
        from metagpt.common.schema import AskUserQuestionItem

        q = AskUserQuestionItem.model_validate(_q("Pick", "P", [("Red", ""), ("Blue", "")]))
        # Option 3 is the auto "Other"; it maps to no label, so with no
        # accompanying free text the raw token falls through unchanged.
        assert AskUserQuestion._resolve_answer(q, "3") == "3"

    def test_format_result_wording(self):
        out = AskUserQuestion._format_result({"Q": "A"})
        assert out == (
            'User has answered your questions: "Q"="A". '
            "You can now continue with the user's answers in mind."
        )
