#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the human-interaction tools (``mote.product.toolsets.builtin.human``).

Covers AskUser + ReplyToUser (thin delegations to the ask_user /
reply_to_user Role capabilities) and AskUserQuestion (which runs
an end-to-end *structured* round-trip: questions go out as typed items, answers
come back as ``AskUserQuestionAnswers`` with ``selected`` labels + ``free_text``
kept in separate fields — no text rendering / parsing). CapRole fakes the
structured channel with scriptable answers.
"""
from __future__ import annotations

import pytest

from mote.contracts.interaction import AskUserQuestionAnswer, AskUserQuestionAnswers
from mote.product.toolsets.builtin.human import AskUser, AskUserQuestion, ReplyToUser
from mote.runtime.tools.tool_result import ToolError, ToolResult

from .conftest import CapRole, bind, run


def _call(tool, **kwargs):
    return run(tool.call(**kwargs))


class TestAskUser:
    def test_returns_human_reply(self, workspace):
        role = CapRole(ask_reply="the answer is 42")
        tool = bind(AskUser(), role)
        out = _call(tool, question="what is the answer?")
        assert out == "the answer is 42"
        assert role.ask_questions == ["what is the answer?"]


class TestReplyToUser:
    def test_echoes_content(self, workspace):
        role = CapRole()
        tool = bind(ReplyToUser(), role)
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


def _answers(*answers):
    """Build a scripted AskUserQuestionAnswers from (question, selected, free) tuples."""
    return AskUserQuestionAnswers(
        answers=[AskUserQuestionAnswer(question=q, selected=list(sel), free_text=free) for q, sel, free in answers]
    )


class TestAskUserQuestionSingle:
    def test_selection_returns_structured_and_formatted(self, workspace):
        role = CapRole()
        role.ask_answers = _answers(("Pick", ["Blue"], ""))
        tool = bind(AskUserQuestion(), role)
        result = _call(tool, questions=[_q("Pick", "P", [("Red", ""), ("Blue", "")])])
        assert isinstance(result, ToolResult)
        assert '"Pick"="Blue"' in result.output
        assert "User has answered your questions" in result.output
        # The structured answers ride along on ``data`` (does not enter history).
        assert isinstance(result.data, AskUserQuestionAnswers)
        assert result.data.answers[0].selected == ["Blue"]
        # The tool passed typed AskUserQuestionItem models through the channel.
        items = role.ask_question_items[0]
        assert items[0].question == "Pick"

    def test_free_text_answer_verbatim(self, workspace):
        role = CapRole()
        role.ask_answers = _answers(("Pick", [], "Green please"))
        tool = bind(AskUserQuestion(), role)
        result = _call(tool, questions=[_q("Pick", "P", [("Red", ""), ("Blue", "")])])
        assert '"Pick"="Green please"' in result.output
        assert result.data.answers[0].free_text == "Green please"

    def test_multiselect_joins_labels(self, workspace):
        role = CapRole()
        role.ask_answers = _answers(("Toppings", ["Cheese", "Olives"], ""))
        tool = bind(AskUserQuestion(), role)
        result = _call(
            tool,
            questions=[_q("Toppings", "T", [("Cheese", ""), ("Ham", ""), ("Olives", "")], multiSelect=True)],
        )
        assert '"Toppings"="Cheese, Olives"' in result.output


class TestAskUserQuestionMulti:
    def test_answers_pair_by_question_key(self, workspace):
        role = CapRole()
        role.ask_answers = _answers(("Color?", ["Red"], ""), ("Size?", ["Large"], ""))
        tool = bind(AskUserQuestion(), role)
        result = _call(
            tool,
            questions=[
                _q("Color?", "C", [("Red", ""), ("Blue", "")]),
                _q("Size?", "S", [("Small", ""), ("Large", "")]),
            ],
        )
        assert '"Color?"="Red"' in result.output
        assert '"Size?"="Large"' in result.output

    def test_multiline_free_text_does_not_misalign(self, workspace):
        # Regression #2: Q1 free text with newlines must NOT bleed into Q2.
        role = CapRole()
        role.ask_answers = _answers(
            ("Notes?", [], "line one\nline two\nline three"),
            ("Size?", ["Large"], ""),
        )
        tool = bind(AskUserQuestion(), role)
        result = _call(
            tool,
            questions=[
                _q("Notes?", "N", [("Short", ""), ("Long", "")]),
                _q("Size?", "S", [("Small", ""), ("Large", "")]),
            ],
        )
        assert result.data.answers[0].free_text == "line one\nline two\nline three"
        assert result.data.answers[1].selected == ["Large"]
        assert '"Size?"="Large"' in result.output

    def test_numeric_free_text_stays_free_text(self, workspace):
        # Regression #3: a numeric "Other" answer is free text, not an index.
        role = CapRole()
        role.ask_answers = _answers(("How many?", [], "42"))
        tool = bind(AskUserQuestion(), role)
        result = _call(tool, questions=[_q("How many?", "Q", [("One", ""), ("Two", "")])])
        assert result.data.answers[0].free_text == "42"
        assert '"How many?"="42"' in result.output


class TestAskUserQuestionGuards:
    def test_too_many_options_rejected(self, workspace):
        role = CapRole()
        tool = bind(AskUserQuestion(), role)
        bad = _q("Pick", "P", [("a", ""), ("b", ""), ("c", ""), ("d", ""), ("e", "")])
        with pytest.raises(ToolError, match="invalid questions"):
            _call(tool, questions=[bad])

    def test_too_few_options_rejected(self, workspace):
        role = CapRole()
        tool = bind(AskUserQuestion(), role)
        bad = _q("Pick", "P", [("only", "")])
        with pytest.raises(ToolError, match="invalid questions"):
            _call(tool, questions=[bad])

    def test_empty_questions_rejected(self, workspace):
        role = CapRole()
        tool = bind(AskUserQuestion(), role)
        with pytest.raises(ToolError, match="invalid questions"):
            _call(tool, questions=[])


# --- Permission self-check ---------------------------------------------------


class TestHumanToolsSelfApprove:
    """The human-interaction tools must self-approve (no permission prompt).

    Regression: without a ``check_permissions`` override these tools fell through
    to the engine's default→ask step, firing an "[APPROVAL REQUIRED]" prompt
    *before* the question could be asked — which deadlocked the react loop. A
    model-presented AskUserQuestion selection then never reached the model (while
    the user typing directly in the REPL, ungated, still worked).
    """

    @pytest.mark.parametrize("tool_cls", [AskUser, ReplyToUser, AskUserQuestion])
    def test_check_permissions_allows(self, tool_cls):
        decision = tool_cls().check_permissions({})
        assert decision is not None
        assert decision.behavior == "allow"

    def test_engine_does_not_prompt_in_default_mode(self):
        # End-to-end through the engine: a default-mode AskUserQuestion resolves
        # to allow WITHOUT ever calling the approval channel.
        from mote.runtime.tools.permission.engine import PermissionEngine
        from mote.runtime.tools.permission.rule_store import RuleStore

        prompted: list = []

        async def _ask(prompt):
            prompted.append(prompt)
            return "no"

        engine = PermissionEngine(mode="default", store=RuleStore(), ask_user=_ask)
        tool = AskUserQuestion()

        async def _go():
            return await engine.check(
                "AskUserQuestion", target="", tool_check=tool.check_permissions({}), mutates_fs=False
            )

        decision = run(_go())
        assert decision.behavior == "allow"
        assert prompted == []  # never prompted

    def test_deny_rule_still_wins(self):
        # A user-configured bypass-immune deny rule still overrides self-approval,
        # so explicit gating remains possible.
        from mote.contracts.permissions import PermissionRule
        from mote.runtime.tools.permission.engine import PermissionEngine
        from mote.runtime.tools.permission.rule_store import RuleStore

        store = RuleStore()
        store.add_session_rule(
            PermissionRule(tool_name="AskUserQuestion", pattern=None, behavior="deny", source="session")
        )
        engine = PermissionEngine(mode="default", store=store, ask_user=None)
        tool = AskUserQuestion()

        async def _go():
            return await engine.check(
                "AskUserQuestion", target="", tool_check=tool.check_permissions({}), mutates_fs=False
            )

        assert run(_go()).behavior == "deny"


# --- Pure-helper unit tests --------------------------------------------------


class TestFormatResult:
    def test_wording_from_structured_answers(self):
        answers = _answers(("Q", ["A"], ""))
        out = AskUserQuestion._format_result(answers)
        assert out == (
            'User has answered your questions: "Q"="A". ' "You can now continue with the user's answers in mind."
        )

    def test_display_combines_selected_and_free_text(self):
        answers = _answers(("Q", ["A", "B"], "and more"))
        out = AskUserQuestion._format_result(answers)
        assert '"Q"="A, B, and more"' in out
