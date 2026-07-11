#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the AskUserQuestion answer schema (``mote.common.schema.askuser``).

The answer models are the backbone of the structured channel that replaced the
lossy text round-trip: ``selected`` (chosen labels) and ``free_text`` (the
"Other" text) live in separate fields, and ``display`` only rebuilds CC's
``"q"="a"`` string at the final formatting boundary. These tests pin the
selection/free-text separation the old round-trip collapsed.
"""

from __future__ import annotations

from mote.common.schema import AskUserQuestionAnswer, AskUserQuestionAnswers


class TestAskUserQuestionAnswer:
    def test_selected_only_display(self):
        a = AskUserQuestionAnswer(question="Pick", selected=["Blue"])
        assert a.display == "Blue"
        assert a.is_free_text is False

    def test_multi_selected_joins_with_comma(self):
        a = AskUserQuestionAnswer(question="Toppings", selected=["Cheese", "Olives"])
        assert a.display == "Cheese, Olives"

    def test_free_text_only_display(self):
        a = AskUserQuestionAnswer(question="How many?", free_text="42")
        assert a.display == "42"
        assert a.is_free_text is True
        assert a.selected == []

    def test_multiline_free_text_kept_verbatim(self):
        # Regression #1/#2: newlines survive intact (no split / no misalign).
        text = "para one\n\npara two\n\n42"
        a = AskUserQuestionAnswer(question="Notes?", free_text=text)
        assert a.free_text == text
        assert a.display == text

    def test_selected_and_free_text_combine(self):
        a = AskUserQuestionAnswer(question="Q", selected=["A", "B"], free_text="and more")
        assert a.display == "A, B, and more"

    def test_empty_answer_display_is_blank(self):
        a = AskUserQuestionAnswer(question="Q")
        assert a.display == ""
        assert a.is_free_text is False


class TestAskUserQuestionAnswers:
    def test_defaults_to_empty_list(self):
        answers = AskUserQuestionAnswers()
        assert answers.answers == []

    def test_holds_multiple_answers(self):
        answers = AskUserQuestionAnswers(
            answers=[
                AskUserQuestionAnswer(question="Color?", selected=["Red"]),
                AskUserQuestionAnswer(question="Size?", free_text="XL"),
            ]
        )
        assert [a.display for a in answers.answers] == ["Red", "XL"]

    def test_round_trips_through_model_dump(self):
        answers = AskUserQuestionAnswers(
            answers=[AskUserQuestionAnswer(header="C", question="Pick", selected=["Blue"], free_text="")]
        )
        restored = AskUserQuestionAnswers.model_validate(answers.model_dump())
        assert restored.answers[0].header == "C"
        assert restored.answers[0].selected == ["Blue"]
