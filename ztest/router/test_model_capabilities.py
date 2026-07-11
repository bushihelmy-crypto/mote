"""Tests for the shared model-capability predicates in common.const.llm.

``supports_vision`` / ``supports_pdf_input`` are the single authority behind
``BaseLLM.support_image_input``, ``ModelCard.supports_vision``, and the PDF gate
in ``_user_msg_with_media`` — previously each site re-ran the substring match.
"""

import pytest
from mote.common.const.llm import supports_pdf_input, supports_vision


class TestSupportsVision:
    @pytest.mark.parametrize(
        "model",
        ["gpt-3.5-turbo", "gpt-4o", "claude-3-opus", "claude-sonnet-4-6", "gemini-1.5-pro"],
    )
    def test_true_for_multimodal(self, model):
        assert supports_vision(model) is True

    @pytest.mark.parametrize(
        "model",
        # 'sonnet'/'opus' are the Claude entries — 'haiku' is deliberately absent.
        ["deepseek-chat", "qwen-max", "claude-3-haiku"],
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
