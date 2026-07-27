"""Tests for the shared model-capability predicates in common.const.llm.

``supports_vision`` / ``supports_pdf_input`` are the single authority behind
provider adapters and the semantic route catalog
in ``_user_msg_with_media`` — previously each site re-ran the substring match.
"""

import pytest

from mote.contracts.models.capabilities import (
    supports_native_tool_search,
    supports_pdf_input,
    supports_vision,
    supports_web_search,
)


class TestSupportsVision:
    @pytest.mark.parametrize(
        "model",
        [
            # OpenAI vision-capable
            "gpt-4o",
            "gpt-4.1",
            "gpt-5",
            "o3",
            "o4-mini",
            # Anthropic (Claude 3+ Sonnet/Opus)
            "claude-3-opus",
            "claude-sonnet-4-6",
            # Google
            "gemini-1.5-pro",
            # Chinese vision VARIANTS (narrow markers, not bare brand)
            "qwen-vl-max",
            "qwen3-vl",
            "deepseek-vl2",
            "glm-4v",
            "glm-4.6v",
            "hunyuan-vision",
            "kimi-vl",
            "minimax-vl-01",
        ],
    )
    def test_true_for_multimodal(self, model):
        assert supports_vision(model) is True

    @pytest.mark.parametrize(
        "model",
        # 'sonnet'/'opus' are the Claude entries — 'haiku' is deliberately absent.
        # The Chinese TEXT-ONLY flagships must NOT be flagged just by brand — the
        # vision markers ("-vl"/"-v"/"vision") gate per-variant, so the bare
        # text models fall through correctly.
        [
            "gpt-3.5-turbo",  # no vision (pre-4o)
            "deepseek-chat",
            "deepseek-v4",
            "qwen-max",
            "glm-4",
            "kimi-k2",
            "hunyuan-turbo",
            "claude-3-haiku",
        ],
    )
    def test_false_for_non_multimodal(self, model):
        assert supports_vision(model) is False

    def test_empty_and_none(self):
        assert supports_vision("") is False
        assert supports_vision(None) is False


class TestSupportsPdfInput:
    @pytest.mark.parametrize("model", ["claude-3-opus", "claude-sonnet-4-6", "CLAUDE-3-HAIKU"])
    def test_true_for_claude_any_case(self, model):
        assert supports_pdf_input(model) is True

    @pytest.mark.parametrize("model", ["gpt-4o", "gemini-1.5-pro", "deepseek-chat"])
    def test_false_for_non_claude(self, model):
        assert supports_pdf_input(model) is False

    def test_empty_and_none(self):
        assert supports_pdf_input("") is False
        assert supports_pdf_input(None) is False


class TestSupportsNativeToolSearch:
    """THE single capability gate behind all three tool-search wire projections.

    True → provider takes over tool search on its native wire (Anthropic
    ``tool_reference`` / OpenAI Responses ``tool_search``, both ``defer_loading``);
    False → the shared client-side withhold/reveal fallback. Coarse substring
    match against ``NATIVE_TOOL_SEARCH_MODELS`` (case-insensitive).
    """

    @pytest.mark.parametrize(
        "model",
        # Anthropic tool-search-GA family + OpenAI Responses gpt-5.4+.
        [
            "opus-4",
            "claude-opus-4-8",
            "claude-sonnet-4-6",
            "haiku-4",
            "claude-haiku-4-5",
            "gpt-5.4",
            "gpt-5.5",
            "GPT-5.4",  # case-insensitive
        ],
    )
    def test_true_for_capable(self, model):
        assert supports_native_tool_search(model) is True

    @pytest.mark.parametrize(
        "model",
        # Old Claude (claude-3-*) + old/mid GPT (gpt-4*, gpt-5.0–5.3) — the
        # latent-bug guard: these must NOT be stamped with defer_loading.
        ["claude-3-5-sonnet", "claude-3-opus", "gpt-4o", "gpt-5.0", "gpt-5.3", "deepseek-chat"],
    )
    def test_false_for_non_capable(self, model):
        assert supports_native_tool_search(model) is False

    def test_empty_and_none(self):
        assert supports_native_tool_search("") is False
        assert supports_native_tool_search(None) is False


class TestSupportsWebSearch:
    """Gate for the WebSearch tool's server-side secondary call.

    True → the routed model can drive a provider-native server-side web search
    (Anthropic ``web_search_20250305`` / OpenAI Responses ``web_search``); False →
    the WebSearch tool degrades to a "use WebBrowser" notice. Coarse substring
    match against ``WEB_SEARCH_MODELS`` (case-insensitive).
    """

    @pytest.mark.parametrize(
        "model",
        # Anthropic opus-4/sonnet-4/haiku-4.5+ family PLUS the broad OpenAI web
        # search support: GPT-4o series, GPT-4.1 series, o-series (o3/o4), GPT-5.
        # (Web search is FAR wider than tool_search's gpt-5.4+ — do not conflate.)
        [
            "opus-4",
            "claude-opus-4-8",
            "claude-sonnet-4-6",
            "claude-haiku-4-5-20251001",
            "gpt-4o",
            "gpt-4o-mini",
            "gpt-4.1",
            "o3",
            "o4-mini",
            "gpt-5.0",
            "gpt-5.4",
            "GPT-5.5",  # case-insensitive
        ],
    )
    def test_true_for_capable(self, model):
        assert supports_web_search(model) is True

    @pytest.mark.parametrize(
        "model",
        # Old Claude (claude-3-*) and non-first-party models have no server-side
        # web search; they degrade the WebSearch tool to a "use WebBrowser" notice.
        ["claude-3-5-sonnet", "claude-3-opus", "deepseek-chat", "qwen-max"],
    )
    def test_false_for_non_capable(self, model):
        assert supports_web_search(model) is False

    def test_empty_and_none(self):
        assert supports_web_search("") is False
        assert supports_web_search(None) is False
