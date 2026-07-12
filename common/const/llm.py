#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""LLM-related constants — model capability lists and checks."""

from typing import Optional

MULTI_MODAL_MODELS = ["gpt-4o", "gpt-4o-mini", "sonnet", "opus", "gemini", "gpt"]

# Model-name substrings whose models accept native PDF (document) input. Only
# Anthropic Claude models do today via the Messages ``document`` block.
PDF_INPUT_MODELS = ["claude"]


def supports_vision(model: Optional[str]) -> bool:
    """Whether ``model`` accepts image input (name-substring match).

    THE single authority for the image-capability check. Both
    ``BaseLLM.support_image_input`` and ``ModelCard.supports_vision`` delegate
    here so the two never drift. Coarse by design: a substring hit against
    ``MULTI_MODAL_MODELS`` (e.g. any ``gpt-*`` or ``*sonnet*``).
    """
    return any(m in (model or "") for m in MULTI_MODAL_MODELS)


def supports_pdf_input(model: Optional[str]) -> bool:
    """Whether ``model`` accepts native PDF (document) input (name-substring match).

    THE single authority for the PDF-capability check (was a bare
    ``"claude" in model`` inline in ``_user_msg_with_media``). Substring match
    against ``PDF_INPUT_MODELS``.
    """
    return any(m in (model or "").lower() for m in PDF_INPUT_MODELS)
