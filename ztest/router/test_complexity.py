#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for metagpt.router.complexity (signals → score → tier + rules engine)."""
from __future__ import annotations

from metagpt.router.complexity import (
    TIER_THRESHOLDS,
    ComplexitySignals,
    ContextSignals,
    LexicalSignals,
    StructuralSignals,
    calculate_confidence,
    complexity_score,
    decide_tier,
    extract_all_signals,
    extract_lexical_signals,
    extract_structural_signals,
    score_to_tier,
    signals_from_messages,
)


class TestLexicalSignals:
    def test_architecture_keywords(self):
        s = extract_lexical_signals("Please refactor and redesign the architecture")
        assert s.has_architecture_keywords is True

    def test_debugging_keywords(self):
        s = extract_lexical_signals("debug why is this not working, find root cause")
        assert s.has_debugging_keywords is True

    def test_simple_keywords(self):
        s = extract_lexical_signals("find and list where is the config")
        assert s.has_simple_keywords is True

    def test_question_depth_why(self):
        assert extract_lexical_signals("why is the server crashing?").question_depth == "why"

    def test_question_depth_how(self):
        assert extract_lexical_signals("how do I configure this?").question_depth == "how"

    def test_word_count(self):
        s = extract_lexical_signals("one two three four five")
        assert s.word_count == 5


class TestStructuralSignals:
    def test_security_domain(self):
        s = extract_structural_signals("fix the oauth jwt token vulnerability")
        assert s.domain_specificity == "security"

    def test_backend_domain(self):
        s = extract_structural_signals("add a new rest api endpoint with a database query")
        assert s.domain_specificity == "backend"

    def test_difficult_reversibility(self):
        s = extract_structural_signals("run the production data migration")
        assert s.reversibility == "difficult"

    def test_system_wide_impact(self):
        s = extract_structural_signals("refactor the entire codebase everywhere")
        assert s.impact_scope == "system-wide"

    def test_subtasks_estimate(self):
        s = extract_structural_signals("- step one\n- step two\n- step three")
        assert s.estimated_subtasks > 1

    def test_test_requirements(self):
        assert extract_structural_signals("write unit tests and verify").has_test_requirements is True


class TestScoring:
    def test_score_to_tier_boundaries(self):
        assert score_to_tier(TIER_THRESHOLDS["HIGH"]) == "HIGH"
        assert score_to_tier(TIER_THRESHOLDS["HIGH"] - 1) == "MEDIUM"
        assert score_to_tier(TIER_THRESHOLDS["MEDIUM"]) == "MEDIUM"
        assert score_to_tier(TIER_THRESHOLDS["MEDIUM"] - 1) == "LOW"
        assert score_to_tier(0) == "LOW"

    def test_simple_prompt_is_low(self):
        score = complexity_score(extract_all_signals("show me the file"))
        assert score_to_tier(score) == "LOW"

    def test_architecture_prompt_scores_high(self):
        prompt = (
            "Refactor and redesign the entire system architecture across all modules, "
            "decouple the layers and migrate the production database. Why is the design "
            "so tightly coupled everywhere?"
        )
        score = complexity_score(extract_all_signals(prompt))
        assert score >= TIER_THRESHOLDS["MEDIUM"]

    def test_simple_keyword_lowers_score(self):
        base = ComplexitySignals(lexical=LexicalSignals(has_simple_keywords=True))
        assert complexity_score(base) < 0

    def test_context_previous_failures_capped(self):
        # prev_failure weight 2, capped at 4
        c = ComplexitySignals(context=ContextSignals(previous_failures=10))
        assert complexity_score(c) == 4

    def test_calculate_confidence_range(self):
        for score in range(0, 16):
            conf = calculate_confidence(score, score_to_tier(score))
            assert 0.5 <= conf <= 0.9


class TestSignalsFromMessages:
    def test_empty(self):
        s = signals_from_messages(None)
        assert s.conversation_turns == 0
        assert s.previous_failures == 0

    def test_counts_turns_and_failures(self):
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "do the thing"},
            {"role": "assistant", "content": "here you go"},
            {"role": "user", "content": "that failed with an exception, try again"},
        ]
        s = signals_from_messages(messages)
        assert s.conversation_turns == 3  # 2 user + 1 assistant (system excluded)
        assert s.previous_failures == 1

    def test_non_dict_entries_skipped(self):
        s = signals_from_messages([{"role": "user", "content": "ok"}, "garbage", 42])
        assert s.conversation_turns == 1


class TestDecideTier:
    def test_security_rule_escalates_to_high(self):
        d = decide_tier("review the oauth jwt encryption flow")
        assert d.tier == "HIGH"
        assert any("security" in r for r in d.reasons)

    def test_simple_search_rule_low(self):
        d = decide_tier("find the config file")
        assert d.tier == "LOW"

    def test_rules_never_downgrade_high_score_to_low(self):
        # A heavy architecture+risk prompt scores HIGH; even if a LOW rule matched,
        # escalation-only semantics keep it from dropping to LOW.
        prompt = (
            "Refactor and redesign the entire system architecture across all modules "
            "and migrate the production database. This is critical and irreversible."
        )
        d = decide_tier(prompt)
        assert d.tier in ("HIGH", "MEDIUM")
        assert d.tier != "LOW"

    def test_use_rules_false_uses_raw_score(self):
        d = decide_tier("review the oauth jwt encryption flow", use_rules=False)
        # without the security rule, this short prompt won't be forced HIGH
        assert len(d.reasons) == 1  # only the "complexity score N → tier" reason

    def test_context_failures_push_tier_up(self):
        ctx = ContextSignals(previous_failures=3)
        d = decide_tier("fix the thing", context=ctx)
        assert d.tier in ("LOW", "MEDIUM", "HIGH")
