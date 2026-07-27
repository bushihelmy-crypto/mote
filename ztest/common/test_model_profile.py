"""Tests for the model-name-keyed capability profile registry.

Covers: marker matching, merge precedence (override wins field-by-field / OR
fold over the default), the new ``supports_thinking`` + ``json_schema_transformer``
facets, and a BYTE-EQUIVALENCE parity check proving the migration from the four
old substring lists in ``common/const/llm.py`` changed no verdict for any real
model id.
"""

import pytest

from mote.contracts.models.capabilities import (
    supports_native_tool_search,
    supports_pdf_input,
    supports_vision,
    supports_web_search,
)
from mote.contracts.models.profile import DEFAULT_PROFILE, ModelProfile, merge_profile, profile_for

# The FOUR original substring lists, reproduced verbatim as the parity oracle.
_OLD_MULTI_MODAL = [
    "gpt-4o",
    "gpt-4o-mini",
    "gpt-4.1",
    "gpt-5",
    "o3",
    "o4",
    "sonnet",
    "opus",
    "gemini",
    "-vl",
    "vision",
    "glm-4v",
    "glm-4.1v",
    "glm-4.5v",
    "glm-4.6v",
    "kimi-vl",
]
_OLD_PDF = ["claude"]
_OLD_NATIVE_TOOL_SEARCH = ["opus-4", "sonnet-4", "haiku-4", "gpt-5.4", "gpt-5.5"]
_OLD_WEB_SEARCH = ["opus-4", "sonnet-4", "haiku-4", "gpt-4o", "gpt-4.1", "gpt-5", "o3", "o4"]


def _old_vision(model):
    # NOTE: the old supports_vision was case-SENSITIVE; profile_for normalises
    # to .lower(). Behaviour-preserving for real (lower-case) model ids.
    return any(m in (model or "").lower() for m in _OLD_MULTI_MODAL)


def _old_pdf(model):
    return any(m in (model or "").lower() for m in _OLD_PDF)


def _old_nts(model):
    return any(m in (model or "").lower() for m in _OLD_NATIVE_TOOL_SEARCH)


def _old_web(model):
    return any(m in (model or "").lower() for m in _OLD_WEB_SEARCH)


# A representative matrix spanning every marker family + negatives.
_MATRIX = [
    "claude-3-opus",
    "claude-3-haiku",
    "claude-opus-4-8",
    "claude-sonnet-4-6",
    "claude-haiku-4-5",
    "gpt-4o",
    "gpt-4o-mini",
    "gpt-4.1",
    "gpt-5",
    "gpt-5.4",
    "gpt-5.5",
    "gpt-3.5-turbo",
    "o3",
    "o4-mini",
    "gemini-1.5-pro",
    "qwen-vl-max",
    "qwen-max",
    "deepseek-vl2",
    "deepseek-chat",
    "glm-4v",
    "glm-4.6v",
    "glm-4",
    "hunyuan-vision",
    "hunyuan-turbo",
    "kimi-vl",
    "kimi-k2",
    "minimax-vl-01",
    "",
    None,
]


class TestParity:
    """Each delegate returns identical verdicts to the old substring lists."""

    @pytest.mark.parametrize("model", _MATRIX)
    def test_vision_parity(self, model):
        assert supports_vision(model) == _old_vision(model)

    @pytest.mark.parametrize("model", _MATRIX)
    def test_pdf_parity(self, model):
        assert supports_pdf_input(model) == _old_pdf(model)

    @pytest.mark.parametrize("model", _MATRIX)
    def test_native_tool_search_parity(self, model):
        assert supports_native_tool_search(model) == _old_nts(model)

    @pytest.mark.parametrize("model", _MATRIX)
    def test_web_search_parity(self, model):
        assert supports_web_search(model) == _old_web(model)


class TestProfileFor:
    def test_none_and_unknown_are_all_off(self):
        assert profile_for(None) == DEFAULT_PROFILE
        assert profile_for("some-unknown-model") == DEFAULT_PROFILE

    def test_case_insensitive(self):
        assert profile_for("GPT-4O").supports_vision is True
        assert profile_for("Claude-Opus-4").supports_pdf_input is True

    def test_claude4_accumulates_all_facets(self):
        # "claude-opus-4-8" hits opus(vision) + claude(pdf) + opus-4(nts+web+think).
        p = profile_for("claude-opus-4-8")
        assert p.supports_vision is True
        assert p.supports_pdf_input is True
        assert p.supports_native_tool_search is True
        assert p.supports_web_search is True
        assert p.supports_thinking is True

    def test_claude3_haiku_pdf_only(self):
        # Old claude haiku: pdf via "claude", but no vision (no sonnet/opus) and
        # no native tool search / web (no *-4 marker).
        p = profile_for("claude-3-haiku")
        assert p.supports_pdf_input is True
        assert p.supports_vision is False
        assert p.supports_native_tool_search is False
        assert p.supports_web_search is False

    def test_gpt5_reasoning_facets(self):
        p = profile_for("gpt-5")
        assert p.supports_vision is True
        assert p.supports_web_search is True
        assert p.supports_thinking is True
        assert p.supports_native_tool_search is False

    def test_gpt5_4_adds_native_tool_search(self):
        p = profile_for("gpt-5.4")
        assert p.supports_native_tool_search is True
        # inherits gpt-5's reasoning/web/vision
        assert p.supports_thinking is True
        assert p.supports_web_search is True

    def test_gpt4o_no_thinking(self):
        p = profile_for("gpt-4o")
        assert p.supports_vision is True
        assert p.supports_web_search is True
        assert p.supports_thinking is False

    def test_no_transformer_by_default(self):
        assert profile_for("claude-opus-4-8").json_schema_transformer is None


class TestMergeProfile:
    def test_none_override_returns_base(self):
        base = ModelProfile(supports_vision=True)
        assert merge_profile(base, None) is base

    def test_override_wins_field_by_field_only_for_non_default(self):
        base = ModelProfile(supports_vision=True, supports_pdf_input=True)
        override = ModelProfile(supports_web_search=True)
        merged = merge_profile(base, override)
        # base facets preserved (override left them at default → not applied)
        assert merged.supports_vision is True
        assert merged.supports_pdf_input is True
        # override's non-default facet applied
        assert merged.supports_web_search is True

    def test_transformer_merges_when_set(self):
        def t(schema):
            return schema

        base = DEFAULT_PROFILE
        merged = merge_profile(base, ModelProfile(json_schema_transformer=t))
        assert merged.json_schema_transformer is t

    def test_fold_is_or_over_flags(self):
        p = merge_profile(
            merge_profile(DEFAULT_PROFILE, ModelProfile(supports_vision=True)),
            ModelProfile(supports_pdf_input=True),
        )
        assert p.supports_vision is True
        assert p.supports_pdf_input is True
